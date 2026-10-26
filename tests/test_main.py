import json
from pathlib import Path
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("bad", ["Upper", "bad!", "a" * 33, "a%0A", "%2E%2E"])
@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", "/accept"), ("GET", "/traffic"), ("DELETE", ""), ("GET", "/agents/a"), ("POST", "/agents/a/prompt")])
def test_invalid_graph_slugs_return_400(web_app, bad, method, suffix):
    client = TestClient(web_app.app)
    response = client.request(method, f"/api/graphs/{bad}{suffix}", json={"prompt": "hello"})
    assert response.status_code == 400


@pytest.mark.parametrize("bad", ["Upper", "bad!", "a" * 33, "a%0A", "%2E%2E"])
@pytest.mark.parametrize("method,suffix", [("GET", ""), ("POST", "/prompt")])
def test_invalid_agent_slugs_return_400(web_app, bad, method, suffix):
    client = TestClient(web_app.app)
    response = client.request(method, f"/api/graphs/g/agents/{bad}{suffix}", json={"prompt": "hello"})
    assert response.status_code == 400


def test_valid_slugs_and_prompt_conversation(web_app, monkeypatch):
    client = TestClient(web_app.app)
    assert client.get("/api/graphs/g/agents/a").text == "# Agent"
    graph_dir = web_app.cfg.data_dir / "g"
    (graph_dir / "status.json").write_text('{"state":"live"}')
    nc = AsyncMock()
    monkeypatch.setattr(web_app.fabric, "get_conn", AsyncMock(return_value=nc))
    response = client.post("/api/graphs/g/agents/a/prompt", json={"prompt": "hello"})
    assert response.status_code == 200
    message = json.loads(nc.publish.call_args.args[1])
    assert message["conv"] == message["msg_id"] == response.json()["msg_id"]
    assert message["prev"] is None
    assert message["hops"] == 0


def test_traffic_reads_bounded_tail_and_polls(web_app, monkeypatch):
    path = web_app.cfg.data_dir / "g" / "traffic.jsonl"
    row = lambda i: json.dumps({"i": i, "text": "x" * 100}) + "\n"
    path.write_text("".join(row(i) for i in range(20000)))
    size = path.stat().st_size
    original_open = Path.open
    reads = []

    class BoundedReader:
        def __init__(self, file):
            self.file = file
        def __enter__(self):
            self.file.__enter__()
            return self
        def __exit__(self, *args):
            return self.file.__exit__(*args)
        def seek(self, *args):
            return self.file.seek(*args)
        def tell(self):
            return self.file.tell()
        def read(self, n=-1):
            assert 0 <= n <= 1024 * 1024
            reads.append(n)
            return self.file.read(n)

    def bounded_open(self, *args, **kwargs):
        file = original_open(self, *args, **kwargs)
        return BoundedReader(file) if self == path and args and args[0] == "rb" else file

    monkeypatch.setattr(Path, "open", bounded_open)
    client = TestClient(web_app.app)
    first = client.get("/api/graphs/g/traffic").json()
    assert reads
    assert first["lines"][0]["i"] > 0
    assert first["lines"][-1]["i"] == 19999
    assert first["next"] == size
    assert client.get(f"/api/graphs/g/traffic?after={first['next']}").json()["lines"] == []
    with path.open("a") as f:
        f.write(row(20000))
    second = client.get(f"/api/graphs/g/traffic?after={first['next']}").json()
    assert [x["i"] for x in second["lines"]] == [20000]
    path.write_text(row(20001))
    rotated = client.get(f"/api/graphs/g/traffic?after={second['next']}").json()
    assert [x["i"] for x in rotated["lines"]] == [20001]


def test_traffic_waits_for_complete_line(web_app):
    path = web_app.cfg.data_dir / "g" / "traffic.jsonl"
    path.write_bytes(b'{"i":1}\n{"i":')
    client = TestClient(web_app.app)
    first = client.get("/api/graphs/g/traffic").json()
    assert first["lines"] == [{"i": 1}]
    with path.open("ab") as f:
        f.write(b'2}\n')
    second = client.get(f"/api/graphs/g/traffic?after={first['next']}").json()
    assert second["lines"] == [{"i": 2}]


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/graphs"),
    ("POST", "/api/graphs"),
    ("GET", "/api/graphs/g"),
    ("POST", "/api/graphs/g/accept"),
    ("GET", "/api/graphs/g/agents/a"),
    ("POST", "/api/graphs/g/agents/a/prompt"),
    ("GET", "/api/graphs/g/traffic"),
    ("DELETE", "/api/graphs/g"),
    ("GET", "/api/unknown"),
    ("GET", "/api"),
])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic api-secret", "Bearer "])
def test_api_requires_bearer_before_routing(web_app, monkeypatch, method, path, authorization):
    monkeypatch.setattr(web_app, "cfg", replace(web_app.cfg, api_token="api-secret"))
    client = TestClient(web_app.app)
    response = client.request(
        method, path,
        headers={} if authorization is None else {"Authorization": authorization},
    )
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert (web_app.cfg.data_dir / "g" / "graph.json").is_file()


def test_api_accepts_token_but_not_graph_token(web_app, monkeypatch):
    from app.proxy import graph_token

    monkeypatch.setattr(web_app, "cfg", replace(web_app.cfg, api_token="api-secret"))
    token = graph_token(web_app.cfg.data_dir / "g", web_app.cfg)
    client = TestClient(web_app.app)
    assert client.get("/api/graphs/g", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    response = client.get("/api/graphs/g", headers={"Authorization": "Bearer api-secret"})
    assert response.status_code == 200
    assert response.json()["name"] == "g"


@pytest.mark.parametrize("allow,expected", [(False, 401), (True, 200)])
def test_api_without_token_fails_closed_unless_opted_out(web_app, monkeypatch, allow, expected):
    monkeypatch.setattr(web_app, "cfg", replace(web_app.cfg, api_token="", allow_no_auth=allow))
    client = TestClient(web_app.app)
    assert client.get("/api/graphs").status_code == expected
    assert client.get("/api/graphs", headers={"Authorization": "Bearer arbitrary"}).status_code == expected
    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/health").status_code == 200


@pytest.mark.parametrize("opt_out,expected", [("", 401), ("true", 401), ("1", 200)])
def test_auth_environment_configuration(web_app, monkeypatch, opt_out, expected):
    from app.config import Config

    monkeypatch.setenv("ORC_API_TOKEN", "")
    monkeypatch.setenv("ORC_ALLOW_NO_AUTH", opt_out)
    monkeypatch.setattr(web_app, "cfg", Config.load({"ORC_DATA_DIR": str(web_app.cfg.data_dir)}))
    client = TestClient(web_app.app)
    assert client.get("/api/graphs/g").status_code == expected
    monkeypatch.setenv("ORC_API_TOKEN", "configured-secret")
    monkeypatch.setattr(web_app, "cfg", Config.load({"ORC_DATA_DIR": str(web_app.cfg.data_dir)}))
    assert client.get("/api/graphs/g").status_code == 401
    assert client.get("/api/graphs/g", headers={"Authorization": "Bearer configured-secret"}).status_code == 200
