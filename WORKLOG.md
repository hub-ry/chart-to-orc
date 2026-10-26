# Work log

## Tool permissions
Shell is advertised and executed only with explicit shell permission. Browser calls are also refused when disabled, even if the model invents a call. Graph specs now accept shell as an explicit tool. Offline tests cover denied calls and an enabled local shell command. No new environment variables.

## Shell timeouts
Each shell command starts its own process group. A timeout kills the whole group and drains/reaps the shell before returning an error, so child commands do not keep running. A local sleep regression verifies the shell and its child stop. No new environment variables.

## Observer identity and paths
The observer derives senders from the NATS subject and the graph's channel sources, including after restart. User input subjects remain user messages. Markdown writes require a slug sender; message-body src cannot choose a path or impersonate another sender in the traffic log. Offline subscription tests cover spoofing, channels, input, and invalid subjects. No new environment variables.

## Conversation memory
History is now per conversation, with a 50-conversation LRU and the existing 20-message limit per conversation. Prompts start a conv from their message id; automatic replies and send-tool messages retain it and link prev to the triggering message. Missing conv defaults to the incoming message id. Offline tests cover isolation, continuation, eviction, length limits, and both send routes. No new environment variables.

## Route paths, traffic reads, and upstream errors
Graph names and agent ids use one route helper with the schema slug pattern and return 400 when invalid. Traffic reads at most the last 1 MiB per request and uses byte offsets as opaque after/next cursors; the web UI already passes next back unchanged. Partial rows wait for completion, and a cursor beyond a truncated log restarts from the tail. Unexpected LLM response payloads are limited to 300 characters in errors. Offline TestClient and mocked-httpx tests cover these paths without loading the real config file. No new environment variables.

## Offline verification
The full suite passes: 71 tests. An in-process smoke run exercised an ASGI prompt, agent turns through mocked HTTP, broker messages, observer markdown and traffic polling, conversation isolation, invalid slug rejection, enabled/disabled shell calls, and a real local sleep timeout. No live services or provider APIs were used. Pytest reports one dependency deprecation warning from Starlette's httpx-based TestClient.

## Graph-scoped LLM proxy
Agent containers now receive only a random graph bearer token and separate text/vision
proxy URLs. Docker arguments add the host-gateway alias; no provider key is included.
The proxy forces host-configured models, rejects streaming, truncates upstream error
bodies to 300 characters, and shares one token/dollar budget across text and vision.
`proxy.json` stores the token, caps, prices and usage with mode 0600 and atomic
replacement. Re-provisioning preserves these values, so it cannot reset spend.

The main app starts a second uvicorn ASGI listener in its own lifespan, in the same
process. This is simpler than a second deployment/process and keeps provider config
host-only; the proxy app does not mount the main API. One worker is required:
per-graph async locks cover admission, upstream calls and accounting. Startup bind
errors fail startup; shutdown stops only this child listener. The supplied launch
commands now bind the main API to loopback, not the agent-facing interface.

New environment variables: `ORC_PROXY_HOST` (default `0.0.0.0`), `ORC_PROXY_PORT`
(`8788`), `ORC_GRAPH_TOKEN_BUDGET` (`100000`), `ORC_GRAPH_USD_BUDGET` (`1`),
`ORC_PRICE_IN_PER_M` (`1`), `ORC_PRICE_OUT_PER_M` (`3`). Zero disables an individual
cap; at least one must remain enabled. Defaults/prices are captured on first provision.
README documents bridge/firewall requirements, loopback/private UI access, and
explicit re-provisioning of existing containers; no live containers were changed.

Budget checks are admission checks, not reservations: one admitted call may overshoot.
Usage comes from upstream responses; missing/invalid usage on success closes the
budget. Network failures without usage cannot be accounted as provider billing.

Offline verification: 84 tests pass, including auth rejection, token/dollar caps,
graph isolation, concurrent admission, persistence, model/key enforcement, missing
usage, bounded errors, and mocked Docker provisioning with no provider keys.
An in-process smoke ran the actual agent text and vision functions through the ASGI
proxy with mocked upstream HTTP: both replies succeeded, forced models/keys matched,
12 tokens persisted, a fresh proxy rejected the next call with 429, and its main-API
path returned 404. No provider requests, listeners, Docker commands or live-service
changes were used for verification. Real bridge connectivity and socket lifecycle
remain unexercised under this session's restrictions. The existing Starlette/httpx
TestClient deprecation warning remains.

## Graph call and runtime limits
The host proxy now shares a call counter and runtime limit across text and vision.
Attempts are saved before upstream HTTP, including failures. The graph's original
provisioning time, counters and limits survive restart and re-provisioning; calls
are refused with 429 after the call cap or when elapsed time exceeds the runtime
cap. Containers and in-flight requests are not stopped.

New environment variables: `ORC_GRAPH_MAX_CALLS` (default `200`) and
`ORC_GRAPH_MAX_SECONDS` (default `1800`), both positive. Existing proxy state is
migrated once, using `status.json` provisioning time if available, otherwise a
persisted migration time. Historical calls are unknown and start at zero.

Offline verification: the full suite passes (112 tests, one existing
Starlette/httpx deprecation warning). ASGI/mock-HTTP cases cover shared and
concurrent admission, failed attempts, exact runtime boundaries, restart and
legacy migration. An actual agent HTTP call through the ASGI proxy with mocked
upstream HTTP exercised both call and runtime refusals. No provider requests,
listeners, containers or live services were used.
