import asyncio
import json
from types import SimpleNamespace

import pytest

from app import fabric


@pytest.mark.parametrize("subject,expected", [
    ("orc.g.out.a1", "a1"),
    ("orc.g.ch.c1", "a2"),
    ("orc.g.in.a1", None),
    ("orc.g.out.bad!", None),
    ("orc.g.ch.unknown", None),
    ("orc.other.out.a1", None),
])
def test_observer_uses_subject_sender(tmp_path, subject, expected):
    agents = tmp_path / "agents"
    agents.mkdir()
    envelope = {"src": "../../evil", "kind": "text", "body": {"text": "hello"}}

    class Subscription:
        delivered = False

        async def next_msg(self, timeout):
            if self.delivered:
                raise asyncio.CancelledError
            self.delivered = True
            return SimpleNamespace(subject=subject, data=json.dumps(envelope).encode())

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await fabric._persist(Subscription(), tmp_path, "g", {"c1": "a2"})

    asyncio.run(run())
    assert sorted(p.name for p in agents.iterdir()) == ([f"{expected}.md"] if expected else [])
    if expected:
        assert f"{expected} via" in (agents / f"{expected}.md").read_text()
    assert not (tmp_path.parent / "evil.md").exists()
