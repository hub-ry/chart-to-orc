import httpx
import pytest

from app.llm import LLM, LLMError


def test_unexpected_payload_error_is_truncated(monkeypatch):
    payload = {"error": "x" * 10000 + "private-tail"}
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(200, json=payload))
    with pytest.raises(LLMError) as exc:
        LLM("https://provider.invalid", "test-key", "model").chat([])
    message = str(exc.value)
    assert message.startswith("unexpected upstream payload:")
    assert len(message) < 350
    assert "private-tail" not in message
