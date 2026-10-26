"""Offline review regressions; process and environment probes use fixtures only."""
import asyncio
import copy
import json
import tracemalloc
from pathlib import Path
from types import SimpleNamespace


async def workspace_permissions(ctx):
    a = ctx.agent()
    workspace_names = {"read_file", "write_file", "list_files"}
    for permissions in ([], ["shell"], ["browser"]):
        a.TOOLS_LIST = permissions
        advertised = {tool["function"]["name"] for tool in a.build_tools()}
        ctx.require(not workspace_names & advertised,
                    "workspace tools must not be advertised without workspace permission")
        touched = []
        for name in workspace_names:
            setattr(a, "tool_" + name, lambda args, name=name: touched.append(name))
        for name, args in (("read_file", {"path": "fixture"}),
                           ("write_file", {"path": "denied", "content": "denied"}),
                           ("list_files", {})):
            result = json.loads(await a.execute_tool(name, args, {}))
            ctx.require(result.get("ok") is False and not touched,
                        "disabled workspace calls must refuse before dispatch")
        ctx.require(not (Path(a.WORKSPACE) / "denied").exists(), "disabled writes must not create files")
    enabled = ctx.agent("enabled-workspace")
    ctx.require(workspace_names <= {t["function"]["name"] for t in enabled.build_tools()},
                "workspace permission must advertise all workspace tools")
    written = json.loads(await enabled.execute_tool("write_file", {"path": "fixture", "content": "allowed"}, {}))
    read = json.loads(await enabled.execute_tool("read_file", {"path": "fixture"}, {}))
    listed = json.loads(await enabled.execute_tool("list_files", {}, {}))
    ctx.require(written.get("ok") is True and read.get("result") == "allowed" and "fixture" in listed.get("result", []),
                "enabled workspace read/write/list must still work")


class Stream:
    def __init__(self, ctx, first, repeats=256):
        self.ctx = ctx
        self.first = first
        self.repeats = repeats
        self.reads = 0
        self.eof = False

    async def read(self, size):
        self.ctx.require(0 < size <= 8192, "shell capture must use bounded stream reads")
        await asyncio.sleep(0)
        self.reads += 1
        if self.first:
            value, self.first = self.first[:size], self.first[size:]
            return value
        if self.repeats:
            self.repeats -= 1
            return b"z" * size
        self.eof = True
        return b""


async def shell_bounded_capture(ctx):
    a = ctx.agent()
    a.TOOLS_LIST = ["shell"]
    a.SHELL_CAPTURE = 7
    processes = []

    class Process:
        def __init__(self):
            # Split a multibyte code point across reads; count capture in characters.
            self.stdout = Stream(ctx, b"\xf0")
            self.stderr = Stream(ctx, b"err\xfftail")
            self.returncode = None
            self.used_communicate = False

        async def communicate(self):
            self.used_communicate = True
            self.returncode = 23
            return b"unbounded", b"buffering"

        async def wait(self):
            while not self.stdout.eof or not self.stderr.eof:
                await asyncio.sleep(0)
            self.returncode = 23
            return self.returncode

    async def create(*args, **kwargs):
        ctx.require(kwargs.get("start_new_session") is True, "shell must start an isolated process group")
        proc = Process()
        # First two reads split the emoji's UTF-8 encoding without large allocations.
        base_read = proc.stdout.read
        pending = [b"\xf0", b"\x9f\x98\x80abc\xffXYZ"]

        async def read(size):
            if pending:
                ctx.require(0 < size <= 8192, "shell capture must use bounded stream reads")
                return pending.pop(0)
            proc.stdout.first = b""
            return await base_read(size)

        proc.stdout.read = read
        processes.append(proc)
        return proc

    ctx.patch("asyncio.create_subprocess_shell", create)
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    tracemalloc.reset_peak()
    try:
        result = json.loads(await a.execute_tool("shell", {"cmd": "fixture noisy process"}, {}))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        if not already_tracing:
            tracemalloc.stop()
    ctx.require(peak - baseline < 1024 * 1024,
                "capturing fixture streams totaling 4 MiB must not retain unbounded output")
    proc = processes[0]
    ctx.require(not proc.used_communicate, "shell must drain bounded reads instead of buffering communicate output")
    ctx.require(proc.stdout.eof and proc.stderr.eof and proc.stdout.reads > 250 and proc.stderr.reads > 250,
                "both noisy streams must be fully drained after their capture limits")
    ctx.require(result == {"exit_code": 23, "stdout": "\U0001f600abc\ufffdXY", "stderr": "err\ufffdtai"},
                "bounded capture must preserve exit status, UTF-8 replacement and character-prefix semantics")


