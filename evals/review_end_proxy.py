"""Offline proxy startup guards and ambiguous upstream accounting."""
import fcntl
import json

import httpx

from evals.host_cases import _client, _seed


async def ambiguous_upstream_closes_budget(ctx):
    from app.proxy import create_proxy, graph_token
    cfg = ctx.config()
    directory = _seed(ctx)
    token = graph_token(directory, cfg)
    calls = []

    def upstream(request):
        calls.append(request)
        raise httpx.ReadTimeout("fixture response lost after dispatch", request=request)

    def app():
        return create_proxy(cfg, transport=httpx.MockTransport(upstream))

    async with _client(app(), token) as client:
        response = await client.post('/llm/chat/completions', json={})
        ctx.require(response.status_code == 502, 'ambiguous attempt must fail with 502')
    async with _client(app(), token) as client:
        response = await client.post('/vision/chat/completions', json={})
    ctx.require(response.status_code == 429 and response.json().get('error') == 'graph budget exhausted',
                'lost usage permits another potentially billable request after restart')
    state = json.loads((directory / 'proxy.json').read_text())
    ctx.require(state['calls'] == 1 and len(calls) == 1 and state['exhausted'],
                'ambiguous dispatch must persist budget closure, not fabricated usage')


async def explicit_proxy_bind_required(ctx):
    from app.proxy import running_proxy
    cfg = ctx.config()
    ctx.require(cfg.proxy_host != '0.0.0.0', 'default proxy bind exposes every host interface')
    try:
        async with running_proxy(ctx.config(proxy_host='')):
            ctx.require(False, 'empty proxy bind starts a listener')
    except ValueError:
        return
    ctx.require(False, 'empty proxy bind was accepted')


async def concurrent_proxy_owner_refused(ctx):
    from app.proxy import running_proxy
    cfg = ctx.config(proxy_host='127.0.0.1')
    with (ctx.path / '.proxy.lock').open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Any attempt to bind instead of rejecting ownership fails this offline probe.
        ctx.patch('socket.create_server', lambda *args, **kwargs: ctx.require(False, 'second owner reached socket bind'))
        try:
            async with running_proxy(cfg):
                ctx.require(False, 'second proxy owner started')
        except RuntimeError as exc:
            ctx.require('one' in str(exc).lower() and 'worker' in str(exc).lower(),
                        'ownership error must explain single-worker requirement')
            return
    ctx.require(False, 'concurrent proxy owner was accepted')


CASES = [
    ('end', 'ambiguous_upstream_failure_closes_persisted_budget', ambiguous_upstream_closes_budget),
    ('end', 'proxy_requires_explicit_bind_address', explicit_proxy_bind_required),
    ('end', 'proxy_rejects_concurrent_worker_ownership', concurrent_proxy_owner_refused),
]
