import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def agent():
    path = Path(__file__).resolve().parents[1] / "agentd" / "agentd.py"
    spec = importlib.util.spec_from_file_location("agentd_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def web_app(monkeypatch, tmp_path):
    import importlib
    from dataclasses import replace
    from app import config

    # app.main loads config on import; never open the real environment file.
    monkeypatch.setattr(config, "_load_env_file", lambda path: {})
    main = importlib.import_module("app.main")
    monkeypatch.setattr(main, "cfg", replace(main.cfg, data_dir=tmp_path))
    graph = tmp_path / "g"
    graph.mkdir()
    (graph / "graph.json").write_text('{"name":"g","agents":[{"id":"a"}],"channels":[]}')
    (graph / "agents").mkdir()
    (graph / "agents" / "a.md").write_text("# Agent")
    return main
