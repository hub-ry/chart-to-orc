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
