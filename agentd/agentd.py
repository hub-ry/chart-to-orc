"""agentd — a blind agent runtime.

One agent per container. Connects to NATS, subscribes only to its inbound
subjects (orc.<graph>.in.<id> and orc.<graph>.ch.<id> for channels where it is
`to`), runs one LLM turn per message, and publishes replies through its
outbound channels.

Environment (the interface):
  AGENT_ID, AGENT_NAME, GRAPH, SYSTEM, TOOLS, NATS_URL,
  OUT_CHANNELS, IN_CHANNELS (JSON), LLM_BASE_URL, LLM_API_KEY, LLM_MODEL,
  WORKSPACE (default /workspace), MAX_HOPS (default 8).
"""

from __future__ import annotations

import asyncio
import codecs
import concurrent.futures
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
import nats

KNOWN_KINDS = ("text", "artifact", "result")
USER_KINDS = ("text", "artifact", "result")
MAX_TOOL_CALLS = 8
HISTORY_MAX = 20
CONVERSATIONS_MAX = 50
MAX_HOPS = 8
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
TOOLS_LIST = []
VISION_BASE_URL = ""
VISION_API_KEY = ""
VISION_MODEL = ""

NC = None
HISTORY = OrderedDict()


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
    ]
    if "workspace" in TOOLS_LIST:
        tools += [
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
    ]
    if "shell" in TOOLS_LIST:
        tools += [
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
    if "browser" in TOOLS_LIST:
        tools += [
            {
                "type": "function",
                "function": {
                    "name": "browser_goto",
                    "description": "Navigate the browser to a URL.",
                    "parameters": {
                        "type": "object",
                        "properties": {"url": {"type": "string"}},
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_content",
                    "description": "Read the visible text and links of the current page.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_click",
                    "description": "Click an element by CSS selector or visible text.",
                    "parameters": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_fill",
                    "description": "Type text into an input by CSS selector.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "selector": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["selector", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "browser_screenshot",
                    "description": "Save a screenshot to the workspace and describe it visually.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
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
    if sent["hops"] > MAX_HOPS:
        return err(f"hop limit exceeded (MAX_HOPS={MAX_HOPS}); send refused")
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
        "prev": sent["prev"],
        "conv": sent["conv"],
        "hops": sent["hops"],
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
            start_new_session=True,
        )
        async def capture(stream):
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            parts = []
            remaining = SHELL_CAPTURE
            while chunk := await stream.read(8192):
                if remaining > 0:
                    text = decoder.decode(chunk)[:remaining]
                    parts.append(text)
                    remaining -= len(text)
            if remaining > 0:
                parts.append(decoder.decode(b"", final=True)[:remaining])
            return "".join(parts)

        # Keep draining both pipes after retaining their bounded prefixes so
        # verbose commands cannot deadlock or buffer unbounded output in memory.
        completion = asyncio.gather(capture(proc.stdout), capture(proc.stderr), proc.wait())
        out, errb, _ = await asyncio.wait_for(asyncio.shield(completion), timeout=SHELL_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(completion), timeout=0.25)
        except asyncio.TimeoutError:
            # A descendant can escape the group and retain a pipe. Bound the
            # reap grace, then release our pipes without claiming containment.
            completion.cancel()
            proc._transport.close()
            await asyncio.gather(completion, return_exceptions=True)
        return err(f"shell command timed out after {SHELL_TIMEOUT}s")
    except Exception as e:
        return err(str(e))
    result = {
        "exit_code": proc.returncode,
        "stdout": out,
        "stderr": errb,
    }
    return tool_result(result)


# ---- browser (Playwright; one instance per agent, inside this container) ----
_browser_queue: "queue.Queue" = queue.Queue()
_browser_thread = None


def _browser_worker():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()
        while True:
            fut, fn, args = _browser_queue.get()
            if fn is None:
                break
            try:
                fut.set_result(fn(page, *args))
            except Exception as e:
                fut.set_exception(e)
        browser.close()


def _browser_call(fn, *args):
    global _browser_thread
    if _browser_thread is None:
        _browser_thread = threading.Thread(target=_browser_worker, daemon=True)
        _browser_thread.start()
    fut = concurrent.futures.Future()
    _browser_queue.put((fut, fn, args))
    return asyncio.wrap_future(fut)


async def tool_browser_goto(args):
    url = str(args.get("url", ""))
    if not url.startswith(("http://", "https://")):
        return err("url must start with http:// or https://")

    def op(page, url):
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        return page.title(), page.url

    try:
        title, final = await _browser_call(op, url)
        return ok(f"navigated to {final} — title: {title!r}")
    except Exception as e:
        return err(f"browser_goto failed: {e}")


async def tool_browser_content(args):
    def op(page):
        text = page.inner_text("body")[:8192]
        links = [
            f"{a.inner_text().strip()[:60]} → {a.get_attribute('href')}"
            for a in page.query_selector_all("a[href]")[:60]
        ]
        return {"text": text, "links": links}

    try:
        data = await _browser_call(op)
        return ok(json.dumps(data, ensure_ascii=False)[:9000])
    except Exception as e:
        return err(f"browser_content failed: {e}")


async def tool_browser_click(args):
    target = str(args.get("target", ""))
    if not target:
        return err("target is required (CSS selector or visible text)")

    def op(page, target):
        if target.startswith(("#", ".", "[")):
            page.locator(target).first.click(timeout=15000)
        else:
            page.get_by_text(target, exact=False).first.click(timeout=15000)
        return page.url

    try:
        final = await _browser_call(op, target)
        return ok(f"clicked; now at {final}")
    except Exception as e:
        return err(f"browser_click failed: {e}")


async def tool_browser_fill(args):
    selector = str(args.get("selector", ""))
    text = str(args.get("text", ""))
    if not selector:
        return err("selector is required")

    def op(page, selector, text):
        page.fill(selector, text, timeout=15000)
        return True

    try:
        await _browser_call(op, selector, text)
        return ok("filled")
    except Exception as e:
        return err(f"browser_fill failed: {e}")


async def tool_browser_screenshot(args):
    path = str(args.get("path", "shot.png"))
    try:
        target = resolve_path(path)
    except ValueError as e:
        return err(str(e))

    def op(page, target):
        page.screenshot(path=target)
        return True

    try:
        await _browser_call(op, target)
    except Exception as e:
        return err(f"browser_screenshot failed: {e}")
    if VISION_API_KEY:
        try:
            import base64

            with open(target, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            desc = await vision_describe(
                b64, "Describe what is visible in this browser screenshot."
            )
            return ok(f"saved {path}; vision: {desc}")
        except Exception as e:
            return ok(f"saved {path} (vision describe failed: {e})")
    return ok(f"saved {path}")


async def vision_describe(image_b64: str, prompt: str) -> str:
    url = VISION_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    headers = {
        "Authorization": f"Bearer {VISION_API_KEY}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]


async def execute_tool(name, args, sent):
    if name in {"read_file", "write_file", "list_files"} and "workspace" not in TOOLS_LIST:
        return err("workspace tools are disabled")
    if name == "shell" and "shell" not in TOOLS_LIST:
        return err("shell tool is disabled")
    if name.startswith("browser_") and "browser" not in TOOLS_LIST:
        return err("browser tools are disabled")
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
    if name == "browser_goto":
        return await tool_browser_goto(args)
    if name == "browser_content":
        return await tool_browser_content(args)
    if name == "browser_click":
        return await tool_browser_click(args)
    if name == "browser_fill":
        return await tool_browser_fill(args)
    if name == "browser_screenshot":
        return await tool_browser_screenshot(args)
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


async def handle_envelope(envelope, subject, trigger_is_in):
    hops = envelope.get("hops", 0)
    if type(hops) is not int or hops < 0:
        log("dropped_invalid_hops", subject, envelope.get("msg_id"))
        return
    if hops > MAX_HOPS:
        log("dropped_max_hops", subject, envelope.get("msg_id"))
        return
    conv = envelope.get("conv") or envelope.get("msg_id") or str(uuid.uuid4())
    history = HISTORY.pop(conv, None)
    if history is None:
        history = deque(maxlen=HISTORY_MAX)
    HISTORY[conv] = history
    while len(HISTORY) > CONVERSATIONS_MAX:
        HISTORY.popitem(last=False)
    history.append({"role": "user", "content": format_incoming(envelope)})
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)

    tools = build_tools()
    sent = {"user": False, "any": False, "conv": conv,
            "prev": envelope.get("msg_id"), "hops": hops + 1}
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
                history.append({"role": "assistant", "content": content})
                break
        else:
            await tool_send(
                {"to": "user", "channel_id": "out", "kind": "result",
                 "body": {"ok": False, "error": "tool-call iteration limit exhausted"}},
                sent,
            )
            return
    except Exception as e:
        log_llm_error(subject, e)
        await tool_send(
            {"to": "user", "channel_id": "out", "kind": "result",
             "body": {"ok": False, "error": "LLM call failed"}},
            sent,
        )
        return

    if not sent["any"] and final_text.strip():
        # The model answered in plain text without using send. Route the reply
        # to the operator rather than dropping it silently.
        result = await tool_send(
            {"to": "user", "channel_id": "out", "kind": "text", "body": {"text": final_text}},
            sent,
        )
        if not json.loads(result)["ok"]:
            log("dropped_max_hops", subject, envelope.get("msg_id"))


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
    if "conv" in envelope and (not isinstance(envelope["conv"], str) or not envelope["conv"]):
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
        # Broker ACLs bind this subscription to its configured sender; envelope
        # identity fields are not authoritative. Hops remain cooperative metadata.
        envelope["ch"] = subject.split(".", 2)[2]
        if trigger_is_in:
            envelope["src"] = "user"
        elif channel is not None:
            envelope["src"] = channel["from"]
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
            json.dumps({"event": "connect_error", "error": f"could not connect to NATS at {host}:{port}: {type(e).__name__}"}),
            flush=True,
        )
        return None


async def main():
    global AGENT_ID, AGENT_NAME, GRAPH, SYSTEM_PROMPT, WORKSPACE
    global LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, OUT_CHANNELS, IN_CHANNELS
    global OUT_BY_ID, LIMITERS, NC, MAX_HOPS

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
    MAX_HOPS = int(env("MAX_HOPS", "8"))
    if MAX_HOPS < 0:
        raise ValueError("MAX_HOPS must be nonnegative")

    try:
        OUT_CHANNELS = json.loads(env("OUT_CHANNELS", "[]"))
        IN_CHANNELS = json.loads(env("IN_CHANNELS", "[]"))
    except Exception as e:
        print(json.dumps({"event": "config_error", "error": f"bad channel JSON: {e}"}), flush=True)
        sys.exit(1)

    global TOOLS_LIST, VISION_BASE_URL, VISION_API_KEY, VISION_MODEL
    TOOLS_LIST = [t for t in env("TOOLS", "workspace").split(",") if t]
    VISION_BASE_URL = env("VISION_BASE_URL", "")
    VISION_API_KEY = env("VISION_API_KEY", "")
    VISION_MODEL = env("VISION_MODEL", "")

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
