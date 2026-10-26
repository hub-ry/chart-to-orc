"""Offline probes for review-mid proxy request bounds."""
import json
import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.config import Config, _load_env_file
import httpx
from evals.host_cases import _client, _seed

_CONFIG_LOAD = Config.load.__func__
_ENV_FILE_LOAD = _load_env_file


@contextmanager
def _config_fixture(ctx, values, mode=0o600):
    home = ctx.path / "config-home"
    env_file = home / ".config" / "chart-to-orc" / "env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if env_file.exists():
        env_file.chmod(0o600)
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    env_file.chmod(mode)
    with patch.object(Path, "home", return_value=home), \
            patch("app.config._load_env_file", _ENV_FILE_LOAD), \
            patch.dict(os.environ, {}, clear=True):
        yield env_file


def _routing_env(source):
    return {
        "ORC_LLM_BASE_URL": f"https://{source}-text.invalid/v1",
        "ORC_LLM_API_KEY": f"fake-{source}-text-key",
        "ORC_VISION_BASE_URL": f"https://{source}-vision.invalid/v1",
        "ORC_VISION_API_KEY": f"fake-{source}-vision-key",
        "ORC_API_TOKEN": f"fake-{source}-api-token",
    }


def _require_routing(ctx, cfg, expected):
    for field, key in (
        ("llm_base_url", "ORC_LLM_BASE_URL"),
        ("llm_api_key", "ORC_LLM_API_KEY"),
        ("vision_base_url", "ORC_VISION_BASE_URL"),
        ("vision_api_key", "ORC_VISION_API_KEY"),
        ("api_token", "ORC_API_TOKEN"),
    ):
        ctx.require(getattr(cfg, field) == expected[key], f"wrong config precedence for {key}")


async def process_overrides_home_config(ctx):
    with _config_fixture(ctx, _routing_env("home")):
        process = _routing_env("process")
        os.environ.update(process)
        _require_routing(ctx, _CONFIG_LOAD(Config), process)


async def explicit_overrides_process_config(ctx):
    with _config_fixture(ctx, _routing_env("home")):
        os.environ.update(_routing_env("process"))
        explicit = _routing_env("explicit")
        _require_routing(ctx, _CONFIG_LOAD(Config, explicit), explicit)


async def public_config_refused_before_read(ctx):
    for mode in (0o640, 0o604, 0o610, 0o601, 0o644, 0o666):
        with _config_fixture(ctx, _routing_env("home"), mode):
            with patch.object(Path, "read_text", side_effect=AssertionError("public env file was read")):
                try:
                    _CONFIG_LOAD(Config)
                except PermissionError:
                    continue
            ctx.require(False, f"public env file mode {mode:o} was accepted")


async def private_config_modes_load(ctx):
    for mode in (0o400, 0o600):
        home = _routing_env("home")
        with _config_fixture(ctx, home, mode):
            _require_routing(ctx, _CONFIG_LOAD(Config), home)



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
CASES += [
    ('end', 'config_process_overrides_conflicting_home', process_overrides_home_config),
    ('end', 'config_explicit_overrides_conflicting_process', explicit_overrides_process_config),
    ('end', 'config_public_env_refused_before_read', public_config_refused_before_read),
    ('end', 'config_private_read_only_and_writable_env_load', private_config_modes_load),
]
