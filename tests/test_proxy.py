import asyncio
import json
import stat
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.proxy import create_proxy, graph_token
from app.schema import Graph


@pytest.fixture
def setup_proxy(web_app):
    cfg = replace(web_app.cfg, llm_base_url="https://text.invalid/v1",
                  llm_api_key="real-text-secret", llm_model="fixed-text",
                  vision_base_url="https://vision.invalid/v1",
                  vision_api_key="real-vision-secret", vision_model="fixed-vision",
                  graph_token_budget=10, graph_usd_budget=0)
    directory = cfg.data_dir / "g"
    token = graph_token(directory, cfg)
    return cfg, directory, token


def client_for(cfg, handler):
    return TestClient(create_proxy(cfg, transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "", "Basic wrong"])
def test_token_auth(setup_proxy, authorization):
    cfg, _, _ = setup_proxy
    def unexpected(request):
        pytest.fail("unauthenticated request reached upstream")
    client = client_for(cfg, unexpected)
    response = client.post("/llm/chat/completions", json={"messages": []},
                           headers={} if authorization is None else {"Authorization": authorization})
    assert response.status_code == 401


@pytest.mark.parametrize("budget", ["tokens", "usd"])
def test_shared_budget_persisted_and_models_forced(setup_proxy, budget):
    cfg, directory, token = setup_proxy
    state_path = directory / "proxy.json"
    if budget == "usd":
        state = json.loads(state_path.read_text())
        state.update(token_budget=0, usd_budget=0.00001)
        state_path.write_text(json.dumps(state))
    calls = []
    def upstream(request):
        calls.append(request)
        payload = json.loads(request.content)
        vision = request.url.host == "vision.invalid"
        assert payload["model"] == ("fixed-vision" if vision else "fixed-text")
        assert request.headers["authorization"] == ("Bearer real-vision-secret" if vision else "Bearer real-text-secret")
        assert payload["stream"] is False and payload["n"] == 1
        assert "api_key" not in payload
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
                                        "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})
    headers = {"Authorization": f"Bearer {token}"}
    client = client_for(cfg, upstream)
    assert client.post("/llm/chat/completions", headers=headers,
                       json={"model": "expensive", "messages": [], "n": 10, "api_key": "bad"}).status_code == 200
    # Both routes share the same cap; the last admitted call may overshoot it.
    if budget == "tokens":
        assert client.post("/vision/chat/completions", headers=headers, json={"messages": []}).status_code == 200
    state = json.loads(state_path.read_text())
    assert state["tokens"] == (12 if budget == "tokens" else 6)
    assert state["usd"] == pytest.approx(0.00002 if budget == "tokens" else 0.00001)
    # New proxy and re-provisioning do not reset usage or credentials.
    assert graph_token(directory, cfg) == token
    restarted = client_for(cfg, upstream)
    response = restarted.post("/vision/chat/completions", headers=headers, json={"messages": []})
    assert response.status_code == 429
    assert response.json() == {"error": "graph budget exhausted"}
    assert len(calls) == (2 if budget == "tokens" else 1)


def test_graph_tokens_isolate_budgets(setup_proxy):
    cfg, _, token = setup_proxy
    other = cfg.data_dir / "other"
    other.mkdir()
    other_token = graph_token(other, cfg)
    assert other_token != token
    def upstream(request):
        return httpx.Response(200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 0}})
    client = client_for(cfg, upstream)
    for value in (token, other_token):
        assert client.post("/llm/chat/completions", headers={"Authorization": f"Bearer {value}"}, json={}).status_code == 200
        assert client.post("/llm/chat/completions", headers={"Authorization": f"Bearer {value}"}, json={}).status_code == 429


def test_concurrent_requests_cannot_bypass_cap(setup_proxy):
    cfg, _, token = setup_proxy
    calls = []
    async def upstream(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 10, "completion_tokens": 0}})
    async def scenario():
        app = create_proxy(cfg, transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
            responses = await asyncio.gather(*[
                client.post(f"/{route}/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={})
                for route in ("llm", "vision")])
        assert sorted(r.status_code for r in responses) == [200, 429]
    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("body", [{}, {"usage": {"prompt_tokens": -1, "completion_tokens": 0}}, {"usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 1}}])
def test_unmetered_success_closes_budget(setup_proxy, body):
    cfg, _, token = setup_proxy
    client = client_for(cfg, lambda request: httpx.Response(200, json=body))
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post("/llm/chat/completions", headers=headers, json={}).status_code == 502
    assert client.post("/llm/chat/completions", headers=headers, json={}).status_code == 429


def test_errors_truncated_and_streaming_rejected(setup_proxy):
    cfg, _, token = setup_proxy
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(400, text="x" * 5000)
    client = client_for(cfg, upstream)
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/llm/chat/completions", headers=headers, json={})
    assert response.status_code == 400
    assert response.json() == {"error": "x" * 300}
    assert client.post("/llm/chat/completions", headers=headers, json={"stream": True}).status_code == 400
    assert client.get("/api/graphs").status_code == 404
    assert len(calls) == 1


def test_provision_never_passes_upstream_keys(setup_proxy, monkeypatch):
    from app import provision
    cfg, directory, token = setup_proxy
    calls = []
    monkeypatch.setattr(provision, "_run", lambda *args, **kwargs: calls.append(args) or "existing-image")
    monkeypatch.setattr(provision, "free_port", lambda: 12345)
    graph = Graph(name="g", agents=[{"id": "a"}, {"id": "b"}], channels=[])
    provision.provision(graph, cfg.data_dir, cfg.agent_image, cfg.nats_image, directory, cfg)
    for secret in (cfg.llm_api_key, cfg.vision_api_key):
        assert secret not in repr(calls)
    containers = [args for args in calls if args[:3] == ("docker", "run", "-d") and args[-1] == cfg.agent_image]
    assert len(containers) == 2
    for args in containers:
        assert "host.docker.internal:host-gateway" in args
        assert f"LLM_API_KEY={token}" in args and f"VISION_API_KEY={token}" in args
        assert f"LLM_BASE_URL=http://host.docker.internal:{cfg.proxy_port}/llm" in args
        assert f"VISION_BASE_URL=http://host.docker.internal:{cfg.proxy_port}/vision" in args
    assert stat.S_IMODE((directory / "proxy.json").stat().st_mode) == 0o600
