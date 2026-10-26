"""Minimal OpenAI-compatible chat client (DeepSeek, OpenAI, …). httpx only."""

from __future__ import annotations

import json

import httpx


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 180.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    @property
    def ready(self) -> bool:
        return bool(self.api_key)

    def _post(self, payload: dict, timeout: float) -> dict:
        if not self.api_key:
            raise LLMError("no API key configured")
        try:
            r = httpx.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
                timeout=timeout,
            )
        except httpx.HTTPError as e:
            raise LLMError(f"request failed: {e}") from e
        if r.status_code != 200:
            raise LLMError(f"upstream {r.status_code}: {r.text[:300]}")
        return r.json()

    def chat(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        temperature: float = 0.3,
        max_tokens: int | None = None,
        timeout: float | None = None,
    ) -> str | dict:
        payload: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload, timeout or self.timeout)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected upstream payload: {data}") from e
        if json_mode:
            try:
                return json.loads(content)
            except json.JSONDecodeError as e:
                raise LLMError(f"model returned non-JSON: {content[:200]}") from e
        return content

    def chat_vision(
        self,
        image_b64: str,
        media_type: str,
        prompt: str,
        *,
        temperature: float = 0.2,
        timeout: float | None = None,
    ) -> dict:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media_type};base64,{image_b64}"},
                    },
                ],
            }
        ]
        out = self.chat(messages, json_mode=True, temperature=temperature, timeout=timeout)
        assert isinstance(out, dict)
        return out
