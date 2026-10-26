"""Config from env + ~/.config/chart-to-orc/env (chmod 600)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str
    port: int
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    vision_base_url: str
    vision_api_key: str
    vision_model: str
    agent_image: str
    nats_image: str
    proxy_host: str = "0.0.0.0"
    proxy_port: int = 8788
    graph_token_budget: int = 100_000
    graph_usd_budget: float = 1.0
    price_in_per_m: float = 1.0
    price_out_per_m: float = 3.0
    max_hops: int = 8
    graph_max_calls: int = 200
    graph_max_seconds: float = 1800
    api_token: str = ""
    allow_no_auth: bool = False
    graph_ttl_seconds: float = 0
    max_live_graphs: int = 5

    @classmethod
    def load(cls, extra: dict[str, str] | None = None) -> "Config":
        env = dict(os.environ)
        env.update(_load_env_file(Path.home() / ".config" / "chart-to-orc" / "env"))
        if extra:
            env.update(extra)

        def get(name: str, default: str = "") -> str:
            return env.get(name, default).strip()

        llm_base = get("ORC_LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
        llm_key = get("ORC_LLM_API_KEY") or get("DEEPSEEK_API_KEY")
        llm_model = get("ORC_LLM_MODEL", "deepseek-chat")
        return cls(
            data_dir=Path(get("ORC_DATA_DIR", str(Path.home() / "chart-to-orc" / "data"))).expanduser(),
            host=get("ORC_HOST", "127.0.0.1"),
            port=int(get("ORC_PORT", "8787")),
            llm_base_url=llm_base,
            llm_api_key=llm_key,
            llm_model=llm_model,
            vision_base_url=get("ORC_VISION_BASE_URL", llm_base).rstrip("/"),
            vision_api_key=get("ORC_VISION_API_KEY") or llm_key,
            vision_model=get("ORC_VISION_MODEL", "deepseek-flash"),
            agent_image=get("ORC_AGENT_IMAGE", "chart-orc/agent:1"),
            nats_image=get("ORC_NATS_IMAGE", "nats:2.10-alpine"),
            proxy_host=get("ORC_PROXY_HOST", "0.0.0.0"),
            proxy_port=int(get("ORC_PROXY_PORT", "8788")),
            graph_token_budget=int(get("ORC_GRAPH_TOKEN_BUDGET", "100000")),
            graph_usd_budget=float(get("ORC_GRAPH_USD_BUDGET", "1")),
            price_in_per_m=float(get("ORC_PRICE_IN_PER_M", "1")),
            price_out_per_m=float(get("ORC_PRICE_OUT_PER_M", "3")),
            max_hops=int(get("ORC_MAX_HOPS", "8")),
            graph_max_calls=int(get("ORC_GRAPH_MAX_CALLS", "200")),
            graph_max_seconds=float(get("ORC_GRAPH_MAX_SECONDS", "1800")),
            api_token=get("ORC_API_TOKEN"),
            allow_no_auth=get("ORC_ALLOW_NO_AUTH") == "1",
            graph_ttl_seconds=float(get("ORC_GRAPH_TTL_SECONDS", "0")),
            max_live_graphs=int(get("ORC_MAX_LIVE_GRAPHS", "5")),
        )
