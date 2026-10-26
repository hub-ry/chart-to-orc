"""Manus API v2 client (computer use).

Implements the documented flow from https://open.manus.ai/docs/v2/task-lifecycle:

1. POST /v2/task.create  -> task_id
2. Poll GET /v2/task.listMessages until the agent_status reaches a terminal
   state (stopped / error).
3. Return the final assistant text.

Auth uses the ``x-manus-api-key`` header (see
https://open.manus.ai/docs/v2/authentication).
"""

from __future__ import annotations

import time

import httpx

DEFAULT_BASE_URL = "https://api.manus.ai"
_POLL_INTERVAL = 5.0
_TERMINAL = ("stopped", "error")


class ManusError(Exception):
    pass


def _check_ok(payload):
    if not isinstance(payload, dict) or not payload.get("ok", False):
        err = payload.get("error") if isinstance(payload, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        raise ManusError(msg or f"manus api error: {payload!r}")


def _resume_if_waiting(client, task_id):
    client.post(
        "/v2/task.sendMessage",
        json={"task_id": task_id, "message": {"content": "Continue and use your best judgment."}},
    ).raise_for_status()


def run_task(prompt, api_key, base_url=None, timeout_s=1200):
    """Create a Manus task, poll to completion, return the final output text."""
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "x-manus-api-key": api_key,
    }

    with httpx.Client(base_url=base, headers=headers, timeout=60.0) as client:
        created = client.post(
            "/v2/task.create",
            json={"message": {"content": prompt}, "interactive_mode": False},
        )
        created.raise_for_status()
        payload = created.json()
        _check_ok(payload)
        task_id = payload.get("task_id")
        if not task_id:
            raise ManusError(f"task.create returned no task_id: {payload!r}")

        deadline = time.monotonic() + timeout_s
        while True:
            if time.monotonic() >= deadline:
                raise ManusError(f"task {task_id} timed out after {timeout_s}s")

            resp = client.get(
                "/v2/task.listMessages",
                params={"task_id": task_id, "order": "desc", "limit": 50},
            )
            resp.raise_for_status()
            payload = resp.json()
            _check_ok(payload)
            messages = payload.get("messages", [])

            status = None
            result_text = None
            fallback_text = None
            error_text = None
            for m in messages:
                t = m.get("type")
                if t == "status_update" and status is None:
                    su = m.get("status_update") or {}
                    status = su.get("agent_status")
                elif t == "assistant_message":
                    am = m.get("assistant_message") or {}
                    content = am.get("content") or ""
                    if fallback_text is None:
                        fallback_text = content
                    if result_text is None and am.get("delivery_kind") == "result":
                        result_text = content
                elif t == "error_message":
                    em = m.get("error_message") or {}
                    if error_text is None:
                        error_text = em.get("content") or em.get("error_type")

            if status == "error":
                raise ManusError(error_text or f"task {task_id} failed")
            if status == "stopped":
                return result_text or fallback_text or ""
            if status == "waiting":
                _resume_if_waiting(client, task_id)

            time.sleep(_POLL_INTERVAL)
