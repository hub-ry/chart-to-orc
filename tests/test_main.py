import json
from pathlib import Path
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
