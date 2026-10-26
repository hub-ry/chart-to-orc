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
