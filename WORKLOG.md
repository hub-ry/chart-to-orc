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
