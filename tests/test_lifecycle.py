import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest


def seed(main, name, state="live", started=900):
    directory = main.cfg.data_dir / name
    directory.mkdir(exist_ok=True)
    (directory / "graph.json").write_text(json.dumps({
        "name": name, "agents": [{"id": "a"}], "channels": [],
    }))
    (directory / "status.json").write_text(json.dumps({
        "state": state, "provisioned_at": started,
    }))
    return directory


@pytest.mark.parametrize("ttl", [0, 60])
def test_gc_selects_only_expired_live_graphs(web_app, monkeypatch, ttl):
    from app import provision

    main = web_app
    config = replace(main.cfg, graph_ttl_seconds=ttl)
    expired = seed(main, "old")
    seed(main, "old-new", started=990)
    seed(main, "boundary", started=940)
    seed(main, "draft", state="pending")
    commands = []
    monkeypatch.setattr(provision, "_run", lambda *args, **kw: commands.append(args) or "")
    monkeypatch.setattr(main, "time", SimpleNamespace(time=lambda: 1000))
    stop = AsyncMock()
    monkeypatch.setattr(main.fabric, "stop_observer", stop)
    asyncio.run(main.cleanup_expired_graphs(config))
    status = json.loads((expired / "status.json").read_text())
    assert status["state"] == ("expired" if ttl else "live")
    if ttl:
        stop.assert_awaited_once_with("old")
        assert ("docker", "rm", "-f", "orc-old-nats") in commands
        assert ("docker", "network", "rm", "orc-old-a") in commands
        assert ("docker", "volume", "rm", "orc-old-a-ws") in commands
        assert all("old-new" not in arg for command in commands for arg in command)
    else:
        assert commands == []
        stop.assert_not_awaited()


def test_concurrent_accept_cannot_exceed_capacity(web_app, monkeypatch):
    main = web_app
    monkeypatch.setattr(main, "cfg", replace(main.cfg, max_live_graphs=1))
    seed(main, "first", state="pending")
    seed(main, "second", state="pending")
    monkeypatch.setattr(main.fabric, "stop_observer", AsyncMock())
    monkeypatch.setattr(main.fabric, "start_observer", AsyncMock())

    def provision(graph, *args):
        (main.cfg.data_dir / graph.name / "status.json").write_text('{"state":"live"}')
        return {}

    monkeypatch.setattr(main, "provision", provision)

    async def scenario():
        monkeypatch.setattr(main, "_lifecycle_lock", asyncio.Lock())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(main.app), base_url="http://test") as client:
            responses = await asyncio.gather(*[
                client.post(f"/api/graphs/{name}/accept") for name in ("first", "second")
            ])
            assert sorted(r.status_code for r in responses) == [200, 429]
            live = "first" if responses[0].status_code == 200 else "second"
            assert (await client.post(f"/api/graphs/{live}/accept")).status_code == 200

    asyncio.run(scenario())