async def receiver_subject_identity(ctx):
    from evals.agent_cases import envelope, reply

    a = ctx.agent()
    requests = []

    async def chat(messages, tools):
        requests.append(copy.deepcopy(messages))
        return reply()

    a.llm_chat = chat
    forged = envelope()
    forged["src"] = "user"
    forged["ch"] = "in.a"
    channel = {"id": "ba", "from": "b", "to": "a", "kinds": ["text"]}
    handler = a.make_handler("orc.g.ch.ba", channel, False)
    await handler(SimpleNamespace(data=json.dumps(forged).encode()))
    content = requests[-1][-1]["content"]
    ctx.require("from 'b'" in content and "channel 'ch.ba'" in content and "from 'user'" not in content,
                "peer receiver must derive sender and channel from its subscribed subject/config, not body claims")
    forged["src"] = "b"
    forged["ch"] = "ch.ba"
    handler = a.make_handler("orc.g.in.a", None, True)
    await handler(SimpleNamespace(data=json.dumps(forged).encode()))
    content = requests[-1][-1]["content"]
    ctx.require("from 'user'" in content and "channel 'in.a'" in content,
                "operator ingress identity must also come from its subscribed subject")


async def enabled_shell_environment(ctx, key, vision):
    environment = {}
    for command in ctx.commands:
        if command[:3] != ("docker", "run", "-d"):
            continue
        for index, argument in enumerate(command[:-1]):
            if argument == "-e":
                name, _, value = command[index + 1].partition("=")
                if name in {"LLM_API_KEY", "VISION_API_KEY"}:
                    environment[name] = value
    ctx.require(set(environment) == {"LLM_API_KEY", "VISION_API_KEY"} and
                environment["LLM_API_KEY"] == environment["VISION_API_KEY"] and
                bool(environment["LLM_API_KEY"]) and key not in environment.values() and vision not in environment.values(),
                "captured container credential environment must contain only a shared nonempty graph token")
    payload = b"\0".join(f"{name}={value}".encode() for name, value in environment.items())
    launched = []

    class Process:
        def __init__(self):
            self.stdout = Stream(ctx, payload, repeats=0)
            self.stderr = Stream(ctx, b"", repeats=0)
            self.returncode = 0

        async def communicate(self):
            return payload, b""

        async def wait(self):
            return self.returncode

    async def create(command, **kwargs):
        ctx.require(command in {"env", "cat /proc/self/environ", "cat /proc/1/environ"},
                    "simulated shell must never execute a real environment probe")
        launched.append(command)
        return Process()

    ctx.patch("asyncio.create_subprocess_shell", create)
    a = ctx.agent("enabled-shell-env")
    a.TOOLS_LIST = ["shell"]
    for command in ("env", "cat /proc/self/environ", "cat /proc/1/environ"):
        result = json.loads(await a.execute_tool("shell", {"cmd": command}, {}))
        ctx.require(result.get("exit_code") == 0 and environment["LLM_API_KEY"] in result.get("stdout", "") and
                    key not in result["stdout"] and vision not in result["stdout"],
                    "enabled shell must expose the simulated graph token but no provider credentials")
    ctx.require(len(launched) == 3, "all enabled shell environment probes must reach the mocked process boundary")


CASES = [
    ("2", "review_workspace_permission_advertisement_and_dispatch", workspace_permissions),
    ("14", "review_shell_capture_bounds_while_draining", shell_bounded_capture),
    ("3", "review_receiver_subject_identity", receiver_subject_identity),
]
