"""Offline startup and observer byte-cap regressions."""
import json
from contextlib import asynccontextmanager
from evals.host_cases import _main, _seed, _graph, _client, _observe


async def invalid_resume(ctx):
    main, cfg = _main(ctx)
    bad = _seed(ctx, _graph('bad'))
    value = json.loads((bad / 'graph.json').read_text())
    value['agents'][0]['tools'] = ['nope']
    (bad / 'graph.json').write_text(json.dumps(value))
    _seed(ctx, _graph('good'))
    observed = []
    @asynccontextmanager
    async def no_listener(cfg):
        yield
    async def observe(directory, name, channels):
        observed.append(name)
    main.running_proxy = no_listener
    ctx.patch('app.fabric.start_observer', observe)
    try:
        async with main.lifespan(main.app):
            async with _client(main.app) as client:
                response = await client.get('/health')
                ctx.require(response.status_code == 200, 'invalid graph disables health')
    except ValueError:
        ctx.require(False, 'invalid saved graph aborts lifespan')
    ctx.require(observed == ['good'], 'valid sibling did not resume')


async def bounded_logs(ctx):
    from app import fabric
    main, cfg = _main(ctx)
    directory = _seed(ctx)
    messages = [('orc.g.out.a', {'kind': 'text', 'body': {'text': 'x' * 300000}, 'ts': 'eval'}) for _ in range(20)]
    await _observe(main, directory, messages)
    ctx.require((directory / 'traffic.jsonl').stat().st_size <= fabric.MAX_LOG_BYTES, 'traffic exceeds byte cap')
    ctx.require((directory / 'agents' / 'a.md').stat().st_size <= fabric.MAX_LOG_BYTES, 'markdown exceeds byte cap')
    rows = [json.loads(line) for line in (directory / 'traffic.jsonl').read_text().splitlines()]
    ctx.require(rows[-1]['body']['text'] == 'x' * 300000, 'latest bounded traffic row missing')


async def observer_failure_rollback(ctx):
    from evals.host_cases import _capture_provision
    main, cfg = _main(ctx)
    directory = _seed(ctx, state="pending")
    commands = _capture_provision(ctx, main)

    async def fail(*args):
        raise RuntimeError("offline broker refusal")

    ctx.patch("app.fabric.start_observer", fail)
    async with _client(main.app, cfg.api_token) as client:
        response = await client.post("/api/graphs/g/accept")
    ctx.require(response.status_code == 503, "observer failure must return 503")
    ctx.require(json.loads((directory / "status.json").read_text())["state"] == "failed",
                "observer failure leaves graph live")
    last_run = max(i for i, args in enumerate(commands) if args[:3] == ("docker", "run", "-d"))
    cleanup = commands[last_run + 1:]
    ctx.require(any(args[:3] == ("docker", "rm", "-f") for args in cleanup)
                and any(args[:3] == ("docker", "volume", "rm") for args in cleanup),
                "observer failure leaves provisioned resources running")


async def abandoned_provision_cleanup(ctx):
    import os
    import time
    from evals.host_cases import _capture_provision
    main, cfg = _main(ctx, graph_ttl_seconds=0)
    old = _seed(ctx, _graph("old"), state="provisioning")
    fresh = _seed(ctx, _graph("fresh"), state="provisioning")
    status = old / "status.json"
    status.write_text(json.dumps({"state": "provisioning"}))
    os.utime(status, (time.time() - 600, time.time() - 600))
    commands = _capture_provision(ctx, main)
    await main.cleanup_expired_graphs(cfg)
    ctx.require(json.loads(status.read_text())["state"] == "failed",
                "abandoned provisioning is never collected when TTL is disabled")
    ctx.require(json.loads((fresh / "status.json").read_text())["state"] == "provisioning",
                "fresh provisioning was collected")
    ctx.require(any("orc-old-a" in args for args in commands)
                and not any("orc-fresh-a" in args for args in commands),
                "abandoned provisioning cleanup targets wrong resources")

CASES = [('mid', 'invalid_saved_graph_does_not_abort_startup', invalid_resume),
         ('mid', 'observer_traffic_and_markdown_byte_caps', bounded_logs),
         ('end', 'observer_failure_rolls_back_resources', observer_failure_rollback),
         ('end', 'abandoned_provisioning_is_collected_without_live_ttl', abandoned_provision_cleanup)]
