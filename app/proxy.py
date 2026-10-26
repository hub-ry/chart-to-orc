"""Agent-only, non-streaming chat proxy. Run one worker to serialize graph spend."""
from __future__ import annotations

import asyncio
import hmac
import json
import math
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import Config
from app.schema import SLUG


def _save(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    temporary.replace(path)


def _initialize_guards(directory: Path, state: dict, cfg: Config) -> bool:
    """Migrate once; legacy attempts are unknown, but their original age is not."""
    defaults = {"max_calls": cfg.graph_max_calls, "max_seconds": cfg.graph_max_seconds,
                "calls": 0, "provisioned_at": None}
    missing = defaults.keys() - state.keys()
    if not missing:
        return False
    if (type(cfg.graph_max_calls) is not int or cfg.graph_max_calls <= 0
            or not math.isfinite(cfg.graph_max_seconds) or cfg.graph_max_seconds <= 0):
        raise ValueError("proxy call and runtime caps must be finite and positive")
    if "provisioned_at" in missing:
        try:
            started = json.loads((directory / "status.json").read_text())["provisioned_at"]
            if type(started) not in (int, float) or not math.isfinite(started) or started < 0:
                raise ValueError("invalid provisioning timestamp")
        except (OSError, ValueError, KeyError, TypeError):
            # Without an old timestamp, start one migration window, never a request window.
            started = time.time()
        defaults["provisioned_at"] = started
    state.update({key: defaults[key] for key in missing})
    return True


def graph_token(directory: Path, cfg: Config) -> str:
    """Keep credentials, caps and spend across re-provisioning and restarts."""
    path = directory / "proxy.json"
    if path.exists():
        path.chmod(0o600)
        state = json.loads(path.read_text())
        # Capture legacy status before provisioning replaces status.json.
        if _initialize_guards(directory, state, cfg):
            _save(path, state)
        return state["token"]
    if (cfg.graph_token_budget < 0 or cfg.graph_usd_budget < 0
            or not (cfg.graph_token_budget or cfg.graph_usd_budget)
            or not all(math.isfinite(v) and v >= 0 for v in
                       (cfg.graph_usd_budget, cfg.price_in_per_m, cfg.price_out_per_m))):
        raise ValueError("proxy budgets/prices must be finite and nonnegative; enable at least one cap")
    state = dict(token=secrets.token_urlsafe(32), token_budget=cfg.graph_token_budget,
                 usd_budget=cfg.graph_usd_budget, price_in_per_m=cfg.price_in_per_m,
                 price_out_per_m=cfg.price_out_per_m, tokens=0, usd=0.0, exhausted=False)
    _initialize_guards(directory, state, cfg)
    _save(path, state)
    return state["token"]


def create_proxy(cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    locks: dict[Path, asyncio.Lock] = {}

    @app.post("/{route}/chat/completions")
    async def completions(route: str, request: Request):
        authorization = request.headers.get("authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, "invalid token")
        path = None
        for directory in cfg.data_dir.iterdir() if cfg.data_dir.is_dir() else ():
            if not directory.is_dir() or not SLUG.fullmatch(directory.name):
                continue
            candidate = directory / "proxy.json"
            try:
                stored = json.loads(candidate.read_text())["token"]
            except (OSError, ValueError, KeyError):
                continue
            if hmac.compare_digest(token.encode(), stored.encode()):
                path = candidate
                break
        if path is None:
            raise HTTPException(401, "invalid token")
        if route not in ("llm", "vision"):
            raise HTTPException(404, "unknown route")
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "invalid JSON")
        if not isinstance(payload, dict) or payload.get("stream"):
            raise HTTPException(400, "expected a non-streaming chat request")
        # Never accept caller-selected routing, credentials, or expensive model overrides.
        payload = {key: value for key, value in payload.items() if key in (
            "messages", "tools", "tool_choice", "temperature", "top_p", "max_tokens",
            "max_completion_tokens", "response_format", "stop", "seed",
            "frequency_penalty", "presence_penalty", "parallel_tool_calls")}
        base = cfg.llm_base_url if route == "llm" else cfg.vision_base_url
        key = cfg.llm_api_key if route == "llm" else cfg.vision_api_key
        payload["model"] = cfg.llm_model if route == "llm" else cfg.vision_model
        payload["stream"] = False
        payload["n"] = 1
        async with locks.setdefault(path, asyncio.Lock()):
            state = json.loads(path.read_text())
            if _initialize_guards(path.parent, state, cfg):
                _save(path, state)
            # Evaluate admission after waiting for both routes' shared lock.
            if state["calls"] >= state["max_calls"]:
                return JSONResponse({"error": "graph call limit exhausted"}, status_code=429)
            if time.time() - state["provisioned_at"] > state["max_seconds"]:
                return JSONResponse({"error": "graph runtime limit exhausted"}, status_code=429)
            if (state["exhausted"] or
                    (state["token_budget"] and state["tokens"] >= state["token_budget"]) or
                    (state["usd_budget"] and state["usd"] >= state["usd_budget"])):
                return JSONResponse({"error": "graph budget exhausted"}, status_code=429)
            # Commit admission before I/O, including failed and interrupted upstream attempts.
            state["calls"] += 1
            _save(path, state)
            try:
                async with httpx.AsyncClient(timeout=120, transport=transport, trust_env=False) as client:
                    response = await client.post(base.rstrip("/") + "/chat/completions",
                                                 headers={"Authorization": f"Bearer {key}"}, json=payload)
            except httpx.HTTPError:
                raise HTTPException(502, "upstream unavailable")
            try:
                result = response.json()
                usage = result.get("usage")
                if usage is not None:
                    incoming, outgoing = usage["prompt_tokens"], usage["completion_tokens"]
                    if any(type(v) is not int or v < 0 for v in (incoming, outgoing)):
                        raise ValueError("invalid usage")
                    total = usage.get("total_tokens", incoming + outgoing)
                    if type(total) is not int or total < incoming + outgoing:
                        raise ValueError("invalid total usage")
                    state["tokens"] += total
                    state["usd"] += (incoming * state["price_in_per_m"] +
                                     outgoing * state["price_out_per_m"]) / 1_000_000
                    _save(path, state)
                elif response.is_success:
                    raise ValueError("missing usage")
            except (ValueError, KeyError, TypeError, AttributeError):
                # A successful but unmetered request must not permit unlimited spend.
                if response.is_success:
                    state["exhausted"] = True
                    _save(path, state)
                    raise HTTPException(502, "upstream returned invalid usage; graph budget closed")
            if not response.is_success:
                return JSONResponse({"error": response.text[:300]}, status_code=response.status_code)
            return JSONResponse(result)

    return app


class _ProxyServer(uvicorn.Server):
    # The outer uvicorn owns process signals; the child listener owns only its socket.
    def capture_signals(self):
        return nullcontext()

    def install_signal_handlers(self):
        pass


@asynccontextmanager
async def running_proxy(cfg: Config):
    server = _ProxyServer(uvicorn.Config(create_proxy(cfg), lifespan="off", access_log=False))
    with socket.create_server((cfg.proxy_host, cfg.proxy_port)) as listener:
        listener.setblocking(False)
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("proxy failed to start")
                await asyncio.sleep(0.01)
            yield
        finally:
            server.should_exit = True
            await task
