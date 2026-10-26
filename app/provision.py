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
import os
import socket
import subprocess
import time
from pathlib import Path

from app.schema import Graph, SLUG
from app.config import Config
from app.proxy import graph_token


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


def _write_private(path: Path, text: str) -> None:
    if path.exists():
        path.chmod(0o600)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(text)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def _tracked_agents(directory: Path) -> set[str]:
    inventory = directory / "resources.json"
    if inventory.exists():
        ids = json.loads(inventory.read_text())["agents"]
        if not isinstance(ids, list) or any(not isinstance(aid, str) or not SLUG.fullmatch(aid) for aid in ids):
            raise ProvisionError("invalid provision resource inventory")
        return set(ids)
    # Migrate existing graphs once; subsequent updates can overwrite graph.json.
    spec = directory / "graph.json"
    return {a.id for a in Graph.model_validate_json(spec.read_text()).agents} if spec.exists() else set()


def _teardown_containers(name: str, agents: set[str]) -> None:
    _run("docker", "rm", "-f", f"orc-{name}-nats", check=False)
    for aid in sorted(agents):
        _run("docker", "rm", "-f", f"orc-{name}-{aid}", check=False)
        _run("docker", "network", "rm", f"orc-{name}-{aid}", check=False)


def _remove_volumes(name: str, agents: set[str]) -> None:
    for aid in sorted(agents):
        _run("docker", "volume", "rm", f"orc-{name}-{aid}-ws", check=False)


def provision(
    graph: Graph,
    data_dir: Path,
    agent_image: str,
    nats_image: str,
    build_dir: Path,
    cfg: Config,
) -> dict:
    d = data_dir / graph.name
    d.mkdir(parents=True, exist_ok=True)
    token = graph_token(d, cfg)
    previous = _tracked_agents(d)
    current = {a.id for a in graph.agents}
    known = previous | current
    # Save intent before any Docker mutation, independently of the editable spec.
    _write_private(d / "resources.json", json.dumps({"agents": sorted(known)}))
    _write_private(d / "status.json", json.dumps({"state": "provisioning"}))
    created_volumes: set[str] = set()
    try:
        _teardown_containers(graph.name, known)
        _remove_volumes(graph.name, previous - current)
        out = _start(graph, d, agent_image, nats_image, build_dir, cfg, token, created_volumes)
        _write_private(d / "resources.json", json.dumps({"agents": sorted(current)}))
        _write_private(d / "status.json", json.dumps(
            {"state": "live", "nats_port": out["nats_port"], "provisioned_at": time.time()},
            indent=2,
        ))
        return out
    except Exception as error:
        _teardown_containers(graph.name, known)
        _remove_volumes(graph.name, created_volumes)
        _write_private(d / "status.json", json.dumps({
            "state": "failed", "error": f"{type(error).__name__}: provisioning failed",
        }))
        raise


def _start(
    graph: Graph,
    d: Path,
    agent_image: str,
    nats_image: str,
    build_dir: Path,
    cfg: Config,
    token: str,
    created_volumes: set[str],
) -> dict:
    proxy_url = f"http://host.docker.internal:{cfg.proxy_port}"

    conf, passwords = graph.nats_users()
    conf += "\nport: 4222\nmax_payload: 1048576\n"
    _write_private(d / "nats.conf", conf)

    ensure_agent_image(agent_image, build_dir)

    port = free_port()
    user = f"orc-{graph.name}-user"
    _write_private(d / "user.env", f'NATS_URL="nats://{user}:{passwords[user]}@127.0.0.1:{port}"\n')

    for a in graph.agents:
        _run("docker", "network", "create", "--driver", "bridge", f"orc-{graph.name}-{a.id}")
        volume = f"orc-{graph.name}-{a.id}-ws"
        if not _run("docker", "volume", "inspect", volume, check=False):
            # Include a possibly-created volume even if its create command fails.
            created_volumes.add(a.id)
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
            "SYSTEM": a.system + (
                "\n\nResult bodies sent to the user must match this JSON Schema:\n"
                + json.dumps(graph.result_schema, ensure_ascii=False)
                if graph.result_schema is not None else ""
            ),
            "TOOLS": ",".join(a.tools),
            "NATS_URL": f"nats://{agent_user}:{passwords[agent_user]}@nats:4222",
            "OUT_CHANNELS": json.dumps(out_ch),
            "IN_CHANNELS": json.dumps(in_ch),
            "WORKSPACE": "/workspace",
            "MAX_HOPS": str(cfg.max_hops),
            "LLM_BASE_URL": proxy_url + "/llm",
            "LLM_API_KEY": token,
            "LLM_MODEL": cfg.llm_model,
            "VISION_BASE_URL": proxy_url + "/vision",
            "VISION_API_KEY": token,
            "VISION_MODEL": cfg.vision_model,
        }
        args = [
            "docker", "run", "-d", "--name", f"orc-{graph.name}-{a.id}",
            "--network", f"orc-{graph.name}-{a.id}",
            "--add-host", "host.docker.internal:host-gateway",
            "--memory", f"{a.memory_mb}m", "--cpus", str(a.cpus),
            "--pids-limit", "256",
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

    return {"nats_port": port, "agents": [a.id for a in graph.agents]}


def teardown(graph: Graph, data_dir: Path, rm_data: bool = True) -> None:
    agents = _tracked_agents(data_dir / graph.name) | {a.id for a in graph.agents}
    _teardown_containers(graph.name, agents)
    _remove_volumes(graph.name, agents)
    if rm_data:
        import shutil

        d = data_dir / graph.name
        if d.is_dir():
            shutil.rmtree(d)
