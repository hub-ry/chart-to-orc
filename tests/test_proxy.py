import asyncio
import json
import stat
from dataclasses import replace
from types import SimpleNamespace

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


@pytest.mark.parametrize("route", ["llm", "vision"])
def test_proxy_rejects_api_token(setup_proxy, route):
    cfg, _, _ = setup_proxy
    cfg = replace(cfg, api_token="api-secret", allow_no_auth=True)
    def unexpected(request):
        pytest.fail("API token reached upstream")
    response = client_for(cfg, unexpected).post(
        f"/{route}/chat/completions", json={"messages": []},
        headers={"Authorization": f"Bearer {cfg.api_token}"},
    )
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
    assert json.loads(state_path.read_text())["calls"] == len(calls)


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


@pytest.fixture
def guarded_proxy(setup_proxy, monkeypatch):
    cfg, directory, token = setup_proxy
    now = [1000.0]
    monkeypatch.setattr("app.proxy.time", SimpleNamespace(time=lambda: now[0]))
    cfg = replace(cfg, graph_max_calls=2, graph_max_seconds=30)
    path = directory / "proxy.json"
    state = json.loads(path.read_text())
    state.update(max_calls=2, max_seconds=30, calls=0, provisioned_at=now[0])
    path.write_text(json.dumps(state))
    return cfg, directory, token, now


def test_call_boundary_shared_routes_and_persisted_caps(guarded_proxy):
    cfg, directory, token, _ = guarded_proxy
    calls = []
    path = directory / "proxy.json"
    def upstream(request):
        calls.append(request)
        # Admission is durable before the upstream request, not after its response.
        assert json.loads(path.read_text())["calls"] == len(calls)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    headers = {"Authorization": f"Bearer {token}"}
    client = client_for(cfg, upstream)
    for route in ("llm", "vision"):
        assert client.post(f"/{route}/chat/completions", headers=headers, json={}).status_code == 200
    larger = replace(cfg, graph_max_calls=2000, graph_max_seconds=3000)
    assert graph_token(directory, larger) == token
    restarted = client_for(larger, upstream)
    response = restarted.post("/llm/chat/completions", headers=headers, json={})
    assert response.status_code == 429
    assert response.json() == {"error": "graph call limit exhausted"}
    state = json.loads(path.read_text())
    assert state["calls"] == 2 and state["max_calls"] == 2
    assert state["max_seconds"] == 30
    assert len(calls) == 2


