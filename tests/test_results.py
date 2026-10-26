import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app import fabric
from app.schema import Graph, result_error


@pytest.mark.parametrize("mode", ["limit", "error"])
def test_agent_failure_result(agent, mode):
    agent.GRAPH, agent.AGENT_ID = "g", "a"
    agent.MAX_TOOL_CALLS = 2
    agent.NC = AsyncMock()
    agent.llm_chat = AsyncMock(
        side_effect=RuntimeError("private upstream details") if mode == "error" else None,
        return_value={"choices": [{"message": {"tool_calls": [
            {"id": "call", "function": {"name": "unknown", "arguments": "{}"}}
        ]}}]},
    )
    asyncio.run(agent.handle_envelope(
        {"msg_id": "parent", "conv": "root", "hops": 2, "body": {"text": "go"}},
        "orc.g.in.a", True,
    ))
    assert agent.llm_chat.await_count == (1 if mode == "error" else 2)
    subject, raw = agent.NC.publish.call_args.args
    result = json.loads(raw)
    assert subject == "orc.g.out.a"
    assert result["kind"] == "result"
    assert result["body"]["ok"] is False
    assert "private" not in result["body"]["error"]
    assert (result["prev"], result["conv"], result["hops"]) == ("parent", "root", 3)


@pytest.mark.parametrize("body,schema,valid", [
    ({"answer": 2}, {"type": "object", "required": ["answer"], "properties": {"answer": {"type": "integer"}}}, True),
    ({}, {"required": ["answer"]}, False),
    ({"answer": True}, {"properties": {"answer": {"type": "integer"}}}, False),
    ([1, "bad"], {"type": "array", "items": {"type": "number"}}, False),
    ([1, 2.5], {"items": {"type": "number"}}, True),
    (True, {"enum": [1]}, False),
    ({"x": True}, {"enum": [{"x": 1}]}, False),
    (None, {"type": ["string", "null"]}, True),
    ({"extra": 1}, {"additionalProperties": False}, False),
])
def test_schema_boundaries(body, schema, valid):
    assert (result_error(body, schema) is None) is valid


@pytest.mark.parametrize("valid", [True, False])
def test_wait_observed_result_and_timeout(web_app, valid):
    directory = web_app.cfg.data_dir / "g"
    graph = Graph(name="g", agents=[{"id": "a"}], result_schema={
        "type": "object", "required": ["answer"],
        "properties": {"answer": {"type": "integer"}},
    })
    (directory / "graph.json").write_text(graph.model_dump_json(by_alias=True))

    async def run():
        await fabric.stop_observer("g")
        queue = asyncio.Queue()

        class Subscription:
            async def next_msg(self, timeout):
                return await queue.get()

        observer = asyncio.create_task(fabric._persist(Subscription(), directory, "g", {}))
        transport = httpx.ASGITransport(app=web_app.app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                waits = [asyncio.create_task(client.get("/api/graphs/g/result?conv=root&timeout=1")) for _ in range(2)]
                await asyncio.sleep(0)
                body = {"answer": 42 if valid else "wrong"}
                # Channel results and unrelated conversations must not satisfy these waiters.
                for subject, conv, kind in [("orc.g.ch.c", "root", "result"),
                                             ("orc.g.out.a", "other", "result"),
                                             ("orc.g.out.a", "root", "text"),
                                             ("orc.g.out.a", "root", "result")]:
                    await queue.put(SimpleNamespace(subject=subject, data=json.dumps({
                        "kind": kind, "conv": conv, "body": body, "valid": not valid,
                        "error": "forged", "msg_id": "answer",
                    }).encode()))
                for response in await asyncio.gather(*waits):
                    assert response.status_code == 200
                    result = response.json()["result"]
                    assert result["conv"] == "root" and result["kind"] == "result"
                    assert result["body"] == body and result["valid"] is valid
                    assert ("error" in result) is not valid
                cached = await client.get("/api/graphs/g/result?conv=root&timeout=0")
                assert cached.json()["result"]["body"] == body
                missing = await client.get("/api/graphs/g/result?conv=missing&timeout=0.001")
                assert missing.status_code == 408 and missing.json() == {"status": "timeout"}
                assert (await client.get("/api/graphs/g/result?timeout=301")).status_code == 400
                assert (await client.get("/api/graphs/g/result?timeout=nan")).status_code == 400
                assert (await client.get("/api/graphs/Upper/result")).status_code == 400
            rows = [json.loads(line) for line in (directory / "traffic.jsonl").read_text().splitlines()]
            assert rows[-1]["body"] == body and rows[-1]["valid"] is valid
        finally:
            observer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await observer
            await fabric.stop_observer("g")

    asyncio.run(run())
