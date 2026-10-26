"""Offline resource lifecycle regressions from the mid-hardening review."""
from __future__ import annotations

import json
import os
import stat

from evals.host_cases import _client, _graph, _main, _seed


class Resources:
    def __init__(self, ctx):
        self.ctx = ctx
        self.containers = set()
        self.networks = set()
        self.volumes = set()
        self.commands = []
        self.fail = None
        ctx.patch("app.provision._run", self.run)
        ctx.patch("app.provision.free_port", lambda: 18765)

    def run(self, *args, **kwargs):
        from app.provision import ProvisionError
        self.commands.append(args)
        if self.fail and self.fail(args):
            self.fail = None
            raise ProvisionError("injected offline provisioning failure")
        if args[:3] == ("docker", "image", "inspect"):
            return "image"
        if args[:3] == ("docker", "volume", "inspect"):
            return "volume" if args[-1] in self.volumes else ""
        if args[:3] == ("docker", "run", "-d"):
            self.containers.add(args[args.index("--name") + 1])
        elif args[:3] == ("docker", "rm", "-f"):
            self.containers.discard(args[-1])
        elif args[:3] == ("docker", "network", "create"):
            self.networks.add(args[-1])
        elif args[:3] == ("docker", "network", "rm"):
            self.ctx.require(args[-1] not in self.containers and "orc-g-nats" not in self.containers,
                             "network removed before attached graph containers")
            self.networks.discard(args[-1])
        elif args[:3] == ("docker", "volume", "create"):
            self.volumes.add(args[-1])
        elif args[:3] == ("docker", "volume", "rm"):
            self.volumes.discard(args[-1])
        return ""

    def provision(self, cfg, graph):
        from app.provision import provision
        return provision(graph, cfg.data_dir, cfg.agent_image, cfg.nats_image, self.ctx.path, cfg)


def _failed(ctx, resources, cfg, graph):
    from app.provision import ProvisionError
    try:
        resources.provision(cfg, graph)
    except ProvisionError:
        pass
    else:
        ctx.require(False, "injected failure did not abort provision")
    status = json.loads((ctx.path / graph.name / "status.json").read_text())
    ctx.require(status.get("state") != "live", "failed reaccept retained live status")


async def credential_modes(ctx):
    cfg = ctx.config()
    resources = Resources(ctx)
    directory = _seed(ctx)
    old_umask = os.umask(0)
    try:
        resources.provision(cfg, _graph())
        for name in ("nats.conf", "user.env"):
            path = directory / name
            ctx.require(stat.S_IMODE(path.stat().st_mode) == 0o600,
                        f"new {name} exposes broker credentials")
            path.chmod(0o666)
        resources.provision(cfg, _graph())
        for name in ("nats.conf", "user.env"):
            ctx.require(stat.S_IMODE((directory / name).stat().st_mode) == 0o600,
                        f"legacy permissive {name} was not repaired")
    finally:
        os.umask(old_umask)


async def overwritten_spec_cleanup(ctx):
    from app.provision import teardown
    cfg = ctx.config()
    resources = Resources(ctx)
    directory = _seed(ctx)
    resources.provision(cfg, _graph())
    reduced = _graph(agents=("a",))
    (directory / "graph.json").write_text(reduced.model_dump_json(by_alias=True))
    resources.provision(cfg, reduced)
    ctx.require(resources.containers == {"orc-g-nats", "orc-g-a"},
                "overwritten graph spec hid a removed agent container")
    ctx.require(resources.networks == {"orc-g-a"}, "removed agent network survived")
    ctx.require(resources.volumes == {"orc-g-a-ws"}, "removed agent workspace survived")
    ctx.require(not any(args[:3] == ("docker", "volume", "rm") and args[-1] == "orc-g-a-ws"
                        for args in resources.commands), "reaccept erased retained workspace")
    # Delete must also use the independent inventory rather than just the latest spec.
    replacement = _graph(agents=("c",))
    (directory / "graph.json").write_text(replacement.model_dump_json(by_alias=True))
    teardown(replacement, cfg.data_dir)
    ctx.require(not (resources.containers or resources.networks or resources.volumes),
                "teardown ignored prior provisioned identities")


