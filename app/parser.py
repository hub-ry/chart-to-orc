"""Drawing → graph. Vision LLM parses the drawing; JSON spec is the no-vision path."""

from __future__ import annotations

import json

from pydantic import ValidationError

from app.llm import LLM, LLMError
from app.schema import KNOWN_KINDS, Graph

PARSE_PROMPT = """You parse a hand-drawn diagram of a multi-agent system into one strict JSON object.

- "name": a slug for the whole system (lowercase, `^[a-z][a-z0-9_-]{0,31}$`), inferred from any title text; default "graph".
- "agents": array of boxes in the drawing. Each: {"id": slug, "name": short label, "system": system prompt describing that agent's job (write it yourself if the drawing doesn't say), "tools": ["workspace"], "memory_mb": 512, "cpus": 1.0}.
- "channels": array of arrows. Each: {"id": unique slug, "from": agent id, "to": agent id, "name": label text on the arrow, "filter": {"max_bytes": 16384, "rate_per_min": 30, "kinds": subset of ["text", "artifact", "result"]}}.
- An arrow is one directional channel. If two agents are connected in both directions, emit two channels.
- ids must be unique and match `^[a-z][a-z0-9_-]{0,31}$`.
- Do not invent agents or channels that are not in the drawing.
Reply with ONLY the JSON object, no prose, no code fences."""


class ParseError(RuntimeError):
    pass


def parse_drawing(llm: LLM, image_b64: str, media_type: str, *, attempts: int = 3) -> Graph:
    errors = ""
    for _ in range(attempts):
        prompt = PARSE_PROMPT
        if errors:
            prompt += (
                f"\n\nThe previous attempt failed validation with these errors:\n{errors}\n"
                "Return the corrected JSON object only."
            )
        try:
            data = llm.chat_vision(image_b64, media_type, prompt)
        except LLMError as e:
            raise ParseError(f"vision call failed: {e}") from e
        try:
            return Graph.model_validate(data)
        except ValidationError as e:
            errors = json.dumps(e.errors(), indent=1)
    raise ParseError(f"drawing did not yield a valid graph after {attempts} attempts: {errors}")


def parse_spec(text: str) -> Graph:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ParseError(f"spec is not JSON: {e}") from e
    try:
        return Graph.model_validate(data)
    except ValidationError as e:
        raise ParseError(f"spec invalid: {json.dumps(e.errors(), indent=1)}") from e
