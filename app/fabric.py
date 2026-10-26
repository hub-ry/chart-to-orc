"""Observer fabric: the app's user-account connection per live graph.

The app holds the only eye-of-god account (publish in.>, subscribe >.>) and
persists every observed message to data/<g>/traffic.jsonl (capped, tail-kept).
Blindness applies between agents; the operator sees the filtered channel traffic —
that is the observability surface, not a state store.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import nats

from app.schema import SLUG

TRAFFIC_CAP_LINES = 5000
MAX_LOG_BYTES = 4_000_000

_connections: dict[str, object] = {}
_observers: dict[str, asyncio.Task] = {}


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
        data = json.dumps(obj, ensure_ascii=False)
        try:
            with log.open("a") as f:
                f.write(data + "\n")
            if log.stat().st_size > MAX_LOG_BYTES:
                lines = log.read_text().splitlines()[-TRAFFIC_CAP_LINES:]
                log.write_text("\n".join(lines) + "\n")
            line = _md_line(obj)
            if line and isinstance(src, str) and SLUG.fullmatch(src) and md_dir.is_dir():
                p = md_dir / f"{obj['src']}.md"
                with p.open("a") as f:
                    f.write(line)
        except Exception:
            import traceback

            traceback.print_exc()


async def stop_observer(name: str) -> None:
    task = _observers.pop(name, None)
    if task:
        task.cancel()
    conn = _connections.pop(name, None)
    if conn:
        try:
            await conn.close()
        except Exception:
            pass
