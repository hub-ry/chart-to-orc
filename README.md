# chart-to-orc

Draw a chart. It compiles to a graph of **blind agents** — each in its own Docker
container, reachable only through filtered NATS channels, each a clickable markdown
file you can prompt. Runs on your server, for a single user, with private access.

Architecture and contracts: [DESIGN.md](DESIGN.md).

## How it works

1. **Draw** — upload a photo/sketch of boxes + arrows (vision model parses it), or paste a JSON spec.
2. **Compile** — the app validates it into a graph spec: agents, channels, filters. You review it.
3. **Accept** — a one-shot orchestrator provisions: per-agent bridge network + workspace
   volume + container, one NATS broker per graph with per-agent credentials scoped to
   exactly the drawn edges. A separate host-side LLM proxy meters agent requests.
4. **Prompt** — every agent is a markdown file. Click it, prompt it. Messages cross only
   the drawn channels, through the drawn filters. The UI shows the live filtered traffic.

Blindness is structural: agents share no network, no volumes, no docker socket; the
broker refuses any subject outside an agent's allow-list; agents never see the graph
spec, only their own edges.

## Run

```bash
cd ~/chart-to-orc
uv sync --extra dev          # first time
# secrets: ~/.config/chart-to-orc/env (chmod 600)
#   DEEPSEEK_API_KEY=...       agent brain + vision parser (deepseek-flash)
#   ORC_LLM_BASE_URL / ORC_LLM_MODEL / ORC_VISION_*  overrides
#   ORC_API_TOKEN=...         separate random token for API/UI access
uv run uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787` locally or through a private tunnel. The supplied
`deploy/chart-to-orc.service` also binds the main API to loopback.

For the service template, replace `YOUR_USER`/`YOUR_GROUP` and adapt the
`/opt/chart-to-orc` paths before installing. It reads `/etc/chart-to-orc/env`
and explicitly sets `ORC_DATA_DIR=/opt/chart-to-orc/data`; create that directory
writable by the service user. Keep the env file private (0600). Interactive
runs also load `~/.config/chart-to-orc/env`; avoid conflicting settings there.

## API authentication

Set `ORC_API_TOKEN` to a separate random secret. Every `/api/*` request requires
`Authorization: Bearer <token>`; missing or wrong credentials return 401.
Without a token, the API refuses requests unless `ORC_ALLOW_NO_AUTH=1` explicitly
enables unauthenticated local use. A configured token always takes precedence.
The UI, static files and `/health` remain public.

The UI prompts once and stores the token in this browser's localStorage for the
current origin. A 401 clears it; reload to enter a replacement. Clear the
`orc_api_token` localStorage entry to forget it manually. Use only a trusted
browser and loopback/private tunnel, or HTTPS for remote access. The LLM proxy
accepts only its own per-graph tokens, not this API token.

## Network isolation

`ORC_HOST` defaults to `127.0.0.1`; the uvicorn command and service explicitly
bind loopback too (uvicorn CLI flags control the actual bind). Keep the API there.
For every `orc-*` bridge subnet, block host access except TCP proxy port `8788`
(adjust to `ORC_PROXY_PORT`). Example policy, for an administrator to adapt:

```bash
iptables -I INPUT 1 -s <agent-subnet> -p tcp --dport 8788 -j ACCEPT
iptables -I INPUT 2 -s <agent-subnet> -j DROP
```

Host-destined traffic uses `INPUT`, not Docker's forwarding-only `DOCKER-USER`.
Put these before broader accepts; cover every agent subnet and IPv6 if enabled.
Use `DOCKER-USER` as well if the API is ever forwarded to a container. Restrict
proxy ingress on other interfaces separately; API auth is not a firewall.

## Agent LLM proxy

The main app starts a second, agent-only ASGI listener in the same process. Run
**one uvicorn worker**: per-graph locks serialize text and vision calls, including
budget checks and disk updates. The proxy has no UI, graph-management API, or
static files. Agents receive a random graph token, never provider keys; both
routes force the configured host-side models and reject streaming.

