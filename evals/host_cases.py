"""Offline host/API probes; every provision command is captured, never executed.

Future-facing assumptions (10–13), deliberately small and explicit:
* Agent.result_schema is JSON Schema for a result envelope's body. The observer
  persists result_valid: bool alongside that envelope, including invalid results.
* POST .../prompt accepts wait_for_result=true and timeout_seconds. It waits on
  observer-persisted traffic for the prompt conversation: 200 + result envelope
  for a valid result, 422 + flagged result for an invalid one, 504 on timeout.
* cleanup_expired_graphs(cfg) is a public one-shot sync/async cleanup hook;
  cfg.graph_ttl_seconds ages live graphs from status.provisioned_at; zero disables.
* cfg.max_live_graphs limits admission at /accept, not drafts or reaccepting live
  graphs. Rollback removes newly created containers/networks/volumes; updating
  a graph also tears down agents removed from its previously persisted spec.
Absent schema fields/public hooks score FAIL with reason "not implemented".
Lifecycle probes exercise provision and report missing only if no cleanup is attempted.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import stat
import time
from types import SimpleNamespace

import httpx


def _graph(name="g", agents=("a", "b")):
    from app.schema import Graph
    return Graph(name=name, agents=[{"id": aid} for aid in agents], channels=(
        [{"id": "ab", "from": "a", "to": "b"}] if {"a", "b"} <= set(agents) else []
    ))


def _seed(ctx, graph=None, state="live", started=None):
    graph = graph or _graph()
    started = time.time() if started is None else started
    directory = ctx.path / graph.name
    (directory / "agents").mkdir(parents=True, exist_ok=True)
    (directory / "graph.json").write_text(graph.model_dump_json(by_alias=True))
    (directory / "status.json").write_text(json.dumps({
        "state": state, "created": started, "provisioned_at": started,
    }))
    for agent in graph.agents:
        (directory / "agents" / f"{agent.id}.md").write_text(f"# {agent.id}\n")
    return directory


def _client(app, token=None):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://eval.invalid",
        headers={"Authorization": f"Bearer {token}"} if token else {},
        trust_env=False,
    )


def _main(ctx, **overrides):
    main = ctx.main()
    cfg = ctx.config(api_token="eval-api-token", **overrides)
    main.cfg = cfg  # Context returns a disposable, unregistered module instance.
    return main, cfg


def _capture_provision(ctx, main=None, fail_name=None):
    from app import provision
    calls = []
    failed = [False]

    def run(*args, **kwargs):
        calls.append(tuple(args))
        if (fail_name and args[:3] == ("docker", "run", "-d")
                and "--name" in args and args[args.index("--name") + 1] == fail_name
                and not failed[0]):
            failed[0] = True
            raise provision.ProvisionError("injected offline container failure")
        return "image" if args[:3] == ("docker", "image", "inspect") else ""

    ctx.patch("app.provision._run", run)
    ctx.patch("app.provision.free_port", lambda: 18765)
    if main is not None:
        async def noop(*args, **kwargs):
            return None
        ctx.patch("app.fabric.start_observer", noop)
        ctx.patch("app.fabric.stop_observer", noop)
    return calls


async def provision_credentials(ctx):
    cfg = ctx.config(llm_api_key="eval-upstream-text", vision_api_key="eval-upstream-vision")
    graph = _graph()
    ctx.provision(graph, cfg=cfg)
    state_path = ctx.path / "g" / "proxy.json"
    state = json.loads(state_path.read_text())
    runs = [args for args in ctx.commands
            if args[:3] == ("docker", "run", "-d") and args[-1] == cfg.agent_image]
    ctx.require(len(runs) == 2, "real provision must launch both agents")
    for args in runs:
        env = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "-e")
        for route, prefix in (("llm", "LLM"), ("vision", "VISION")):
            ctx.require(env[f"{prefix}_BASE_URL"] ==
                        f"http://host.docker.internal:{cfg.proxy_port}/{route}",
                        "agent must route requests through the host proxy")
            ctx.require(env[f"{prefix}_API_KEY"] == state["token"],
                        "agent must receive the persisted graph token")
        ctx.require("host.docker.internal:host-gateway" in args, "proxy host gateway missing")
        ctx.require("--cap-drop" in args and "ALL" in args, "agent capability restriction missing")
    ctx.require(all(key not in repr(ctx.commands) for key in
                    (cfg.llm_api_key, cfg.vision_api_key, cfg.api_token) if key),
                "provider/API credentials leaked into Docker arguments")
    ctx.require(stat.S_IMODE(state_path.stat().st_mode) == 0o600, "graph credential file is not private")
    ctx.provision(graph, cfg=cfg)
    ctx.require(json.loads(state_path.read_text())["token"] == state["token"],
                "reprovisioning replaced the graph credential")


async def _proxy_cap(ctx, cap):
    from app import proxy
    now = [1000.0]
    ctx.patch("app.proxy.time", SimpleNamespace(time=lambda: now[0]))
    cfg = ctx.config(graph_token_budget=10 if cap == "tokens" else 1000,
                     graph_usd_budget=0.00001 if cap == "usd" else 0,
                     graph_max_calls=2 if cap == "calls" else 100,
                     graph_max_seconds=30, price_in_per_m=1, price_out_per_m=3)
    directory = _seed(ctx, started=1000)
    token = proxy.graph_token(directory, cfg)
    state_path = directory / "proxy.json"
    requests = []

    def upstream(request):
        requests.append(request)
        ctx.require(json.loads(state_path.read_text())["calls"] == len(requests),
                    "upstream attempt must be durable before forwarding")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}})

    def app():
        return proxy.create_proxy(cfg, transport=httpx.MockTransport(upstream))

    admitted = 1 if cap in ("usd", "runtime") else 2
    async with _client(app(), token) as client:
        for index in range(admitted):
            if cap == "runtime":
                now[0] = 1030.0  # The exact runtime boundary is still admitted.
            response = await client.post(f"/{'llm' if index == 0 else 'vision'}/chat/completions", json={"messages": []})
            ctx.require(response.status_code == 200, f"{cap}: request below cap rejected")
    before = json.loads(state_path.read_text())
    ctx.require(before["tokens"] == admitted * 6 and abs(before["usd"] - admitted * .00001) < 1e-12,
                "shared token/USD accounting was not persisted")
    ctx.require(proxy.graph_token(directory, cfg) == token, "restart changed token")
    if cap == "runtime":
        now[0] = 1030.001
    async with _client(app(), token) as client:  # New app, no old locks/cache.
        response = await client.post("/vision/chat/completions", json={"messages": []})
    ctx.require(response.status_code == 429, f"{cap}: exhausted graph accepted after restart")
    expected = ("graph call limit exhausted" if cap == "calls" else
                "graph runtime limit exhausted" if cap == "runtime" else "graph budget exhausted")
    ctx.require(response.json().get("error") == expected, f"{cap}: wrong rejection reason")
    ctx.require(len(requests) == admitted, "rejected request reached upstream")
    ctx.require(json.loads(state_path.read_text()) == before, "rejected request changed persisted usage")


async def proxy_token_cap(ctx):
    await _proxy_cap(ctx, "tokens")


async def proxy_usd_cap(ctx):
    await _proxy_cap(ctx, "usd")


async def proxy_call_cap(ctx):
    await _proxy_cap(ctx, "calls")


async def proxy_runtime_cap(ctx):
    await _proxy_cap(ctx, "runtime")


async def api_authentication(ctx):
    from app.proxy import create_proxy, graph_token
    main, cfg = _main(ctx)
    directory = _seed(ctx)
    token = graph_token(directory, cfg)
    async with _client(main.app) as client:
        for header in (None, "Bearer wrong", "Basic eval-api-token", f"Bearer {token}"):
            headers = {} if header is None else {"Authorization": header}
            for method, path in (("GET", "/api/graphs"), ("POST", "/api/graphs/g/accept"),
                                 ("DELETE", "/api/graphs/g"), ("GET", "/api/unknown")):
                response = await client.request(method, path, headers=headers)
                ctx.require(response.status_code == 401, "missing/wrong/graph token bypassed API auth")
                ctx.require(response.headers.get("www-authenticate") == "Bearer", "missing Bearer challenge")
        response = await client.get("/api/graphs/g", headers={"Authorization": f"Bearer {cfg.api_token}"})
        ctx.require(response.status_code == 200 and response.json()["name"] == "g", "right API token rejected")
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    async with _client(create_proxy(cfg, transport=httpx.MockTransport(upstream))) as client:
        for header in (None, "Bearer wrong", f"Bearer {cfg.api_token}"):
            response = await client.post("/llm/chat/completions", json={},
                headers={} if header is None else {"Authorization": header})
            ctx.require(response.status_code == 401, "proxy accepted absent/wrong/API credential")
        response = await client.post("/llm/chat/completions", json={}, headers={"Authorization": f"Bearer {token}"})
        ctx.require(response.status_code == 200, "proxy rejected its graph credential")
    ctx.require(len(requests) == 1, "unauthorized proxy request reached upstream")


async def api_auth_default_closed(ctx):
    main = ctx.main()
    for allowed, expected in ((False, 401), (True, 200)):
        main.cfg = ctx.config(api_token="", allow_no_auth=allowed)
        async with _client(main.app) as client:
            response = await client.get("/api/graphs")
            ctx.require(response.status_code == expected, "empty-token auth policy is not fail closed/explicit opt-out")


async def _observe(main, directory, messages):
    class Subscription:
        def __init__(self):
            self.remaining = iter(messages)
        async def next_msg(self, timeout=None):
            try:
                subject, envelope = next(self.remaining)
            except StopIteration:
                raise asyncio.CancelledError
            return SimpleNamespace(subject=subject, data=json.dumps(envelope).encode())
    try:
        await main.fabric._persist(Subscription(), directory, "g", {"ab": "a"})
    except asyncio.CancelledError:
        pass


async def observer_subject_identity(ctx):
    main, cfg = _main(ctx)
    directory = _seed(ctx)
    entries = [
        ("orc.g.out.a", "user", "a", "out-user-spoof"),
        ("orc.g.out.a", "b", "a", "out-peer-spoof"),
        ("orc.g.ch.ab", "b", "a", "channel-peer-spoof"),
        ("orc.g.ch.ab", "user", "a", "channel-user-spoof"),
        ("orc.g.in.b", "a", "user", "in-agent-spoof"),
        ("orc.g.ch.unknown", "b", None, "unknown-channel-spoof"),
        ("orc.other.out.b", "b", None, "foreign-graph-spoof"),
        ("orc.g.out.bad!", "../../escape", "bad!", "invalid-slug-spoof"),
    ]
    await _observe(main, directory, [(subject, {"src": claimed, "kind": "text", "ch": "ch.ab",
        "body": {"text": text}, "ts": "eval"}) for subject, claimed, _, text in entries])
    async with _client(main.app, cfg.api_token) as client:
        traffic = (await client.get("/api/graphs/g/traffic")).json()["lines"]
        markdown_a = (await client.get("/api/graphs/g/agents/a")).text
        markdown_b = (await client.get("/api/graphs/g/agents/b")).text
    ctx.require([row["src"] for row in traffic] == [row[2] for row in entries],
                "observer trusted envelope src instead of broker subject")
    for _, _, sender, marker in entries:
        ctx.require((marker in markdown_a) == (sender == "a"), "sender markdown contains spoofed/missing entry")
        ctx.require(marker not in markdown_b, "spoofed identity wrote another agent's markdown")
    ctx.require(sorted(p.name for p in (directory / "agents").iterdir()) == ["a.md", "b.md"],
                "untrusted subject/src created an extra markdown file")
    ctx.require(not (ctx.path / "escape.md").exists(), "observer path escaped graph directory")


async def invalid_slug_http(ctx):
    main, cfg = _main(ctx)
    _seed(ctx)
    async with _client(main.app, cfg.api_token) as client:
        for bad in ("Upper", "bad!", "a" * 33, "a%0A", "%2E%2E", "%2e%2e%5cescape"):
            for method, suffix in (("GET", ""), ("POST", "/accept"), ("GET", "/traffic"),
                                   ("DELETE", ""), ("GET", "/agents/a"), ("POST", "/agents/a/prompt")):
                response = await client.request(method, f"/api/graphs/{bad}{suffix}", json={"prompt": "probe"})
                ctx.require(response.status_code == 400, f"bad graph slug {bad!r} did not return HTTP400")
            for method, suffix in (("GET", ""), ("POST", "/prompt")):
                response = await client.request(method, f"/api/graphs/g/agents/{bad}{suffix}", json={"prompt": "probe"})
                ctx.require(response.status_code == 400, f"bad agent slug {bad!r} did not return HTTP400")
    ctx.require((ctx.path / "g" / "agents" / "a.md").read_text() == "# a\n", "invalid paths mutated agent state")


_RESULT_SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
                  "required": ["answer"], "additionalProperties": False}


def _result_graph():
    from app.schema import Agent, Graph
    if "result_schema" not in Agent.model_fields:
        raise NotImplementedError("not implemented")
    return Graph(name="g", agents=[{"id": "a", "result_schema": _RESULT_SCHEMA}], channels=[])


async def _result_flag(ctx, valid):
    graph = _result_graph()  # Existing schema must actually retain the contract.
    main, cfg = _main(ctx)
    directory = _seed(ctx, graph)
    body = {"answer": 42 if valid else "not-an-integer"}
    await _observe(main, directory, [("orc.g.out.a", {"v": 1, "msg_id": "result-1", "conv": "prompt-1",
        "src": "a", "ch": "out.a", "kind": "result", "body": body})])
    async with _client(main.app, cfg.api_token) as client:
        response = await client.get("/api/graphs/g/traffic")
    rows = response.json()["lines"]
    ctx.require(len(rows) == 1 and rows[0].get("result_valid") is valid,
                "observer must preserve and explicitly flag result schema validity")
    ctx.require(rows[0]["body"] == body, "result validation altered/discarded the result body")


async def result_valid_flag(ctx):
    await _result_flag(ctx, True)


async def result_invalid_flag(ctx):
    await _result_flag(ctx, False)


async def _wait_result(ctx, outcome):
    main, cfg = _main(ctx)
    directory = _seed(ctx, _graph(agents=("a",)))
    # Keep waiting probes independent of whether Agent currently retains schemas.
    # The persisted schema is the proposed consumer contract, not a fake validator.
    stored = json.loads((directory / "graph.json").read_text())
    stored["agents"][0]["result_schema"] = _RESULT_SCHEMA
    (directory / "graph.json").write_text(json.dumps(stored))
    sent = []

    async def publish(subject, raw):
        prompt = json.loads(raw)
        sent.append((subject, prompt))
        if outcome != "timeout":
            await _observe(main, directory, [("orc.g.out.a", {"v": 1, "msg_id": "result-1",
                "conv": prompt["conv"], "prev": prompt["msg_id"], "src": "a", "ch": "out.a",
                "kind": "result", "body": {"answer": 42 if outcome == "valid" else "wrong"}})])

    async def connection(*args):
        return SimpleNamespace(publish=publish)
    ctx.patch("app.fabric.get_conn", connection)
    async with _client(main.app, cfg.api_token) as client:
        response = await client.post("/api/graphs/g/agents/a/prompt", json={
            "prompt": "return an answer", "wait_for_result": True, "timeout_seconds": .01,
        })
    if (response.status_code == 200 and response.json().get("ok") is True
            and "result" not in response.json()):
        # The wait flag returned only an immediate acknowledgement, not a result.
        raise NotImplementedError("not implemented")
    ctx.require(len(sent) == 1 and sent[0][0] == "orc.g.in.a", "wait must publish exactly one ordinary prompt")
    expected = {"valid": 200, "invalid": 422, "timeout": 504}[outcome]
    ctx.require(response.status_code == expected, f"wait-for-result {outcome}: expected HTTP{expected}")
    if outcome != "timeout":
        result = response.json().get("result", {})
        ctx.require(result.get("kind") == "result" and result.get("result_valid") is (outcome == "valid"),
                    "wait must return the flagged result envelope")
        ctx.require(result.get("conv") == sent[0][1]["conv"], "wait returned another conversation's result")


async def wait_valid_result(ctx):
    await _wait_result(ctx, "valid")


async def wait_invalid_result(ctx):
    await _wait_result(ctx, "invalid")


async def wait_result_timeout(ctx):
    await _wait_result(ctx, "timeout")


async def _ttl(ctx, mode):
    main, cfg = _main(ctx)
    cleanup = getattr(main, "cleanup_expired_graphs", None)
    if not callable(cleanup):
        raise NotImplementedError("not implemented")
    cfg = SimpleNamespace(**{**vars(cfg), "graph_ttl_seconds": 0 if mode == "disabled" else 60})
    main.cfg = cfg
    main.time = SimpleNamespace(time=lambda: 1000.0)
    directory = _seed(ctx, started=900 if mode != "fresh" else 990)
    commands = _capture_provision(ctx, main)
    try:
        inspect.signature(cleanup).bind(cfg)
    except TypeError as error:
        raise AssertionError("cleanup_expired_graphs must accept explicit cfg") from error
    result = cleanup(cfg)
    if inspect.isawaitable(result):
        await result
    async with _client(main.app, cfg.api_token) as client:
        response = await client.get("/api/graphs/g")
    if mode == "expired":
        ctx.require(response.status_code == 404 and not directory.exists(), "expired live graph was not removed")
        ctx.require(any(args[:3] == ("docker", "rm", "-f") for args in commands), "expired containers not torn down")
        ctx.require(any(args[:3] == ("docker", "volume", "rm") for args in commands), "expired volumes not removed")
    else:
        ctx.require(response.status_code == 200 and directory.exists(), "fresh/TTL-disabled graph was removed")
        ctx.require(not commands, "fresh/TTL-disabled graph triggered Docker cleanup")


async def ttl_expired(ctx):
    await _ttl(ctx, "expired")


async def ttl_fresh(ctx):
    await _ttl(ctx, "fresh")


async def ttl_disabled(ctx):
    await _ttl(ctx, "disabled")


async def graph_capacity(ctx):
    main, cfg = _main(ctx)
    if not hasattr(cfg, "max_live_graphs"):
        raise NotImplementedError("not implemented")
    cfg = SimpleNamespace(**{**vars(cfg), "max_live_graphs": 1})
    main.cfg = cfg
    _seed(ctx, _graph("live"), state="live")
    _seed(ctx, _graph("draft"), state="pending")
    commands = _capture_provision(ctx, main)
    async with _client(main.app, cfg.api_token) as client:
        response = await client.post("/api/graphs/draft/accept")
        ctx.require(response.status_code == 429, "over-cap acceptance must return HTTP429")
        ctx.require(not commands, "over-cap graph started provisioning")
        ctx.require((await client.get("/api/graphs/draft")).json()["status"] == "pending", "rejection changed draft state")
        response = await client.post("/api/graphs/live/accept")
        ctx.require(response.status_code == 200 and response.json()["status"] == "live", "cap rejected an existing live graph")
        ctx.require(any(args[:3] == ("docker", "run", "-d") for args in commands), "live reaccept did not actually provision")


async def failed_provision_cleanup(ctx):
    main, cfg = _main(ctx)
    _seed(ctx, state="pending")
    commands = _capture_provision(ctx, main, fail_name="orc-g-b")
    async with _client(main.app, cfg.api_token) as client:
        response = await client.post("/api/graphs/g/accept")
    ctx.require(response.status_code == 500, "injected provision failure must reach API as HTTP500")
    failed_index = next(i for i, args in enumerate(commands)
                        if args[:3] == ("docker", "run", "-d") and "orc-g-b" in args)
    cleanup = commands[failed_index + 1:]
    if not any(args[:2] == ("docker", "rm") or args[:3] in
               (("docker", "network", "rm"), ("docker", "volume", "rm")) for args in cleanup):
        raise NotImplementedError("not implemented")
    for resource in ("orc-g-nats", "orc-g-a", "orc-g-b"):
        ctx.require(any(args[:3] == ("docker", "rm", "-f") and resource in args for args in cleanup),
                    f"failed provision did not remove container {resource}")
    for aid in ("a", "b"):
        for kind, resource in (("network", f"orc-g-{aid}"), ("volume", f"orc-g-{aid}-ws")):
            ctx.require(any(args[:3] == ("docker", kind, "rm") and resource in args for args in cleanup),
                        f"failed provision leaked {kind} {resource}")
    status = json.loads((ctx.path / "g" / "status.json").read_text()) if (ctx.path / "g" / "status.json").exists() else {}
    ctx.require(status.get("state") != "live", "failed provision marked the graph live")


async def removed_agent_teardown(ctx):
    from app import provision
    cfg = ctx.config()
    old = _graph()
    directory = _seed(ctx, old)
    ctx.provision(old, cfg=cfg)
    commands = _capture_provision(ctx)
    provision.provision(_graph(agents=("a",)), cfg.data_dir, cfg.agent_image, cfg.nats_image, directory, cfg)
    removal = [args for args in commands if args[:2] == ("docker", "rm") or
               args[:3] in (("docker", "network", "rm"), ("docker", "volume", "rm"))]
    if not any("orc-g-b" in args or "orc-g-b-ws" in args for args in removal):
        raise NotImplementedError("not implemented")
    for prefix, name in ((("docker", "rm", "-f"), "orc-g-b"),
                         (("docker", "network", "rm"), "orc-g-b"),
                         (("docker", "volume", "rm"), "orc-g-b-ws")):
        ctx.require(any(args[:3] == prefix and name in args for args in removal),
                    f"removed agent resource not torn down: {name}")
    ctx.require(not any(args[:3] == ("docker", "run", "-d") and "orc-g-b" in args for args in commands),
                "removed agent was relaunched")


async def forced_proxy_routing(ctx):
    from app.proxy import create_proxy, graph_token
    cfg = ctx.config(llm_base_url="https://text.invalid/v1", vision_base_url="https://vision.invalid/v2",
                     llm_model="eval-text-fixed", vision_model="eval-vision-fixed",
                     llm_api_key="eval-text-key", vision_api_key="eval-vision-key")
    token = graph_token(_seed(ctx), cfg)
    requests = []
    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": 0}})
    async with _client(create_proxy(cfg, transport=httpx.MockTransport(upstream)), token) as client:
        for route in ("llm", "vision"):
            response = await client.post(f"/{route}/chat/completions", json={"messages": [],
                "model": "attacker-model", "n": 99, "stream": False,
                "base_url": "https://attacker.invalid", "url": "https://attacker.invalid",
                "api_key": "attacker-key", "headers": {"Authorization": "Bearer attacker-key"}})
            ctx.require(response.status_code == 200, "routed proxy request failed")
    ctx.require(len(requests) == 2, "proxy forwarded an unexpected number of requests")
    for request, base, model, key in zip(requests,
            (cfg.llm_base_url, cfg.vision_base_url), (cfg.llm_model, cfg.vision_model),
            (cfg.llm_api_key, cfg.vision_api_key)):
        payload = json.loads(request.content)
        ctx.require(str(request.url) == base + "/chat/completions", "caller changed upstream URL")
        ctx.require(request.headers["authorization"] == f"Bearer {key}", "caller changed upstream credentials")
        ctx.require(payload["model"] == model and payload["n"] == 1 and payload["stream"] is False,
                    "caller changed fixed model/request multiplicity")
        ctx.require(not ({"api_key", "base_url", "url", "headers"} & payload.keys()), "routing fields reached upstream payload")


CASES = [
    ("1", "provision uses proxy URLs and graph-only credentials", provision_credentials),
    ("4", "token budget shared across routes survives restart", proxy_token_cap),
    ("4", "USD budget shared across routes survives restart", proxy_usd_cap),
    ("4", "durable call admission and cap survive restart", proxy_call_cap),
    ("4", "runtime exact boundary and expiry survive restart", proxy_runtime_cap),
    ("5", "API and proxy reject absent wrong and cross-domain tokens", api_authentication),
    ("5", "API empty-token policy fails closed unless explicitly disabled", api_auth_default_closed),
    ("6+B", "observer subject identity defeats user peer and path spoofing", observer_subject_identity),
    ("7", "bad graph and agent slugs reject traversal with HTTP400", invalid_slug_http),
    ("10", "valid result body is explicitly flagged valid", result_valid_flag),
    ("10", "invalid result body is retained and flagged invalid", result_invalid_flag),
    ("10", "wait-for-result returns matching valid envelope", wait_valid_result),
    ("10", "wait-for-result rejects a matching invalid result", wait_invalid_result),
    ("10", "wait-for-result returns HTTP504 on timeout", wait_result_timeout),
    ("11", "TTL removes expired graph and Docker resources", ttl_expired),
    ("11", "TTL preserves fresh live graph", ttl_fresh),
    ("11", "zero TTL disables cleanup", ttl_disabled),
    ("12", "live graph cap rejects drafts but allows live reaccept", graph_capacity),
    ("13", "failed provision rolls back all newly created resources", failed_provision_cleanup),
    ("13", "reprovision tears down agents removed from graph", removed_agent_teardown),
    ("D", "proxy forces configured upstream URL credentials and model", forced_proxy_routing),
]
