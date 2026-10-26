"""Observer fabric: the app's user-account connection per live graph.

The app holds the only eye-of-god account (publish in.>, subscribe >.>) and
persists every observed message to data/<g>/traffic.jsonl (capped, tail-kept).
Blindness applies between agents; the operator sees the filtered channel traffic —
that is the observability surface, not a state store.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import nats

from app.schema import SLUG, result_error

MAX_LOG_BYTES = 4_000_000


def append_bounded(path: Path, text: str) -> None:
    """Keep complete recent lines, never reading more than the storage byte cap."""
    data = text.encode("utf-8")
    if len(data) > MAX_LOG_BYTES:
        return
    with path.open("r+b" if path.exists() else "w+b") as file:
        file.seek(0, 2)
        size = file.tell()
        if size + len(data) <= MAX_LOG_BYTES:
            file.write(data)
            return
        keep = MAX_LOG_BYTES - len(data)
        start = max(0, size - keep)
        file.seek(start)
        tail = file.read(keep)
        if start:
            newline = tail.find(b"\n")
            tail = tail[newline + 1:] if newline >= 0 else b""
        file.seek(0)
        file.write(tail)
        file.write(data)
        file.truncate()

_connections: dict[str, object] = {}
_observers: dict[str, asyncio.Task] = {}


@dataclass
class ResultState:
    results: deque = field(default_factory=lambda: deque(maxlen=100))
    changed: asyncio.Event = field(default_factory=asyncio.Event)


_results: dict[str, ResultState] = {}


def observe_result(name: str, obj: dict, schema: dict | None) -> None:
    """Called only for broker-observed results on a user-facing out subject."""
    error = result_error(obj.get("body"), schema) if schema is not None else None
    obj["valid"] = error is None
    obj.pop("error", None)
    if error:
        obj["error"] = error
    state = _results.setdefault(name, ResultState())
    state.results.append(obj)
    state.changed.set()
    state.changed = asyncio.Event()


async def wait_result(name: str, conv: str | None, timeout: float) -> dict:
    state = _results.setdefault(name, ResultState())
    for obj in reversed(state.results):
        if conv is None or obj.get("conv") == conv:
            return obj
    async def matching():
        while True:
            for obj in reversed(state.results):
                if conv is None or obj.get("conv") == conv:
                    return obj
            await state.changed.wait()
    return await asyncio.wait_for(matching(), timeout)


def _user_url(graph_dir: Path) -> str | None:
    p = graph_dir / "user.env"
    if not p.is_file():
        return None
    for line in p.read_text().splitlines():
        if line.startswith("NATS_URL="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


async def get_conn(graph_dir: Path, name: str):
    if name in _connections:
        return _connections[name]
    url = _user_url(graph_dir)
    if not url:
        raise RuntimeError("graph has no observer credentials (not live?)")
    try:
        nc = await nats.connect(url, connect_timeout=5, max_reconnect_attempts=0)
    except Exception as e:
        raise RuntimeError(f"nats unreachable: {e}") from e
    _connections[name] = nc
    return nc


async def start_observer(graph_dir: Path, name: str, channel_senders: dict[str, str]) -> None:
    if name in _observers:
        return
    nc = await get_conn(graph_dir, name)
    sub = await nc.subscribe(f"orc.{name}.>")
    _observers[name] = asyncio.create_task(_persist(sub, graph_dir, name, channel_senders))


def _md_line(msg: dict) -> str | None:
    src = msg.get("src")
    if not src or src == "user" or msg.get("kind") != "text":
        return None
    body = msg.get("body") or {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return f"### {msg.get('ts', '?')} {src} via {msg.get('ch', '?')}\n{text.strip()}\n\n"


async def _persist(sub, graph_dir: Path, name: str, channel_senders: dict[str, str]) -> None:
    log = graph_dir / "traffic.jsonl"
    md_dir = graph_dir / "agents"
    spec_path = graph_dir / "graph.json"
    schema = json.loads(spec_path.read_text()).get("result_schema") if spec_path.is_file() else None
    while True:
        msg = await sub.next_msg(timeout=None)
        try:
            data = msg.data.decode()
            obj = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        parts = msg.subject.split(".")
        src = None
        if len(parts) == 4 and parts[:2] == ["orc", name]:
            if parts[2] == "out":
                src = parts[3]
            elif parts[2] == "ch":
                src = channel_senders.get(parts[3])
            elif parts[2] == "in":
                src = "user"
        obj["src"] = src
        if (len(parts) == 4 and parts[:3] == ["orc", name, "out"]
                and SLUG.fullmatch(parts[3]) and obj.get("kind") == "result"):
            observe_result(name, obj, schema)
        data = json.dumps(obj, ensure_ascii=False)
        try:
            append_bounded(log, data + "\n")
            line = _md_line(obj)
            if line and isinstance(src, str) and SLUG.fullmatch(src) and md_dir.is_dir():
                p = md_dir / f"{obj['src']}.md"
                append_bounded(p, line)
        except Exception:
            import traceback

            traceback.print_exc()


async def stop_observer(name: str) -> None:
    state = _results.pop(name, None)
    if state:
        state.results.clear()
    task = _observers.pop(name, None)
    if task:
        task.cancel()
    conn = _connections.pop(name, None)
    if conn:
        try:
            await conn.close()
        except Exception:
            pass
