"""Offline probes for review-mid proxy request bounds."""
import json
import httpx
from evals.host_cases import _client, _seed


async def request_bounds(ctx):
    from app.proxy import create_proxy, graph_token
    cfg = ctx.config(graph_token_budget=100, graph_usd_budget=0)
    token = graph_token(_seed(ctx), cfg)
    forwarded = []
    def upstream(request):
        forwarded.append(json.loads(request.content))
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    async with _client(create_proxy(cfg, transport=httpx.MockTransport(upstream)), token) as client:
        response = await client.post('/llm/chat/completions', json={"messages": [{"role": "user", "content": "x" * 1048576}], "max_tokens": 1000000})
        ctx.require(response.status_code == 413 and not forwarded, 'oversized body reached upstream')
        for field in ('max_tokens', 'max_completion_tokens'):
            response = await client.post('/llm/chat/completions', json={"messages": [], field: 1000000})
            ctx.require(response.status_code == 200, 'bounded completion was rejected')
            ctx.require(forwarded[-1][field] <= 100 - 2 * (len(forwarded) - 1), 'completion exceeds remaining token allowance')


async def dollar_prices(ctx):
    from app.proxy import graph_token
    directory = _seed(ctx)
    try:
        graph_token(directory, ctx.config(graph_token_budget=0, graph_usd_budget=1, price_in_per_m=0, price_out_per_m=0))
    except ValueError:
        return
    ctx.require(False, 'enabled dollar budget accepts two zero prices')


CASES = [('mid', 'proxy_request_body_and_completion_bounds', request_bounds),
         ('mid', 'dollar_budget_requires_nonzero_price', dollar_prices)]