def test_concurrent_routes_share_call_admission(guarded_proxy):
    cfg, directory, token, _ = guarded_proxy
    path = directory / "proxy.json"
    state = json.loads(path.read_text())
    state["max_calls"] = 1
    path.write_text(json.dumps(state))
    calls = []
    async def upstream(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    async def scenario():
        app = create_proxy(cfg, transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
            responses = await asyncio.gather(*[
                client.post(f"/{route}/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={})
                for route in ("llm", "vision")])
        assert sorted(response.status_code for response in responses) == [200, 429]
        rejected = next(response for response in responses if response.status_code == 429)
        assert rejected.json() == {"error": "graph call limit exhausted"}
    asyncio.run(scenario())
    assert len(calls) == 1
    assert json.loads(path.read_text())["calls"] == 1


@pytest.mark.parametrize("failure", ["transport", "http"])
def test_failed_attempts_count_and_survive_restart(guarded_proxy, failure):
    cfg, directory, token, _ = guarded_proxy
    calls = []
    def upstream(request):
        calls.append(request)
        assert json.loads((directory / "proxy.json").read_text())["calls"] == len(calls)
        if failure == "transport":
            raise httpx.ConnectError("offline failure", request=request)
        return httpx.Response(503, json={"error": "unavailable"})
    headers = {"Authorization": f"Bearer {token}"}
    for route in ("llm", "vision"):
        # Recreate the ASGI application between attempts as well as after exhaustion.
        client = client_for(cfg, upstream)
        response = client.post(f"/{route}/chat/completions", headers=headers, json={})
        assert response.status_code == (502 if failure == "transport" else 503)
        assert graph_token(directory, cfg) == token
    response = client_for(cfg, upstream).post("/vision/chat/completions", headers=headers, json={})
    assert response.status_code == 429
    assert response.json() == {"error": "graph call limit exhausted"}
    state = json.loads((directory / "proxy.json").read_text())
    assert state["calls"] == 2 and state["tokens"] == 0 and state["usd"] == 0
    assert len(calls) == 2


@pytest.mark.parametrize(("elapsed", "status"), [(29.999, 200), (30, 200), (30.001, 429)])
def test_runtime_exact_boundary(guarded_proxy, elapsed, status):
    cfg, directory, token, now = guarded_proxy
    now[0] += elapsed
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    response = client_for(cfg, upstream).post(
        "/llm/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={})
    assert response.status_code == status
    if status == 429:
        assert response.json() == {"error": "graph runtime limit exhausted"}
    assert len(calls) == (1 if status == 200 else 0)
    assert json.loads((directory / "proxy.json").read_text())["calls"] == len(calls)


def test_runtime_checked_after_lock_acquisition(guarded_proxy):
    cfg, directory, token, now = guarded_proxy
    calls = []
    async def upstream(request):
        calls.append(request)
        await asyncio.sleep(0.01)
        now[0] = 1030.001
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    async def scenario():
        app = create_proxy(cfg, transport=httpx.MockTransport(upstream))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
            responses = await asyncio.gather(*[
                client.post(f"/{route}/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={})
                for route in ("llm", "vision")])
        assert sorted(response.status_code for response in responses) == [200, 429]
        rejected = next(response for response in responses if response.status_code == 429)
        assert rejected.json() == {"error": "graph runtime limit exhausted"}
    asyncio.run(scenario())
    assert len(calls) == 1
    assert json.loads((directory / "proxy.json").read_text())["calls"] == 1


def test_runtime_keeps_original_provisioning_time(guarded_proxy):
    cfg, directory, token, now = guarded_proxy
    now[0] = 1010
    (directory / "status.json").write_text(json.dumps({"provisioned_at": now[0]}))
    assert graph_token(directory, replace(cfg, graph_max_seconds=3000)) == token
    now[0] = 1030.001
    def unexpected(request):
        pytest.fail("expired graph reached upstream after re-provisioning")
    response = client_for(cfg, unexpected).post(
        "/vision/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={})
    assert response.status_code == 429
    assert response.json() == {"error": "graph runtime limit exhausted"}
    assert json.loads((directory / "proxy.json").read_text())["provisioned_at"] == 1000


@pytest.mark.parametrize("original", [980, None])
@pytest.mark.parametrize("migrate_on_provision", [False, True])
def test_legacy_state_migrates_once(guarded_proxy, original, migrate_on_provision):
    cfg, directory, token, now = guarded_proxy
    path = directory / "proxy.json"
    state = json.loads(path.read_text())
    for key in ("max_calls", "max_seconds", "calls", "provisioned_at"):
        del state[key]
    state.update(tokens=3, usd=0.000005)
    path.write_text(json.dumps(state))
    if original is not None:
        (directory / "status.json").write_text(json.dumps({"provisioned_at": original}))
    if migrate_on_provision:
        assert graph_token(directory, cfg) == token
        (directory / "status.json").write_text(json.dumps({"provisioned_at": now[0]}))
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    headers = {"Authorization": f"Bearer {token}"}
    assert client_for(cfg, upstream).post("/llm/chat/completions", headers=headers, json={}).status_code == 200
    migrated = json.loads(path.read_text())
    started = 1000 if original is None else original
    assert migrated["provisioned_at"] == started
    assert migrated["max_calls"] == 2 and migrated["max_seconds"] == 30
    assert migrated["calls"] == 1 and migrated["tokens"] == 4
    assert migrated["usd"] >= state["usd"]
    now[0] = started + 30.001
    (directory / "status.json").write_text(json.dumps({"provisioned_at": now[0]}))
    assert graph_token(directory, cfg) == token
    response = client_for(cfg, upstream).post("/vision/chat/completions", headers=headers, json={})
    assert response.status_code == 429
    assert response.json() == {"error": "graph runtime limit exhausted"}
    assert json.loads(path.read_text())["provisioned_at"] == started
    assert len(calls) == 1


def test_expired_legacy_state_is_migrated_before_rejection(guarded_proxy):
    cfg, directory, token, _ = guarded_proxy
    path = directory / "proxy.json"
    state = json.loads(path.read_text())
    for key in ("max_calls", "max_seconds", "calls", "provisioned_at"):
        del state[key]
    path.write_text(json.dumps(state))
    (directory / "status.json").write_text(json.dumps({"provisioned_at": 900}))
    def unexpected(request):
        pytest.fail("expired legacy graph reached upstream")
    response = client_for(cfg, unexpected).post(
        "/llm/chat/completions", headers={"Authorization": f"Bearer {token}"}, json={})
    assert response.status_code == 429
    assert response.json() == {"error": "graph runtime limit exhausted"}
    migrated = json.loads(path.read_text())
    assert migrated["provisioned_at"] == 900 and migrated["calls"] == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize(("field", "value"), [
    ("graph_max_calls", 0), ("graph_max_calls", -1),
    ("graph_max_seconds", 0), ("graph_max_seconds", -1),
    ("graph_max_seconds", float("inf")), ("graph_max_seconds", float("nan")),
])
def test_invalid_guard_caps_do_not_create_proxy_state(guarded_proxy, field, value):
    cfg, _, _, _ = guarded_proxy
    directory = cfg.data_dir / "new"
    directory.mkdir()
    with pytest.raises(ValueError, match="call and runtime caps"):
        graph_token(directory, replace(cfg, **{field: value}))
    assert not (directory / "proxy.json").exists()
