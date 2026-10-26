"""Behavior tests for the graph domain model and its NATS permission artifacts.

Outcome-focused: these assert what the model does for consumers — valid graphs pass,
invalid ones are rejected with the reason, and the generated permissions grant exactly
the drawn edges (blindness is generated, not enforced elsewhere).
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.schema import Graph


def two_agents() -> dict:
    return {
        "name": "ship-docs",
        "agents": [
            {"id": "researcher", "name": "Researcher", "system": "research"},
            {"id": "writer", "name": "Writer", "system": "write"},
        ],
        "channels": [
            {"id": "c1", "from": "researcher", "to": "writer", "name": "drafts"}
        ],
    }


def test_valid_graph_builds():
    g = Graph.model_validate(two_agents())
    assert [a.id for a in g.agents] == ["researcher", "writer"]
    assert g.channels[0].filter.max_bytes == 16384


def test_empty_agents_rejected():
    with pytest.raises(ValidationError):
        Graph.model_validate({"name": "x", "agents": []})


def test_channel_to_unknown_agent_rejected():
    spec = two_agents()
    spec["channels"][0]["to"] = "ghost"
    with pytest.raises(ValidationError, match="unknown agent"):
        Graph.model_validate(spec)


def test_self_loop_rejected():
    spec = two_agents()
    spec["channels"][0]["to"] = "researcher"
    with pytest.raises(ValidationError, match="loops back"):
        Graph.model_validate(spec)


def test_duplicate_channel_id_rejected():
    spec = two_agents()
    spec["channels"].append(dict(spec["channels"][0]))
    with pytest.raises(ValidationError, match="duplicate channel id"):
        Graph.model_validate(spec)


def test_bad_slugs_rejected():
    for bad in ["Upper", "has space", "-lead", "a" * 40, ""]:
        spec = two_agents()
        spec["agents"][0]["id"] = bad
        with pytest.raises(ValidationError):
            Graph.model_validate(spec)


def test_unknown_tool_or_kind_rejected():
    spec = two_agents()
    spec["agents"][0]["tools"] = ["sudo"]
    with pytest.raises(ValidationError, match="tools"):
        Graph.model_validate(spec)
    spec = two_agents()
    spec["channels"][0]["filter"] = {"kinds": ["text", "binary"]}
    with pytest.raises(ValidationError, match="kinds"):
        Graph.model_validate(spec)


def test_two_way_conversation_is_two_channels():
    spec = two_agents()
    spec["channels"].append(
        {"id": "c2", "from": "writer", "to": "researcher", "name": "notes"}
    )
    g = Graph.model_validate(spec)
    assert len(g.channels) == 2


def test_graph_roundtrip_through_json():
    spec = two_agents()
    g = Graph.model_validate(spec)
    dumped = json.loads(g.model_dump_json(by_alias=True))
    assert dumped["channels"][0]["from"] == "researcher"
    again = Graph.model_validate(dumped)  # stored canonical JSON reparses
    assert again.channels[0].from_ == "researcher"
    legacy = two_agents()  # files written before the by_alias fix used from_
    legacy["channels"][0]["from_"] = legacy["channels"][0].pop("from")
    assert Graph.model_validate(legacy).channels[0].from_ == "researcher"


def test_permissions_grant_exactly_the_drawn_edges():
    g = Graph.model_validate(two_agents())
    block, pw = g.nats_users()

    researcher = next(u for u in json.loads(pw_key(block)) if u["user"] == "orc-ship-docs-researcher")
    # researcher publishes only on c1 and its own out; subscribes only on its own in
    assert researcher["permissions"]["publish"] == ["orc.ship-docs.ch.c1", "orc.ship-docs.out.researcher"]
    assert researcher["permissions"]["subscribe"] == ["orc.ship-docs.in.researcher"]

    writer = next(u for u in json.loads(pw_key(block)) if u["user"] == "orc-ship-docs-writer")
    assert writer["permissions"]["publish"] == ["orc.ship-docs.out.writer"]
    assert writer["permissions"]["subscribe"] == ["orc.ship-docs.ch.c1", "orc.ship-docs.in.writer"]

    user = next(u for u in json.loads(pw_key(block)) if u["user"] == "orc-ship-docs-user")
    assert user["permissions"]["publish"] == ["orc.ship-docs.in.>"]
    assert user["permissions"]["subscribe"] == ["orc.ship-docs.>.>"]

    assert set(pw) == {"orc-ship-docs-researcher", "orc-ship-docs-writer", "orc-ship-docs-user"}


def pw_key(block: str) -> str:
    # the users array is embedded in the conf block; extract it for assertions
    inner = block.split("users = [", 1)[1].rsplit("]", 1)[0]
    return f"[{inner}]"


def test_three_agent_routing_only_touches_endpoints():
    spec = two_agents()
    spec["agents"].append({"id": "critic", "name": "Critic", "system": "review"})
    spec["channels"] = [
        {"id": "c1", "from": "researcher", "to": "writer"},
        {"id": "c2", "from": "writer", "to": "critic"},
    ]
    g = Graph.model_validate(spec)
    block, pw = g.nats_users()
    users = {u["user"]: u for u in json.loads(pw_key(block))}

    # critic never touches c1 in any direction
    assert "orc.ship-docs.ch.c1" not in users["orc-ship-docs-critic"]["permissions"]["publish"]
    assert "orc.ship-docs.ch.c1" not in users["orc-ship-docs-critic"]["permissions"]["subscribe"]
    # writer is the midpoint: subscribes c1, publishes c2
    assert "orc.ship-docs.ch.c1" in users["orc-ship-docs-writer"]["permissions"]["subscribe"]
    assert "orc.ship-docs.ch.c2" in users["orc-ship-docs-writer"]["permissions"]["publish"]
