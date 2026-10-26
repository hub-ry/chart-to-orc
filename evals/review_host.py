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


CASES = [('mid', 'invalid_saved_graph_does_not_abort_startup', invalid_resume),
         ('mid', 'observer_traffic_and_markdown_byte_caps', bounded_logs)]