async def fresh_failure_rollback(ctx):
    cfg = ctx.config()
    resources = Resources(ctx)
    _seed(ctx, state="pending")
    resources.fail = lambda args: args[:3] == ("docker", "run", "-d") and "orc-g-b" in args
    _failed(ctx, resources, cfg, _graph())
    ctx.require(not (resources.containers or resources.networks or resources.volumes),
                "fresh failed provision leaked Docker resources")


async def retained_failure_rollback(ctx):
    cfg = ctx.config()
    resources = Resources(ctx)
    directory = _seed(ctx)
    resources.provision(cfg, _graph())
    updated = _graph(agents=("a", "c"))
    (directory / "graph.json").write_text(updated.model_dump_json(by_alias=True))
    resources.fail = lambda args: args[:3] == ("docker", "run", "-d") and "orc-g-c" in args
    _failed(ctx, resources, cfg, updated)
    ctx.require(not resources.containers and not resources.networks,
                "failed reaccept left live containers or networks")
    ctx.require(resources.volumes == {"orc-g-a-ws"},
                "rollback must preserve retained workspace and remove new/removed workspaces")


async def early_failure_nonlive(ctx):
    cfg = ctx.config()
    resources = Resources(ctx)
    _seed(ctx)
    resources.provision(cfg, _graph())
    resources.fail = lambda args: args[:3] == ("docker", "image", "inspect")
    _failed(ctx, resources, cfg, _graph())
    ctx.require(not resources.containers and not resources.networks,
                "early failed reaccept left old containers or networks")
    ctx.require(resources.volumes == {"orc-g-a-ws", "orc-g-b-ws"},
                "early failure erased retained workspaces")


async def pending_delete_inventory(ctx):
    main, cfg = _main(ctx)
    resources = Resources(ctx)
    directory = _seed(ctx)
    resources.provision(cfg, _graph())
    # A pending status can coexist with resources after an interrupted accept.
    (directory / "status.json").write_text('{"state":"pending"}')
    (directory / "graph.json").write_text(_graph(agents=("a",)).model_dump_json(by_alias=True))
    async def noop(*args, **kwargs):
        return None
    ctx.patch("app.fabric.stop_observer", noop)
    async with _client(main.app, cfg.api_token) as client:
        response = await client.delete("/api/graphs/g")
    ctx.require(response.status_code == 200, "pending graph delete failed")
    ctx.require(not (resources.containers or resources.networks or resources.volumes),
                "pending graph delete discarded inventory without removing Docker resources")
    ctx.require(not directory.exists(), "delete left graph data behind")


async def bounded_agent_processes(ctx):
    cfg = ctx.config()
    resources = Resources(ctx)
    resources.provision(cfg, _graph())
    runs = [args for args in resources.commands
            if args[:3] == ("docker", "run", "-d") and args[-1] == cfg.agent_image]
    ctx.require(len(runs) == 2, "agent resource probe did not launch both agents")
    for args in runs:
        ctx.require("--pids-limit" in args, "agent has no process-count limit")
        limit = int(args[args.index("--pids-limit") + 1])
        ctx.require(1 <= limit <= 512, "agent process-count limit is not bounded")
        ctx.require("--tmpfs" not in args and "/workspace" in " ".join(args),
                    "process hardening replaced persistent workspace")


CASES = [
    ("review-provision", "broker credential files are private on creation and repair", credential_modes),
    ("review-provision", "resource inventory survives overwritten graph specifications", overwritten_spec_cleanup),
    ("review-provision", "fresh provision failure leaves no Docker resources", fresh_failure_rollback),
    ("review-provision", "failed reaccept preserves only retained workspace volumes", retained_failure_rollback),
    ("review-provision", "early failed reaccept clears live status and resources", early_failure_nonlive),
    ("review-provision", "pending API delete cleans independently inventoried resources", pending_delete_inventory),
    ("review-provision", "agents have bounded process counts and persistent workspaces", bounded_agent_processes),
]
