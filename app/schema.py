"""Single source of truth for the compiled graph spec and its enforcement artifacts.

A graph is the accepted, compiled form of a drawing: agents, channels, filters.
Everything derived — NATS subjects, per-agent permissions, the broker users file —
is generated here so no other module re-derives names or permissions.
(model-the-domain: the data structure is the domain; boundary-discipline: these
types are the only cross-module contract.)
"""

from __future__ import annotations

import json
import re
import secrets

from pydantic import BaseModel, Field, field_validator, model_validator

SLUG = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
KNOWN_TOOLS = ("workspace", "computer_use")
KNOWN_KINDS = ("text", "artifact", "result")


class Agent(BaseModel):
    id: str
    name: str = ""
    system: str = ""
    tools: list[str] = ["workspace"]
    memory_mb: int = Field(default=512, ge=64, le=2048)
    cpus: float = Field(default=1.0, ge=0.25, le=4.0)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError(f"invalid agent id {v!r} (want {SLUG.pattern})")
        return v

    @field_validator("tools")
    @classmethod
    def _tools(cls, v: list[str]) -> list[str]:
        v = list(dict.fromkeys(v))
        if not v or any(t not in KNOWN_TOOLS for t in v):
            raise ValueError(f"tools must be a non-empty subset of {KNOWN_TOOLS}")
        return v


class Filter(BaseModel):
    max_bytes: int = Field(default=16384, ge=64, le=1_048_576)
    rate_per_min: int = Field(default=30, ge=1, le=600)
    kinds: list[str] = ["text"]

    @field_validator("kinds")
    @classmethod
    def _kinds(cls, v: list[str]) -> list[str]:
        v = list(dict.fromkeys(v))
        if not v or any(k not in KNOWN_KINDS for k in v):
            raise ValueError(f"kinds must be a non-empty subset of {KNOWN_KINDS}")
        return v


class Channel(BaseModel):
    id: str
    from_: str = Field(alias="from")
    to: str
    name: str = ""
    filter: Filter = Filter()

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError(f"invalid channel id {v!r} (want {SLUG.pattern})")
        return v

    @field_validator("from_", "to")
    @classmethod
    def _endpoint(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError(f"invalid channel endpoint {v!r}")
        return v


class Graph(BaseModel):
    name: str = "graph"
    agents: list[Agent] = Field(min_length=1)
    channels: list[Channel] = []

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not SLUG.match(v):
            raise ValueError(f"invalid graph name {v!r} (want {SLUG.pattern})")
        return v

    @model_validator(mode="after")
    def _check_endpoints(self) -> "Graph":
        ids = {a.id for a in self.agents}
        seen: set[str] = set()
        for ch in self.channels:
            if ch.id in seen:
                raise ValueError(f"duplicate channel id {ch.id!r}")
            seen.add(ch.id)
            if ch.from_ not in ids or ch.to not in ids:
                raise ValueError(f"channel {ch.id!r} references an unknown agent")
            if ch.from_ == ch.to:
                raise ValueError(f"channel {ch.id!r} loops back to its source")
        return self

    # ---- derived artifacts ----

    def subject(self, kind: str, name: str) -> str:
        """kind in {ch, in, out}."""
        return f"orc.{self.name}.{kind}.{name}"

    def channel_subject(self, ch: Channel) -> str:
        return self.subject("ch", ch.id)

    def inbound_of(self, a: Agent) -> list[str]:
        """Subjects an agent may subscribe to."""
        return [self.channel_subject(ch) for ch in self.channels if ch.to == a.id] + [
            self.subject("in", a.id)
        ]

    def outbound_of(self, a: Agent) -> list[str]:
        """Subjects an agent may publish to."""
        return [
            self.channel_subject(ch) for ch in self.channels if ch.from_ == a.id
        ] + [self.subject("out", a.id)]

    def nats_users(self) -> tuple[str, dict[str, str]]:
        """NATS `authorization` block + {username: password}.

        Agent users get exactly their edges (no wildcards). The user account is the
        app's observer: publish on in.>, subscribe on everything. This block is the
        broker-side blindness enforcement; nothing else grants access.
        """
        passwords: dict[str, str] = {}
        entries: list[str] = []
        for a in self.agents:
            user = f"orc-{self.name}-{a.id}"
            pwd = secrets.token_urlsafe(24)
            passwords[user] = pwd
            entries.append(
                json.dumps(
                    {
                        "user": user,
                        "password": pwd,
                        "permissions": {
                            "publish": self.outbound_of(a),
                            "subscribe": self.inbound_of(a),
                        },
                    }
                )
            )
        upwd = secrets.token_urlsafe(24)
        passwords[f"orc-{self.name}-user"] = upwd
        entries.append(
            json.dumps(
                {
                    "user": f"orc-{self.name}-user",
                    "password": upwd,
                    "permissions": {
                        "publish": [self.subject("in", ">")],
                        "subscribe": [self.subject(">", ">")],
                    },
                }
            )
        )
        block = "authorization {\n  users = [\n    " + ",\n    ".join(entries) + "\n  ]\n}\n"
        return block, passwords
