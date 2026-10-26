"""agentd — a blind agent runtime.

One agent per container. Connects to NATS, subscribes only to its inbound
subjects (orc.<graph>.in.<id> and orc.<graph>.ch.<id> for channels where it is
`to`), runs one LLM turn per message, and publishes replies through its
outbound channels.

Environment (the interface):
  AGENT_ID, AGENT_NAME, GRAPH, SYSTEM, TOOLS, NATS_URL,
  OUT_CHANNELS, IN_CHANNELS (JSON), LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
  WORKSPACE (default /workspace).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import nats

KNOWN_KINDS = ("text", "artifact", "result")
USER_KINDS = ("text", "artifact", "result")
MAX_TOOL_CALLS = 8
HISTORY_MAX = 20
SHELL_TIMEOUT = 60
SHELL_CAPTURE = 8192

# ---- config (populated in main) ----
AGENT_ID = ""
AGENT_NAME = ""
GRAPH = ""
SYSTEM_PROMPT = ""
WORKSPACE = "/workspace"
LLM_BASE_URL = ""
LLM_API_KEY = ""
LLM_MODEL = ""
OUT_CHANNELS = []
IN_CHANNELS = []
OUT_BY_ID = {}
LIMITERS = {}

NC = None
HISTORY = deque()


def env(name, default=None):
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v


def log(event, subject, msg_id=None):
    line = {"event": event, "subject": subject}
    if msg_id is not None:
        line["msg_id"] = msg_id
    print(json.dumps(line), flush=True)


def log_llm_error(subject, error):
    print(json.dumps({"event": "llm_error", "subject": subject, "error": str(error)}), flush=True)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class RateLimiter:
    def __init__(self, rate_per_min):
        self.rate = float(rate_per_min)
        self.capacity = self.rate
        self.tokens = self.rate
        self.last = time.monotonic()

    def try_acquire(self):
        now = time.monotonic()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * (self.rate / 60.0))
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def build_system_prompt():
    parts = [f"You are agent {AGENT_NAME} (id {AGENT_ID}) in graph {GRAPH}."]
    if SYSTEM_PROMPT:
        parts.append(SYSTEM_PROMPT.strip())
    parts.append("You are reachable only through channels. Use the `send` tool to send messages.")
    if IN_CHANNELS:
        lines = ["Inbound channels (messages you receive):"]
        for c in IN_CHANNELS:
            lines.append(f"- channel {c.get('name')!r} (id {c.get('id')!r}) from {c.get('from')!r}")
        parts.append("\n".join(lines))
    lines = ["Outbound channels (channels you can send on):"]
    for c in OUT_CHANNELS:
        lines.append(
            f"- to send to {c.get('to')!r} use channel id {c.get('id')!r} "
            f"name {c.get('name')!r} (kinds {c.get('kinds')!r})"
        )
    lines.append("- to send to the user use channel id 'out' with to='user' (kinds text/artifact/result)")
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


def build_tools():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "send",
                "description": "Send a message to another agent or to the user.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Destination agent id, or 'user'."},
                        "channel_id": {"type": "string", "description": "An OUT_CHANNELS id, or 'out' when to='user'."},
                        "kind": {"type": "string", "enum": list(KNOWN_KINDS)},
                        "body": {"type": "object", "description": "Message body (JSON object)."},
                    },
                    "required": ["to", "channel_id", "kind", "body"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file inside the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": "Write a file inside the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "List files in the workspace.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "shell",
                "description": "Run a shell command in the workspace.",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            },
        },
    ]
    return tools


def resolve_path(path):
    ws = os.path.realpath(WORKSPACE)
    p = os.path.realpath(os.path.join(ws, path.lstrip("/") if path else "."))
    if p != ws and not p.startswith(ws + os.sep):
        raise ValueError(f"path escapes workspace: {path!r}")
    return p


def tool_result(payload):
    return json.dumps(payload, ensure_ascii=False)


def err(msg):
    return tool_result({"ok": False, "error": msg})


def ok(msg):
    return tool_result({"ok": True, "result": msg})


async def tool_send(args, sent):
    to = args.get("to")
    channel_id = args.get("channel_id")
    kind = args.get("kind")
    body = args.get("body")
    if kind not in KNOWN_KINDS:
        return err(f"invalid kind {kind!r}; want one of {list(KNOWN_KINDS)}")
    if not isinstance(body, (dict, list, str, int, float, bool, type(None))):
        return err("body is not JSON-serializable")

    max_bytes = None
    limiter = None
    if to == "user":
        if channel_id != "out":
            return err("for to='user', channel_id must be 'out'")
        subject = f"out.{AGENT_ID}"
        ch_field = f"out.{AGENT_ID}"
        if kind not in USER_KINDS:
            return err(f"invalid kind {kind!r} for user")
        sent["user"] = True
    else:
        ch = OUT_BY_ID.get(channel_id)
        if ch is None:
            return err(f"unknown channel_id {channel_id!r}; want one of {list(OUT_BY_ID)}")
        if to != ch["to"]:
            return err(f"to {to!r} does not match channel {channel_id!r} (to={ch['to']!r})")
        if kind not in ch["kinds"]:
            return err(f"kind {kind!r} not allowed on channel {channel_id!r} (kinds={ch['kinds']!r})")
        subject = f"ch.{ch['id']}"
        ch_field = f"ch.{ch['id']}"
        max_bytes = ch.get("max_bytes")
        limiter = LIMITERS.get(ch["id"])

    envelope = {
        "v": 1,
        "msg_id": str(uuid.uuid4()),
        "ts": now_iso(),
        "src": AGENT_ID,
        "ch": ch_field,
        "kind": kind,
        "body": body,
        "prev": None,
    }
    data = json.dumps(envelope, ensure_ascii=False).encode()
    if max_bytes is not None and len(data) > max_bytes:
        return err(f"message too large ({len(data)} > {max_bytes} bytes)")

    if limiter is not None and not limiter.try_acquire():
        return err(f"rate limit exceeded on channel {channel_id!r} (rate_per_min={ch['rate_per_min']}); send dropped")

    full_subject = f"orc.{GRAPH}.{subject}"
    await NC.publish(full_subject, data)
    sent["any"] = True
    log("sent", full_subject, envelope["msg_id"])
    return ok(f"sent on {full_subject}")


def tool_read_file(args):
    path = args.get("path", "")
    try:
        p = resolve_path(path)
    except ValueError as e:
        return err(str(e))
    try:
        with open(p, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return err(f"file not found: {path!r}")
    except IsADirectoryError:
        return err(f"is a directory: {path!r}")
    except Exception as e:
        return err(str(e))
    return ok(content)


def tool_write_file(args):
    path = args.get("path", "")
    content = args.get("content", "")
    try:
        p = resolve_path(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
    except ValueError as e:
        return err(str(e))
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        return err(str(e))
    return ok(f"wrote {len(content)} bytes to {path!r}")


def tool_list_files(args):
    ws = os.path.realpath(WORKSPACE)
    try:
        names = sorted(os.listdir(ws))
    except FileNotFoundError:
        return ok([])
    except Exception as e:
        return err(str(e))
    return ok(names)


async def tool_shell(args):
    cmd = args.get("cmd", "")
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=WORKSPACE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, errb = await asyncio.wait_for(proc.communicate(), timeout=SHELL_TIMEOUT)
    except asyncio.TimeoutError:
        return err(f"shell command timed out after {SHELL_TIMEOUT}s")
    except Exception as e:
        return err(str(e))
    result = {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", "replace")[:SHELL_CAPTURE],
        "stderr": errb.decode("utf-8", "replace")[:SHELL_CAPTURE],
    }
    return tool_result(result)


async def execute_tool(name, args, sent):
    if name == "send":
        return await tool_send(args, sent)
    if name == "read_file":
        return tool_read_file(args)
    if name == "write_file":
        return tool_write_file(args)
    if name == "list_files":
        return tool_list_files(args)
    if name == "shell":
        return await tool_shell(args)
    return err(f"unknown tool {name!r}")


async def llm_chat(messages, tools):
    url = LLM_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"model": LLM_MODEL, "messages": messages, "tools": tools}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()


def format_incoming(envelope):
    src = envelope.get("src")
    ch = envelope.get("ch")
    kind = envelope.get("kind")
    body = envelope.get("body")
    return (
        f"Message received on channel {ch!r} from {src!r} (kind {kind!r}):\n"
        f"{json.dumps(body, ensure_ascii=False)}"
    )


def trim_history():
    while len(HISTORY) > HISTORY_MAX:
        HISTORY.popleft()


def publish_out(text, prev_msg_id):
    envelope = {
        "v": 1,
        "msg_id": str(uuid.uuid4()),
        "ts": now_iso(),
        "src": AGENT_ID,
        "ch": f"out.{AGENT_ID}",
        "kind": "text",
        "body": {"text": text},
        "prev": prev_msg_id,
    }
    subject = f"orc.{GRAPH}.out.{AGENT_ID}"
    asyncio.ensure_future(NC.publish(subject, json.dumps(envelope, ensure_ascii=False).encode()))
    log("sent", subject, envelope["msg_id"])


async def handle_envelope(envelope, subject, trigger_is_in):
    HISTORY.append({"role": "user", "content": format_incoming(envelope)})
    trim_history()
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(HISTORY)

    tools = build_tools()
    sent = {"user": False, "any": False}
    final_text = ""
    try:
        for _ in range(MAX_TOOL_CALLS):
            resp = await llm_chat(messages, tools)
            choice = resp["choices"][0]
            msg = choice["message"]
            content = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                assistant_msg = {"role": "assistant", "content": content or None, "tool_calls": tool_calls}
                messages.append(assistant_msg)
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    result = await execute_tool(name, args, sent)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.get("id", ""), "content": result}
                    )
            else:
                final_text = content
                messages.append({"role": "assistant", "content": content})
                HISTORY.append({"role": "assistant", "content": content})
                trim_history()
                break
    except Exception as e:
        log_llm_error(subject, e)
        return

    if not sent["any"] and final_text.strip():
        # The model answered in plain text without using send. Route the reply
        # to the operator rather than dropping it silently.
        publish_out(final_text, envelope.get("msg_id"))


def validate_envelope(data):
    try:
        envelope = json.loads(data)
    except Exception:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("v") != 1:
        return None
    if not isinstance(envelope.get("src"), str):
        return None
    if not isinstance(envelope.get("ch"), str):
        return None
    if envelope.get("kind") not in KNOWN_KINDS:
        return None
    if not isinstance(envelope.get("body"), dict):
        return None
    return envelope


def make_handler(subject, channel, trigger_is_in):
    async def handler(msg):
        envelope = validate_envelope(msg.data)
        if envelope is None:
            return
        if channel is not None:
            if envelope["kind"] not in channel["kinds"]:
                return
            if len(msg.data) > channel.get("max_bytes", 1 << 30):
                return
        log("received", subject, envelope.get("msg_id"))
        await handle_envelope(envelope, subject, trigger_is_in)

    return handler


async def connect_nats(nats_url):
    u = urlparse(nats_url)
    host = u.hostname or "127.0.0.1"
    port = u.port or 4222
    try:
        nc = await nats.connect(
            f"nats://{host}:{port}",
            user=u.username,
            password=u.password,
            allow_reconnect=False,
            max_reconnect_attempts=0,
            connect_timeout=5,
        )
        return nc
    except Exception as e:
        print(
            json.dumps({"event": "connect_error", "error": f"could not connect to NATS at {nats_url}: {e}"}),
            flush=True,
        )
        return None


async def main():
    global AGENT_ID, AGENT_NAME, GRAPH, SYSTEM_PROMPT, WORKSPACE
    global LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, OUT_CHANNELS, IN_CHANNELS
    global OUT_BY_ID, LIMITERS, NC

    NATS_URL = env("NATS_URL")
    if not NATS_URL:
        print(
            json.dumps({"event": "connect_error", "error": "NATS_URL is not set; cannot connect"}),
            flush=True,
        )
        sys.exit(1)

    AGENT_ID = env("AGENT_ID", "")
    AGENT_NAME = env("AGENT_NAME", AGENT_ID)
    GRAPH = env("GRAPH", "graph")
    WORKSPACE = env("WORKSPACE", "/workspace")
    LLM_BASE_URL = env("LLM_BASE_URL", "")
    LLM_API_KEY = env("LLM_API_KEY", "")
    LLM_MODEL = env("LLM_MODEL", "")
    SYSTEM_PROMPT = env("SYSTEM", "")

    try:
        OUT_CHANNELS = json.loads(env("OUT_CHANNELS", "[]"))
        IN_CHANNELS = json.loads(env("IN_CHANNELS", "[]"))
    except Exception as e:
        print(json.dumps({"event": "config_error", "error": f"bad channel JSON: {e}"}), flush=True)
        sys.exit(1)

    tools_list = [t for t in env("TOOLS", "workspace").split(",") if t]

    OUT_BY_ID = {c["id"]: c for c in OUT_CHANNELS}
    LIMITERS = {c["id"]: RateLimiter(c.get("rate_per_min", 30)) for c in OUT_CHANNELS}

    SYSTEM_PROMPT = build_system_prompt()

    NC = await connect_nats(NATS_URL)
    if NC is None:
        sys.exit(1)

    in_subject = f"orc.{GRAPH}.in.{AGENT_ID}"
    await NC.subscribe(in_subject, cb=make_handler(in_subject, None, True))

    for c in IN_CHANNELS:
        subject = f"orc.{GRAPH}.ch.{c['id']}"
        await NC.subscribe(subject, cb=make_handler(subject, c, False))

    log("ready", f"orc.{GRAPH}.*")
    try:
        await asyncio.Future()
    finally:
        await NC.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