| Environment variable | Default | Meaning |
|---|---|---|
| `ORC_PROXY_HOST` | `0.0.0.0` | Proxy bind address, reachable from agent bridges |
| `ORC_PROXY_PORT` | `8788` | Dedicated proxy port |
| `ORC_GRAPH_TOKEN_BUDGET` | `100000` | Combined input/output tokens per graph; `0` disables this cap |
| `ORC_GRAPH_USD_BUDGET` | `1` | Estimated dollars per graph; `0` disables this cap |
| `ORC_GRAPH_MAX_CALLS` | `200` | Maximum upstream attempts per graph, shared by text and vision |
| `ORC_GRAPH_MAX_SECONDS` | `1800` | Maximum elapsed seconds since first provisioning |
| `ORC_PRICE_IN_PER_M` | `1` | Estimated dollars per million input tokens |
| `ORC_PRICE_OUT_PER_M` | `3` | Estimated dollars per million output tokens |

At least one token/dollar cap must be enabled. Prices are simple estimates shared by
text and vision, not provider billing. Budgets/prices are captured at first provision.
`proxy.json` in the graph data directory holds the token, caps, prices and usage
with mode `0600`; re-provisioning and restarts preserve it. Deleting the graph
removes it. Usage updates use atomic file replacement.

Containers use `--add-host host.docker.internal:host-gateway` and
`http://host.docker.internal:8788/llm` or `/vision` (port follows configuration).
Restrict proxy ingress to local agent bridges with a host firewall; the default
wildcard bind otherwise exposes this port on every host interface. Alternatively
bind `ORC_PROXY_HOST` to the Docker host-gateway address (commonly `172.17.0.1`).
Keep the **main API on loopback**, not `0.0.0.0` or the Docker gateway. For remote
UI access use a private tunnel, or a dedicated private interface with firewall
rules denying agent-bridge access. Proxy authentication does not protect the main API.

Requests above 1 MiB are rejected before JSON parsing. Completion limits are
clamped to 4096 tokens and the remaining token allowance; omitted limits use that
ceiling. This bounds requested output, not prompt/vision tokens or actual billing.
An enabled dollar budget requires at least one nonzero token price.

The cap is checked before forwarding; one admitted request can exceed its remaining
budget. The next request gets HTTP 429. Accounting relies on upstream `usage`;
an otherwise successful response with missing/invalid usage closes the graph budget
and returns 502. Failed/ambiguous network requests cannot be billed locally without
reported usage. Upstream error bodies are limited to 300 characters.
Call and runtime limits also return HTTP 429 before forwarding. Attempts are counted
and persisted before upstream HTTP, even if it fails; restarts and re-provisioning do
not reset them or the runtime epoch. These limits do not stop containers or cancel
in-flight calls. Old proxy state gains these limits on first use; it uses the existing
`status.json` provisioning time when available, otherwise a persisted migration time.
Historical call counts from before this upgrade are unavailable and start at zero.
Both call and runtime limits must be positive.

Existing running containers retain their old environment until explicitly
re-provisioned. This change does not alter running containers automatically.

## Graph lifecycle

`ORC_MAX_LIVE_GRAPHS` defaults to `5`. Accepting a new live graph at capacity
returns HTTP 429 without changing its draft; reaccepting a live graph is allowed.
Run one worker so lifecycle admission and teardown share the same lock.

`ORC_GRAPH_TTL_SECONDS` defaults to `0` (disabled). A positive value enables
cleanup roughly every 60 seconds, based on `status.json`'s `provisioned_at`
(reset on successful reaccept). Enabling it **tears down old live graphs**,
including their workspace volumes. Cleanup stops the observer and removes the
containers, networks and volumes, then marks status `expired`. Specs and logs
remain available; reaccept recreates resources. Exact TTL boundaries remain live.

## Results and waiting

Agents publish `kind: "result"` with `body: {"ok": false, "error": "..."}` on
LLM failures or exhausted tool-call iterations. These retain `prev` and `conv`,
increment `hops`, and obey the same hop limit as other sends.

An optional graph-level `result_schema` object describes result bodies. The host
uses a dependency-free JSON Schema subset: `type` (including type arrays),
`required`, `properties`, `items`, `enum`, and `additionalProperties: false`.
Other keywords are ignored; this is not a full JSON Schema implementation.
Every agent receives the schema in its system prompt on provisioning.
The observer validates only results on user-facing `out` subjects. Traffic keeps
the original body with host-computed `valid` and, on failure, an `error` string.
Runtime error bodies may themselves fail a graph's success-result schema.

