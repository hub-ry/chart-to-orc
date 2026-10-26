"""Provisioning: turn an accepted graph into running, blind agents.

One-shot orchestrator (invoked by POST /accept): writes NATS conf + per-agent creds,
creates per-agent bridge networks + workspace volumes, starts the per-graph NATS
container and one agent container per agent. After this returns there is no
orchestrator role left: the app's only runtime handle is the user/observer account.

Blindness here is structural (DESIGN.md I1-I6): each agent lives on its own bridge
network (no L2/L3 path to any other agent), NATS per-user allow-lists grant exactly
the drawn edges, no shared volumes, no docker socket.

Idempotent: re-running tears down containers/networks (not volumes) and recreates
them from the current graph spec, so it converges.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path

from app.schema import Graph


class ProvisionError(RuntimeError):
    pass


def _run(*args: str, check: bool = True) -> str:
    p = subprocess.run(list(args), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise ProvisionError(f"{' '.join(args[:2])} failed: {p.stderr.strip()[:400]}")
    return p.stdout.strip()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_agent_image(image: str, build_dir: Path) -> None:
    if _run("docker", "image", "inspect", image, check=False):
        return
    _run("docker", "build", "-t", image, str(build_dir))


def _teardown_containers(graph: Graph) -> None:
    _run("docker", "rm", "-f", f"orc-{graph.name}-nats", check=False)
    for a in graph.agents:
        _run("docker", "rm", "-f", f"orc-{graph.name}-{a.id}", check=False)
        _run("docker", "network", "rm", f"orc-{graph.name}-{a.id}", check=False)


def provision(
    graph: Graph,
    data_dir: Path,
    agent_image: str,
    nats_image: str,
    build_dir: Path,
    llm_env: dict[str, str],
) -> dict:
    d = data_dir / graph.name
    d.mkdir(parents=True, exist_ok=True)

    _teardown_containers(graph)

    conf, passwords = graph.nats_users()
    conf += "\nport: 4222\nmax_payload: 1048576\n"
    (d / "nats.conf").write_text(conf)

    ensure_agent_image(agent_image, build_dir)

    port = free_port()
    user = f"orc-{graph.name}-user"
    (d / "user.env").write_text(f'NATS_URL="nats://{user}:{passwords[user]}@127.0.0.1:{port}"\n')

    for a in graph.agents:
        _run("docker", "network", "create", "--driver", "bridge", f"orc-{graph.name}-{a.id}")
        _run("docker", "volume", "create", f"orc-{graph.name}-{a.id}-ws")

    # nats container, multi-homed on every per-agent network, host-loopback port for the app
    _run(
        "docker", "run", "-d", "--name", f"orc-{graph.name}-nats",
        "-p", f"127.0.0.1:{port}:4222",
        "-v", f"{d}/nats.conf:/etc/nats/nats.conf:ro",
        nats_image, "--config", "/etc/nats/nats.conf",
    )
    for a in graph.agents:
        _run(
            "docker", "network", "connect", "--alias", "nats",
            f"orc-{graph.name}-{a.id}", f"orc-{graph.name}-nats",
        )

    for a in graph.agents:
        out_ch = [
            {
                "id": ch.id,
                "name": ch.name or ch.id,
                "to": ch.to,
                "kinds": ch.filter.kinds,
                "max_bytes": ch.filter.max_bytes,
                "rate_per_min": ch.filter.rate_per_min,
            }
            for ch in graph.channels
            if ch.from_ == a.id
        ]
        in_ch = [
            {
                "id": ch.id,
                "name": ch.name or ch.id,
                "from": ch.from_,
                "kinds": ch.filter.kinds,
                "max_bytes": ch.filter.max_bytes,
            }
            for ch in graph.channels
            if ch.to == a.id
        ]
        agent_user = f"orc-{graph.name}-{a.id}"
        env = {
            "AGENT_ID": a.id,
            "AGENT_NAME": a.name or a.id,
            "GRAPH": graph.name,
            "SYSTEM": a.system,
            "TOOLS": ",".join(a.tools),
            "NATS_URL": f"nats://{agent_user}:{passwords[agent_user]}@nats:4222",
            "OUT_CHANNELS": json.dumps(out_ch),
            "IN_CHANNELS": json.dumps(in_ch),
            "WORKSPACE": "/workspace",
            "LLM_BASE_URL": llm_env["base_url"],
            "LLM_API_KEY": llm_env["api_key"],
            "LLM_MODEL": llm_env["model"],
        }
        args = [
            "docker", "run", "-d", "--name", f"orc-{graph.name}-{a.id}",
            "--network", f"orc-{graph.name}-{a.id}",
            "--memory", f"{a.memory_mb}m", "--cpus", str(a.cpus),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-v", f"orc-{graph.name}-{a.id}-ws:/workspace",
        ]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        args.append(agent_image)
        _run(*args)

    # agent markdown files — the clickable prompt surface
    adir = d / "agents"
    adir.mkdir(exist_ok=True)
    for a in graph.agents:
        p = adir / f"{a.id}.md"
        if not p.exists():
            p.write_text(f"# {a.name or a.id}\n\n## role\n{a.system}\n\n## log\n\n")

    (d / "status.json").write_text(
        json.dumps(
            {"state": "live", "nats_port": port, "provisioned_at": time.time()},
            indent=2,
        )
    )
    return {"nats_port": port, "agents": [a.id for a in graph.agents]}


def teardown(graph: Graph, data_dir: Path, rm_data: bool = True) -> None:
    _teardown_containers(graph)
    for a in graph.agents:
        _run("docker", "volume", "rm", f"orc-{graph.name}-{a.id}-ws", check=False)
    if rm_data:
        import shutil

        d = data_dir / graph.name
        if d.is_dir():
            shutil.rmtree(d)
