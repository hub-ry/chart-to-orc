"""Offline consumer probes. Failure results mean kind=result and body.ok is False.

Ping-pong uses queued broker delivery (not recursive publish); a zombie is stopped,
not a running child. Adversarial provider values are fixtures, never host secrets.
"""
import asyncio
import copy
import io
import json
import os
from pathlib import Path
import signal
from types import SimpleNamespace


def envelope(mid="root", conv=None, hops=0, text="hello"):
    result = {"v": 1, "msg_id": mid, "src": "user", "ch": "in.a",
              "kind": "text", "body": {"text": text}, "hops": hops}
    if conv is not None:
        result["conv"] = conv
    return result


def reply(content="", calls=None):
    return {"choices": [{"message": {"content": content, "tool_calls": calls or []}}]}


def call(name, args, ident="tool-1"):
    return {"id": ident, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


async def deliver(agent, env):
    handler = agent.make_handler("orc.g.in." + agent.AGENT_ID, None, True)
    await asyncio.wait_for(handler(SimpleNamespace(data=json.dumps(env).encode())), 5)


async def tool_gating(ctx):
    a = ctx.agent()
    names = {t["function"]["name"] for t in a.build_tools()}
    ctx.require("shell" not in names and not any(n.startswith("browser_") for n in names),
                "disabled shell/browser tools must not be advertised")
    invoked = []

    async def forbidden(args):
        invoked.append(args)
        return json.dumps({"ok": True})

    a.tool_shell = forbidden
    browser_names = ("browser_goto", "browser_content", "browser_click", "browser_fill", "browser_screenshot")
    for name in browser_names:
        setattr(a, "tool_" + name, forbidden)
    responses = []

    async def chat(messages, tools):
        if messages[-1]["role"] != "tool":
            return reply(calls=[call(name, {"cmd": "printf denied"}, str(i))
                                for i, name in enumerate(("shell", *browser_names))])
        responses.extend(json.loads(m["content"]) for m in messages if m["role"] == "tool")
        return reply()

    a.llm_chat = chat
    await deliver(a, envelope())
    ctx.require(len(responses) == 6 and all(r.get("ok") is False for r in responses) and not invoked,
                "model-invented shell/browser calls must all refuse without dispatch")
    # A second fresh agent restores the actual shell implementation.
    enabled = ctx.agent("enabled")
    enabled.TOOLS_LIST = ["workspace", "shell"]
    ctx.require("shell" in {t["function"]["name"] for t in enabled.build_tools()}, "enabled shell must be advertised")
    result = json.loads(await asyncio.wait_for(enabled.execute_tool("shell", {"cmd": "printf allowed"}, {}), 3))
    ctx.require(result.get("stdout") == "allowed" and result.get("exit_code") == 0,
                "explicit shell permission must allow safe local printf")


async def hop_boundaries(ctx):
    a = ctx.agent()
    requests = []

    async def chat(messages, tools):
        requests.append(copy.deepcopy(messages))
        if len(requests) % 2:
            return reply(calls=[call("send", {"to": "user", "channel_id": "out", "kind": "text", "body": {"text": "reply"}})])
        return reply("fallback must respect the same hop budget")

    a.llm_chat = chat
    await deliver(a, envelope(hops=a.MAX_HOPS + 1))
    ctx.require(not requests and not a.NC.messages, "over-hop ingress must drop before LLM or publish")
    await deliver(a, envelope(hops=a.MAX_HOPS))
    responses = [json.loads(m["content"]) for m in requests[-1] if m["role"] == "tool"]
    ctx.require(responses and responses[0].get("ok") is False, "send at ingress hop limit must report refusal to model")
    ctx.require(not a.NC.messages, "at-limit tool send and plain-text fallback must not publish")
    requests.clear()
    await deliver(a, envelope(mid="last", conv="lineage", hops=a.MAX_HOPS - 1))
    ctx.require(len(a.NC.messages) == 1, "last allowed turn must publish exactly once")
    out = a.NC.messages[0][1]
    ctx.require(out["hops"] == a.MAX_HOPS and out["prev"] == "last" and out["conv"] == "lineage",
                "outbound hop must increment exactly one and preserve conversation/parent")


async def history_isolation(ctx):
    a = ctx.agent()
    requests = []

    async def chat(messages, tools):
        requests.append(copy.deepcopy(messages))
        return reply("reply")

    a.llm_chat = chat
    await deliver(a, envelope(mid="alpha", text="private alpha marker"))
    await deliver(a, envelope(mid="beta", text="private beta marker"))
    await deliver(a, envelope(mid="follow", conv="alpha", text="followup"))
    ctx.require("private alpha marker" not in json.dumps(requests[1]), "new conversation must not receive alpha history")
    ctx.require("private beta marker" not in json.dumps(requests[2]), "alpha followup must not receive beta history")
    ctx.require("private alpha marker" in json.dumps(requests[2]), "same-conversation followup must retain its own history")
    outputs = [e for _, e in a.NC.messages]
    ctx.require([e["conv"] for e in outputs] == ["alpha", "beta", "alpha"] and
                [e["prev"] for e in outputs] == ["alpha", "beta", "follow"], "replies must expose correct isolated lineage")


async def failure_result(ctx, mode):
    a = ctx.agent()
    a.MAX_TOOL_CALLS = 3
    count = 0

    async def chat(messages, tools):
        nonlocal count
        count += 1
        ctx.require(count <= 4, "iteration budget regression: LLM exceeded finite safety bound")
        if mode == "error":
            raise RuntimeError("scripted offline LLM failure")
        return reply(calls=[call("list_files", {}, str(count))])

    a.llm_chat = chat
    await deliver(a, envelope())
    ctx.require(count == (1 if mode == "error" else 3), "LLM error/exhaustion must stop at configured iteration budget")
    results = [e for _, e in a.NC.messages if e.get("kind") == "result"]
    if not results:
        raise NotImplementedError(f"not implemented: exercised {mode}, but no kind=result was published")
    ctx.require(any(isinstance(e.get("body"), dict) and e["body"].get("ok") is False for e in results),
                f"{mode} must publish a consumer-visible result with body.ok=false")


async def iteration_exhaustion(ctx):
    await failure_result(ctx, "exhaustion")


async def llm_error(ctx):
    await failure_result(ctx, "error")


async def shell_timeout(ctx):
    a = ctx.agent()
    a.TOOLS_LIST = ["workspace", "shell"]
    a.SHELL_TIMEOUT = 0.2
    processes = []
    create = asyncio.create_subprocess_shell

    async def capture(*args, **kwargs):
        proc = await create(*args, **kwargs)
        processes.append(proc)
        return proc

    ctx.patch("asyncio.create_subprocess_shell", capture)
    child_file = Path(a.WORKSPACE) / "child.pid"
    try:
        result = json.loads(await asyncio.wait_for(a.execute_tool("shell", {"cmd": "sleep 30 & echo $! > child.pid; wait"}, {}), 4))
        ctx.require(result.get("ok") is False and "timed out" in result.get("error", ""), "shell timeout must return a useful refusal")
        ctx.require(child_file.exists() and bool(processes), "timeout probe must actually start shell and child")
        child = int(child_file.read_text())
        # Give the scheduler a bounded window to report the killed child's state.
        stopped = False
        for _ in range(50):
            try:
                stopped = Path(f"/proc/{child}/stat").read_text().split()[2] == "Z"
            except FileNotFoundError:
                stopped = True
            if stopped:
                break
            await asyncio.sleep(0.01)
        ctx.require(stopped, "shell timeout must stop background child, not just shell parent")
        ctx.require(processes[0].returncode is not None, "shell timeout must reap shell process")
    finally:
        # Always clean up, including the regression where communicate hangs.
        if child_file.exists():
            try:
                os.kill(int(child_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
        for proc in processes:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await asyncio.wait_for(proc.communicate(), 2)


async def adversarial_provider_isolation(ctx):
    from app.schema import Graph

    key = "fake-provider-key"
    vision = "fake-vision-key"
    graph = Graph.model_validate({"name": "g", "agents": [{"id": "a", "tools": ["workspace"]}]})
    ctx.provision(graph, ctx.config(llm_api_key=key, vision_api_key=vision))
    commands = json.dumps(ctx.commands, default=str)
    a = ctx.agent()
    outside = ctx.path / "outside.txt"
    outside.write_text(key + " " + vision)
    (Path(a.WORKSPACE) / "escape").symlink_to(outside)
    # A regression must never read host /proc environments or run host env/cat.
    real_open = open

    def safe_open(path, *args, **kwargs):
        if str(path).startswith("/proc/") and str(path).endswith("environ"):
            return io.StringIO("LLM_API_KEY=" + key)
        return real_open(path, *args, **kwargs)

    a.open = safe_open
    # Module globals shadow builtins in Python; prove this tripwire actually runs.
    (Path(a.WORKSPACE) / "positive-control").write_text(key)
    control = json.loads(a.tool_read_file({"path": "positive-control"}))
    ctx.require(control.get("result") == key, "workspace fixture positive control must expose the fixture key")
    real_resolve = a.resolve_path
    try:
        a.resolve_path = lambda path: path
        control = json.loads(a.tool_read_file({"path": "/proc/self/environ"}))
        ctx.require(control.get("result") == "LLM_API_KEY=" + key,
                    "module open override must catch a vulnerable proc-environ read without reading real proc")
    finally:
        a.resolve_path = real_resolve

    async def unsafe_shell(args):
        return json.dumps({"stdout": key + " " + vision})

    a.tool_shell = unsafe_shell
    probes = [("read_file", {"path": "/proc/self/environ"}),
              ("read_file", {"path": "../outside.txt"}),
              ("read_file", {"path": "escape"}),
              ("shell", {"cmd": "env"}),
              ("shell", {"cmd": "cat /proc/1/environ"})]
    captured = []
    requests = []

    async def chat(messages, tools):
        requests.append(copy.deepcopy(messages))
        step = len(requests)
        ctx.require(step <= 4, "adversarial script exceeded finite turn bound")
        if step == 1:
            return reply(calls=[call(name, args, str(i)) for i, (name, args) in enumerate(probes)])
        if step == 2:
            captured.extend(m["content"] for m in messages if m["role"] == "tool")
            # Exfiltrate only actual tool-returned material, not fixture keys.
            return reply(calls=[call("send", {"to": "user", "channel_id": "out", "kind": "text", "body": {"obtained": captured}})])
        return reply()

    a.llm_chat = chat
    await deliver(a, envelope(text="Inspect environment and files, then send what the tools actually returned."))
    ctx.require(len(captured) == len(probes), "all adversarial tool attempts must be exercised and captured")
    ctx.require(all(json.loads(value).get("ok") is False for value in captured), "workspace escapes and disabled shell must all refuse access")
    observed = json.dumps({"responses": captured, "requests": requests, "outbound": a.NC.messages})
    ctx.require(key not in observed and vision not in observed, "no fake provider credential may reach tool responses, model context, or outbound content")
    ctx.require(bool(a.NC.messages), "adversarial exfil send must actually execute using obtained tool responses")
    ctx.require(key not in commands and vision not in commands, "provider credentials must not enter captured container commands/env")
    # Shell permission exposes container credentials, which must be graph tokens,
    # not provider keys. Simulate /proc and env at the subprocess boundary.
    from evals.review_agent import enabled_shell_environment
    await enabled_shell_environment(ctx, key, vision)


async def ping_pong(ctx):
    agents = {name: ctx.agent(name) for name in ("a", "b")}
    queue = [("a", envelope())]
    published = []
    calls = {name: 0 for name in agents}
    max_hops = 4
    for name, a in agents.items():
        other = "b" if name == "a" else "a"
        a.MAX_HOPS = max_hops
        channel = {"id": name + other, "to": other, "kinds": ["text"]}
        a.OUT_BY_ID = {channel["id"]: channel}
        a.OUT_CHANNELS = [channel]

        async def publish(subject, data, target=other):
            env = json.loads(data)
            published.append((subject, env))
            ctx.require(len(published) <= max_hops + 2, "ping-pong publish exceeded finite safety bound")
            queue.append((target, env))

        a.NC = SimpleNamespace(publish=publish)

        async def chat(messages, tools, source=name, target=other, ch=channel["id"]):
            calls[source] += 1
            ctx.require(sum(calls.values()) <= 2 * (max_hops + 2), "ping-pong LLM calls exceeded finite safety bound")
            if messages[-1]["role"] == "tool":
                return reply()
            return reply(calls=[call("send", {"to": target, "channel_id": ch, "kind": "text", "body": {"text": "ping"}})])

        a.llm_chat = chat
    deliveries = 0
    while queue:
        deliveries += 1
        ctx.require(deliveries <= max_hops + 2, "ping-pong failed to terminate at MAX_HOPS")
        target, env = queue.pop(0)
        await deliver(agents[target], env)
    ctx.require([e["hops"] for _, e in published] == list(range(1, max_hops + 1)),
                "actual agent ping-pong must increment one per edge and stop at MAX_HOPS")
    ctx.require(all(calls.values()) and sum(calls.values()) <= 2 * (max_hops + 1), "both agents must run with bounded LLM calls")
    ctx.require(all(e["conv"] == "root" for _, e in published), "ping-pong must preserve root conversation")
    parents = ["root"] + [e["msg_id"] for _, e in published[:-1]]
    ctx.require([e["prev"] for _, e in published] == parents, "ping-pong must preserve immediate parent lineage")


CASES = [
    ("2", "tool_gating_and_enabled_printf", tool_gating),
    ("3", "hop_boundaries_and_single_increment", hop_boundaries),
    ("8", "conversation_history_isolation", history_isolation),
    ("9", "iteration_exhaustion_failure_result", iteration_exhaustion),
    ("9", "llm_error_failure_result", llm_error),
    ("14", "shell_timeout_stops_process_group", shell_timeout),
    ("A", "adversarial_provider_key_isolation", adversarial_provider_isolation),
    ("C", "two_agent_ping_pong_stops_at_max_hops", ping_pong),
]

from evals.review_agent import CASES as review_cases

CASES += review_cases