After POSTing a prompt, use its `msg_id` as `conv`:
`GET /api/graphs/{name}/result?timeout=30&conv=<msg_id>`.
HTTP 200 returns `{"status":"result","result":<flagged envelope>}`, including
invalid results; callers must check `result.valid`. With no `conv`, it returns
the latest observed result for that graph. Timeout returns HTTP 408 with
`{"status":"timeout"}`. The default timeout is 30 seconds; accepted range is
0–300 seconds (otherwise HTTP 400); zero checks already-cached results only.
Waiting uses in-memory asyncio events, not disk polling. The most recent 100
results per graph remain available, without consuming them on reads; restart,
deletion and reaccept clear that cache. Historical traffic is not replayed.

## Message loop guard

`ORC_MAX_HOPS` (default `8`, nonnegative) is passed to agent containers as
`MAX_HOPS`. User prompts start with `hops: 0` and `prev: null`. Every response,
including automatic plain-text replies, retains `conv`, sets `prev` to the
triggering message id, and increments `hops`.

Agents log and drop incoming messages above the limit before calling the LLM.
A message at the limit may be processed, but its send tool returns an error
visible to the model; automatic replies cannot bypass the limit. Missing hop
counts are treated as zero for older envelopes; invalid counts are logged and
dropped. Existing containers need explicit re-provisioning to receive the new
runtime and environment.

Hop counts are cooperative loop metadata, not broker-attested security limits.
An agent with shell access can publish directly using its own NATS credentials
and forge/reset hops within its allowed subjects; proxy call/runtime caps remain
the host-side spending backstop.

## Shell and storage limits

Only grant shell access to trusted workloads. Timeout cleanup kills the command's
process group, not descendants that create a new session (`setsid`/double-fork).
Commands may also leave background processes after a successful exit. Workspace
volumes and container writable layers have no portable disk quota here; deploy
on quota-managed storage if disk exhaustion is a concern. Budget exhaustion
does not stop containers. Docker isolation is not a complete hostile-code sandbox.


## Data

- `~/chart-to-orc/data/<graph>/` — `graph.json` (spec), `status.json`,
  `nats.conf` + `user.env` (observer creds), `agents/<id>.md` (prompt surface),
  `traffic.jsonl` (capped log of everything that crossed a channel).
- Docker objects per graph: `orc-<graph>-nats` container, per agent
  `orc-<graph>-<agent>` container + bridge network + `-ws` volume.
- `DELETE /api/graphs/<name>` tears containers down and removes the data dir.

`nats.conf` and `user.env` are private (0600). `resources.json` records intended
agent ids before provisioning changes, independently of the editable graph spec.
Failed provisioning removes containers/networks and newly created workspaces,
marks the graph failed, and preserves existing retained workspaces. Reaccept
removes departed agents' resources; DELETE cleans inventoried resources regardless
of status. Agent containers have a 256-process limit.

Traffic and agent markdown each retain at most 4,000,000 bytes of recent complete
lines. Oversized individual appends are dropped; trimming reads only a bounded
tail, not the entire prior file. These are rolling views, not archival records.
On startup, a schema-invalid saved graph is skipped without preventing valid
sibling graphs from resuming.

## Tests

```bash
uv run pytest -q
```

## Evals

```bash
.venv/bin/python -m evals --label baseline
```

The offline scorecard exercises real app and agent paths with scripted LLM replies,
mock HTTP upstreams, fake NATS publishing and captured provisioning commands.
It does not read provider config, contact services or run Docker. The shell timeout
case starts and cleans up a local shell and child process in a temporary workspace.

Each case has equal weight: score = passed / total × 100. Missing features count
as `FAIL: not implemented`, not skips. Case failures still exit 0; harness errors
exit nonzero. This measures these scenarios, not overall security or live network
isolation. Future result/lifecycle contracts are documented in
[`evals/host_cases.py`](evals/host_cases.py).

Each run appends UTC time, commit, pre-run dirty state, optional label, score and
failed case names to `evals/history.jsonl`. Commit the history when recording a
baseline. Repeating the command should give the same outcomes for unchanged code.

## Status

Working: spec/drawing → compile → validate; accept → provision; agent runtime (agentd)
with shell + workspace tools and per-agent Playwright browser (computer use); per-agent
prompt + live traffic.
See [DESIGN.md](DESIGN.md) proof table P1–P7.
