import asyncio
import json

import pytest

from app.schema import Graph


def test_shell_requires_explicit_permission(agent, tmp_path):
    agent.WORKSPACE = str(tmp_path)
    agent.TOOLS_LIST = ["workspace"]
    assert "shell" not in {t["function"]["name"] for t in agent.build_tools()}
    result = json.loads(asyncio.run(agent.execute_tool("shell", {"cmd": "touch denied"}, {})))
    assert result["ok"] is False
    assert "disabled" in result["error"]
    assert not (tmp_path / "denied").exists()
    agent.TOOLS_LIST = ["workspace", "shell"]
    assert "shell" in {t["function"]["name"] for t in agent.build_tools()}
    result = json.loads(asyncio.run(agent.execute_tool("shell", {"cmd": "printf allowed"}, {})))
    assert result["stdout"] == "allowed"
    graph = Graph.model_validate({"name": "g", "agents": [{"id": "a", "tools": agent.TOOLS_LIST}]})
    assert "shell" in graph.agents[0].tools


@pytest.mark.parametrize("name", ["browser_goto", "browser_content", "browser_click", "browser_fill", "browser_screenshot"])
def test_browser_calls_refused_when_disabled(agent, name):
    agent.TOOLS_LIST = ["workspace"]
    result = json.loads(asyncio.run(agent.execute_tool(name, {}, {})))
    assert result["ok"] is False
    assert "disabled" in result["error"]
    assert agent._browser_thread is None


def test_shell_timeout_kills_process_group(agent, tmp_path, monkeypatch):
    import os
    import signal
    from pathlib import Path

    agent.WORKSPACE = str(tmp_path)
    monkeypatch.setattr(agent, "SHELL_TIMEOUT", 0.1)
    processes = []
    create = asyncio.create_subprocess_shell

    async def capture(*args, **kwargs):
        proc = await create(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_shell", capture)
    async def run():
        try:
            result = json.loads(await agent.tool_shell({"cmd": "sleep 30 & echo $! > child.pid; wait"}))
            assert "timed out" in result["error"]
            child = int((tmp_path / "child.pid").read_text())
            status = Path(f"/proc/{child}/stat")
            assert not status.exists() or status.read_text().split()[2] == "Z"
            assert processes[0].returncode is not None
        finally:
            for proc in processes:
                if proc.returncode is None:
                    # Also clean up when demonstrating the old timeout bug.
                    child_file = tmp_path / "child.pid"
                    if child_file.exists():
                        try:
                            os.kill(int(child_file.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    proc.kill()
                    await proc.communicate()

    asyncio.run(run())


def test_conversations_do_not_share_history(agent):
    from unittest.mock import AsyncMock

    requests = []
    async def chat(messages, tools):
        requests.append([dict(m) for m in messages])
        return {"choices": [{"message": {"content": "reply"}}]}

    agent.llm_chat = chat
    agent.NC = AsyncMock()
    async def run():
        for mid, conv, text in [("root", None, "private alpha"), ("other", None, "private beta"), ("follow", "root", "alpha followup")]:
            env = {"msg_id": mid, "src": "user", "ch": "in.a", "kind": "text", "body": {"text": text}}
            if conv:
                env["conv"] = conv
            await agent.handle_envelope(env, "orc.g.in.a", True)
        await asyncio.sleep(0)

    asyncio.run(run())
    assert "private alpha" not in json.dumps(requests[1])
    assert "private beta" not in json.dumps(requests[2])
    assert "private alpha" in json.dumps(requests[2])
    replies = [json.loads(call.args[1]) for call in agent.NC.publish.call_args_list]
    assert [r["conv"] for r in replies] == ["root", "other", "root"]
    assert [r["prev"] for r in replies] == ["root", "other", "follow"]


def test_conversation_history_is_bounded_and_lru(agent):
    from unittest.mock import AsyncMock

    agent.NC = AsyncMock()
    agent.llm_chat = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    async def turn(conv, text):
        await agent.handle_envelope({"conv": conv, "msg_id": text, "body": {"text": text}}, "", True)

    async def run():
        for i in range(50):
            await turn(str(i), f"seed-{i}")
        await turn("0", "refresh")
        await turn("new", "new")
        assert "0" in agent.HISTORY and "1" not in agent.HISTORY
        assert len(agent.HISTORY) == 50
        for i in range(agent.HISTORY_MAX):
            await turn("0", f"later-{i}")
        messages = agent.llm_chat.call_args.args[0]
        assert "seed-0" not in json.dumps(messages)
        assert len(agent.HISTORY["0"]) <= agent.HISTORY_MAX

    asyncio.run(run())


def test_tool_send_propagates_conversation_on_all_routes(agent):
    from unittest.mock import AsyncMock

    agent.NC = AsyncMock()
    agent.OUT_BY_ID = {"c": {"id": "c", "to": "b", "kinds": ["text"]}}
    async def run():
        for to, channel in [("user", "out"), ("b", "c")]:
            result = await agent.tool_send(
                {"to": to, "channel_id": channel, "kind": "text", "body": {"text": "hello"}},
                {"conv": "root", "prev": "parent"},
            )
            assert json.loads(result)["ok"]

    asyncio.run(run())
    for call in agent.NC.publish.call_args_list:
        message = json.loads(call.args[1])
        assert message["conv"] == "root"
        assert message["prev"] == "parent"
