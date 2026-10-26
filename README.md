# chart-to-orc

Draw a chart. It compiles to a graph of **blind agents** — each in its own Docker
container, reachable only through filtered NATS channels, each a clickable markdown
file you can prompt. Runs on hub, single user, tailnet-visible.

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

## Data

- `~/chart-to-orc/data/<graph>/` — `graph.json` (spec), `status.json`,
  `nats.conf` + `user.env` (observer creds), `agents/<id>.md` (prompt surface),
  `traffic.jsonl` (capped log of everything that crossed a channel).
- Docker objects per graph: `orc-<graph>-nats` container, per agent
  `orc-<graph>-<agent>` container + bridge network + `-ws` volume.
- `DELETE /api/graphs/<name>` tears containers down and removes the data dir.

## Tests

```bash
uv run pytest -q
```

## Status

Working: spec/drawing → compile → validate; accept → provision; agent runtime (agentd)
with shell + workspace tools and per-agent Playwright browser (computer use); per-agent
prompt + live traffic.
See [DESIGN.md](DESIGN.md) proof table P1–P7.
