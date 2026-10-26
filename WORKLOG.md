# Work log

## Tool permissions
Shell is advertised and executed only with explicit shell permission. Browser calls are also refused when disabled, even if the model invents a call. Graph specs now accept shell as an explicit tool. Offline tests cover denied calls and an enabled local shell command. No new environment variables.

## Shell timeouts
Each shell command starts its own process group. A timeout kills that group and reaps the shell before returning an error. The local sleep regression covers same-group children only: descendants that call setsid/double-fork can escape, and successful commands may leave background processes. No new environment variables.

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

## Message hop limit
User prompts now start at hop zero. Tool sends and automatic replies increment
the triggering message's hop count while retaining its conversation and parent
message id. Incoming messages above the limit are logged and dropped before
history or LLM work. At-limit sends return an error to the model; automatic
replies cannot bypass the same check. Missing counts default to zero for older
envelopes; negative, noninteger and boolean counts are logged and dropped.

New environment variable: `ORC_MAX_HOPS` (default `8`, nonnegative), passed into
containers as `MAX_HOPS`. No running containers were changed.

Offline verification: the old agent called the LLM for a hop-9 message; the
regressions now confirm it is dropped without a call. The full suite passes
(112 tests). An agent-to-ASGI-proxy smoke with mocked upstream HTTP preserved
parent/conversation/hop-8 lineage, refused at-limit tool and automatic sends,
and dropped hop 9. The only warning is the existing Starlette/httpx deprecation.

## API authentication and network isolation
Every `/api/*` path now requires a constant-time checked bearer token from
`ORC_API_TOKEN`. Without it, requests fail with 401 unless the new
`ORC_ALLOW_NO_AUTH=1` explicitly opts out; a configured token takes precedence.
Static UI files and health remain public. The UI prompts for a token, stores it
in localStorage and sends it on API requests, preserving JSON and multipart
headers. A 401 clears the saved token and tells the user to reload.

Graph proxy tokens remain independent; offline regressions reject the API token
on both proxy routes and graph tokens on the management API. Loopback defaults
were already present in config, README and service launch and remain unchanged.
README now documents auth setup, browser storage and example INPUT firewall
rules for agent subnets, with the DOCKER-USER forwarding distinction.

Offline verification: 124 focused API/proxy tests pass. An in-process ASGI smoke
created, read and deleted a graph with auth and confirmed missing/wrong-token
401s and bidirectional token separation. A real browser with intercepted offline
responses exercised the prompt, token reuse after reload, authenticated GET/POST,
multipart headers, public health and visible 401 recovery. No provider requests,
listeners, firewall commands, containers or live services were used.
Full suite: `.venv/bin/pytest -q` passes (160 tests); the existing
Starlette/httpx TestClient deprecation warning remains.

## Offline hardening evals
Added `.venv/bin/python -m evals [--label LABEL]`: 29 equally weighted,
isolated offline cases covering fixes 1–14 and adversarial attempts A–D.
The runner uses fake provider credentials, scripted LLM replies, mock HTTP,
fake broker publishing and captured provisioning commands. Network access is
blocked, provider config is not loaded, and only the shell probes start local
processes. No production behavior changed and no new environment variables.

The corrected `--label baseline` run passed 16/29 (55.17%), exiting 0.
The 13 failures are all `not implemented`: failure results, result validation
and waiting, TTL cleanup, graph capacity, provision rollback and removed-agent
cleanup. Future-facing API/config assumptions are explicit in `evals/host_cases.py`.
An initial 14/29 run exposed a harness-only timestamp bug: auth/routing fixtures
looked expired to the proxy. That fixture is fixed; the initial run is retained
alongside the corrected baseline in the append-only history.

Verification: a second scorecard run (`--label repeatability`) produced the same
16/29 and identical failed cases. Both completed with exit 0 and appended history.
`.venv/bin/pytest -q` passed all 160 tests; the existing Starlette/httpx
deprecation warning remains. No Docker commands, provider calls or live services
were used.

## Review mid

### Proxy request bounds and prices
Grok 2 / DeepSeek 4: real unbounded request-body and completion-limit paths.
Stream-read at most 1 MiB before parsing; clamp requested output to 4096 or the
remaining token allowance. Admission remains an estimate, not a hard billing cap:
provider-specific prompt/vision usage can still overshoot, as documented.
Grok 10: real ineffective dollar budget with two zero prices; reject that config.
No new environment variables.

Eval-first: new probes failed on oversized forwarding and accepted zero prices.
Eval score before -> after: 16/31 (51.61%) -> 18/31 (58.06%); initial baseline
16/29 (55.17%). All 160 tests passed. ASGI/mock-upstream probes exercised actual
request rejection, completion clamping and credential creation without network I/O.

### Finding dispositions and deployment boundaries
- Grok 1: rejected automatic legacy-container shutdown/startup refusal as an operational migration policy outside this small fix; README already requires explicit re-provisioning and says old environments persist. No running containers inspected or changed.
- Grok 3 / DeepSeek 1, 10: real shell/storage limitations, not a complete hostile-code sandbox. Corrected the false child-cleanup claim above and documented escaped sessions, successful background commands and unquota'd persistent storage. Portable disk quotas require an operator-selected filesystem/volume driver; tmpfs would silently remove persistence, and rootfs storage-opt does not quota named volumes. Complex subreapers/PDEATHSIG do not solve all descendants; rejected as out of scope.
- Grok 4 / DeepSeek 3: verified private credential creation/repair needed; tracked in provisioning fix below.
- Grok 5: receiver identity spoofing is real and fixed in agent work below; broker-attested hops are not available in this direct client-publish architecture. Documented cooperative hops; no fictitious unforgeable NATS header added.
- Grok 6: verified traffic trim retained oversized byte totals and read whole files; markdown also unbounded. Fixed byte storage limits below. Client-side out-rate limits cannot constrain raw shell publishing; broker ingress/flood controls remain a deployment concern.
- Grok 7 / DeepSeek 6: verified missing rollback and pending-delete cleanup; tracked in provisioning fix below.
- Grok 8: verified workspace tools bypass permissions; tracked in agent fix below.
- Grok 9: verified schema-invalid saved graph aborts startup; fixed below.
- Grok 11 / DeepSeek 2: rejected the claim that module-global a.open cannot override builtin open: Python global lookup does use it. Disabled-shell probe intentionally tests denial, and captured provisioning args independently test provider-key exclusion. Strengthened positive controls/enabled-shell probe below; no host secret reads.
- DeepSeek 5: verified removed-agent resource leak; tracked in provisioning fix below.
- DeepSeek 7: rejected unverified backend compatibility request; fail-closed accounting intentionally rejects inconsistent totals, accepts missing total by summing components, and avoids trusting underreported usage. No provider-specific exception added.
- DeepSeek 8: rejected token-cache infrastructure for a small private listener; absent tokens already reject before scans, and unknown nonempty tokens require lookup. Cache invalidation across delete/re-provision/restart adds state for an unmeasured scale concern.
- DeepSeek 9: verified unbounded communicate buffering; tracked in agent fix below.
- DeepSeek 11: rejected; UTF-8 encoding of Latin-1 header characters is valid. Offline raw-header GET with Bearer byte ff returned 401, not 500.

No new environment variables for these documentation corrections. Each real fix
has an eval-first score entry below; rejected findings do not change eval cases.

### Agent permissions, identity and bounded shell capture
Grok 8: workspace tool advertisement and dispatch now require workspace permission.
Grok 5: normal receivers overwrite src/ch from their broker-bound subscription and
configured sender before building model context; hops remain cooperative.
DeepSeek 9: drain stdout/stderr concurrently in bounded chunks and retain only
8192 decoded characters each, rather than buffering all output with communicate.
Grok 11 / DeepSeek 2: added positive controls proving the simulated proc tripwire
actually exposes fixture credentials if path resolution is bypassed, plus an
enabled-shell probe of the captured container graph-token environment.
No new environment variables.

Eval-first: permissions, bounded capture and receiver identity all failed.
Eval score before -> after (integrated tree): 20/43 (46.51%) -> 32/43 (74.42%).
All 160 tests passed. Existing evals ran local printf and same-group timeout paths;
a separate real local shell emitted 1,000,000 characters on each stream and
returned exit 0 with exactly 8192 captured per stream. No real env/proc reads.

### Provision credentials and resource lifecycle
Grok 4 / DeepSeek 3: create/replace broker credential files with mode 0600,
including repair on reaccept. Grok 7 / DeepSeek 6: record intended resource ids
in resources.json before Docker mutations, rollback failures, mark status failed,
and clean resources even when DELETE sees a non-live status. DeepSeek 5: remove
departed-agent containers, networks and workspaces using that independent inventory,
even if graph.json was overwritten; retain continuing-agent workspace volumes.
Grok 3: add a fixed 256-process container limit; no disk-quota claim.
No new environment variables.

Eval-first: all seven new provision regressions failed, as did both existing
lifecycle cases. Eval score before -> after (integrated tree): 19/40 (47.50%) ->
32/43 (74.42%). The first provision-only measurement hit a concurrent harness import
error before recording a score; the recorded before run above verified the same
failures before implementation. All 160 tests passed. Mock Docker state exercised
fresh/failed reaccept, overwritten specs, private files and pending deletion; actual
Docker/network/volume behavior was not exercised under the session restrictions.

### Bounded observer storage and resilient resume
Grok 6: traffic and agent markdown now keep at most 4,000,000 bytes each;
trimming reads a bounded tail and discards partial/oversized lines instead of
reading the whole file and retaining an unbounded last-5000-lines total. User
prompt markdown uses the same cap. Grok 9: skip Pydantic ValidationError on
resume so one invalid saved graph does not disable the app or valid siblings.
No new environment variables.

Eval-first: both new probes failed on current code. Eval score before -> after
(integrated tree): 18/33 (54.55%) -> 32/43 (74.42%). An intermediate 19/40
(47.50%) run caught an append-mode trimming bug in this change and included seven
new still-unfixed provisioning probes; both issues were fixed before committing.
All 160 tests passed. Actual observer writes of twenty 300-KiB messages stayed
within both byte caps, preserved the newest JSON row, and the lifespan/ASGI probe
kept health available and resumed a valid sibling beside a schema-invalid graph.

### Review outcome
Every numbered finding from both reports is listed above, including duplicates,
partial fixes and rejected scope. Baseline 16/29 (55.17%) -> final 32/43 (74.42%);
all fourteen added cases pass and both existing provisioning cases now pass.
The eleven remaining failures are preexisting unimplemented result envelopes,
result validation/waiting, TTL and live-graph capacity features, not regressions.
All checks were offline; no containers, services, firewall, provider APIs or
credential files were accessed. Existing history was preserved and each commit
includes evals/history.jsonl.

## Failure results, result schemas and waiting
Agents now publish an error result when the tool-call iteration budget runs out
or the LLM fails, instead of silently ending the turn. Errors use short fixed
reasons, retain parent/conversation metadata, and increment hops through the same
hop-limited send path as other messages.

Graphs may set a graph-level result_schema object, included in every agent's
system prompt on provisioning. Host observation validates user-facing result
bodies with a small dependency-free validator: type, required, properties,
items, enum and additionalProperties:false. Other JSON Schema keywords are
ignored. Invalid bodies stay in traffic and in wait responses with valid:false
and an error; valid bodies get valid:true. No new environment variables.

GET /api/graphs/{name}/result accepts optional conv and timeout (default 30,
range 0–300 seconds). It returns HTTP 200 with status:result and the flagged
envelope, or HTTP 408 with status:timeout. Asyncio events wake concurrent
waiters; no disk polling. A non-consuming cache keeps the latest 100 results per
graph, cleared on restart, deletion or reaccept. Zero timeout checks the cache.

The old future-facing eval contract incorrectly assumed per-agent schemas,
POST prompt waiting, result_valid and HTTP 422/504. Updated those probes to the
requested graph-level schema, separate GET endpoint, valid flag and documented
HTTP 200/408 semantics. No cases removed; invalid-result probes additionally
require a validation error. Historical score rows were preserved.

Eval score before -> after: 32/43 (74.42%) -> 39/43 (90.70%).
Intermediate failure-result change: 34/43 (79.07%). All 173 tests pass; the
existing Starlette/httpx deprecation warning remains. Offline cases cover
iteration exhaustion, LLM failure, schema boundaries, concurrent waiting,
conversation/channel isolation, cached results and timeout. An in-process smoke
ran an actual ASGI prompt through the agent with a failing mock LLM, fake broker
and real observer: HTTP 200 result, valid:true, preserved lineage, hop 1, and
HTTP 408 for an absent conversation. No provider requests, services or containers
were used. Remaining eval failures are TTL and graph capacity, outside this task.

## Graph lifecycle admission and expiry

Added `ORC_GRAPH_TTL_SECONDS` (default `0`, disabled) and
`ORC_MAX_LIVE_GRAPHS` (default `5`). Enabling a positive TTL will tear down old
live graphs, including their workspace volumes. A lifespan-owned task checks
every 60 seconds, stops the observer, removes inventoried resources and marks
status expired while retaining specs/logs. Age is measured from successful
provisioning; reaccept resets it. Exact TTL boundaries are retained.

One lifecycle lock serializes accept, delete and GC, including capacity checks;
concurrent accepts cannot both take the final slot. Live reaccept is allowed.
Existing inventory rollback and departed-agent cleanup already satisfy this
task and still pass their offline resource-state evals. Failed status now also
contains a short error category, without embedding command arguments or secrets.

The future-facing TTL eval incorrectly required deletion of all graph data.
Updated only that assertion to require the requested expired status; container
and volume cleanup assertions remain. No cases removed or weakened.
Eval score before -> after: 39/43 (90.70%) -> 43/43 (100.00%).
All 176 tests pass, with one existing Starlette/httpx deprecation warning.
Offline ASGI/captured-command evals exercise failed provision, departed agents,
capacity and TTL. A separate real app lifespan smoke observed background GC
remove expired resources, preserve a fresh graph and finish shutdown, with
mocked broker/proxy/Docker operations. No live services or provider calls used.

## Broker payload ceiling and send-rate limits

The existing generated NATS configuration already enforces
`max_payload: 1048576` per graph server; no second size setting is needed.
DESIGN now distinguishes that whole-envelope ceiling from receiving agents'
per-channel body/kind checks. A compromised agent can use its own password to
publish directly within its ACL, bypassing cooperative send-rate checks; there
is no broker-side per-channel rate cap. No new environment variables.

Eval score before -> after: 43/43 (100.00%) -> 43/43 (100.00%).
All 176 offline tests pass; one existing Starlette/httpx warning remains.
No broker or container was started; broker enforcement itself was not exercised.

## Open-source licensing and portable deployment

Added the MIT license, copyright 2026 chart-to-orc contributors. The service
template uses YOUR_USER/YOUR_GROUP, `/opt/chart-to-orc` and
`/etc/chart-to-orc/env`, explicitly sets the matching data directory, and keeps
the API bound to 127.0.0.1. README explains adapting these paths and permissions.
Removed personal server/credential references from docs and the config header.
No new environment variables.

The public source/docs/config scan found no remaining personal identifiers
from the requested list (dependency/code words are not personal references).
Eval score before -> after: 43/43 (100.00%) -> 43/43 (100.00%).
All 176 tests pass, with the existing Starlette/httpx warning. No service file
was installed and no service or container was changed; deployment is operator-run.

## Deploying this branch

New hardening environment variables across this branch:

| Variable | Default | Effect |
|---|---|---|
| `ORC_API_TOKEN` | empty | Management API refuses requests until a token is configured or no-auth is explicitly allowed |
| `ORC_ALLOW_NO_AUTH` | unset/false | Only `1` permits unauthenticated API use when no token is set |
| `ORC_PROXY_HOST` | `0.0.0.0` | Agent-only proxy listener bind |
| `ORC_PROXY_PORT` | `8788` | Agent-only proxy listener port |
| `ORC_GRAPH_TOKEN_BUDGET` | `100000` | Shared text/vision token budget; `0` disables |
| `ORC_GRAPH_USD_BUDGET` | `1` | Shared estimated-dollar budget; `0` disables |
| `ORC_PRICE_IN_PER_M` | `1` | Estimated dollars per million input tokens |
| `ORC_PRICE_OUT_PER_M` | `3` | Estimated dollars per million output tokens |
| `ORC_GRAPH_MAX_CALLS` | `200` | Positive maximum upstream attempts per graph |
| `ORC_GRAPH_MAX_SECONDS` | `1800` | Positive proxy runtime limit from original provisioning; does not stop containers |
| `ORC_MAX_HOPS` | `8` | Nonnegative cooperative message hop cap, passed as agent `MAX_HOPS` |
| `ORC_GRAPH_TTL_SECONDS` | `0` | Disabled by default; positive TTL tears down old live graphs and their workspace volumes |
| `ORC_MAX_LIVE_GRAPHS` | `5` | Admission cap on live graphs; live reaccept is permitted |

At least one token/dollar budget must remain enabled. An enabled dollar cap needs
a nonzero price. Proxy budgets/prices/call/runtime limits persist per graph;
changing defaults does not reset existing proxy state. TTL instead uses the last
successful provisioning time in status and is checked about every 60 seconds.
The service template also explicitly sets the existing `ORC_DATA_DIR` to
`/opt/chart-to-orc/data`; interactive config defaults to `~/chart-to-orc/data`.

Operator steps (not executed during this task):

1. Review/merge the hardening commits into the intended deployment checkout,
   preserving its data. Do not point a running service at this test worktree.
2. Set a separate random `ORC_API_TOKEN`, provider configuration and desired limits
   in the private env file (0600). Keep TTL at zero unless removal of old live
   graphs and workspaces is intended. The supplied service reads
   `/etc/chart-to-orc/env`; interactive config additionally loads
   `~/.config/chart-to-orc/env`, which overrides process env values. Avoid conflicts.
3. Adapt YOUR_USER/YOUR_GROUP and `/opt/chart-to-orc` paths in the service template,
   including the data path; give the service user access to that data directory.
   Reinstall the service file and reload the service manager's unit definitions.
   Keep the main API on 127.0.0.1 and run one uvicorn worker.
4. Install updated project dependencies, then explicitly rebuild the configured
   agent image from `agentd/` (default tag `chart-orc/agent:1`): agentd changed
   across this branch. Image existence checks alone will not rebuild an old tag.
5. Apply an operator-reviewed firewall policy allowing agent bridges to reach
   only the host proxy port, not the management API or other host services.
   Restrict proxy access on other interfaces as well.
6. Restart the service. Check health and authenticated API access. Enter the API
   token in the UI; graph bearer tokens and this token are intentionally separate.
7. Re-accept existing graphs so containers receive the graph proxy token/URLs,
   hop limit, updated result behavior and rebuilt agent runtime; old running
   containers retain their old environment, including any provider credentials.
   Reaccept also installs the generated broker config and removes departed agents.
   Confirm traffic/result behavior and inspect status. Existing proxy caps remain.

Documentation-only final change; no new environment variables beyond those listed.
Eval score before -> after: 43/43 (100.00%) -> 43/43 (100.00%).
Final offline suite: 176 passed, one existing Starlette/httpx deprecation warning.
All task commits include eval history. No deployment commands, real broker/Docker
operations, secret files or provider calls were used.

## Review end

Baseline before changes: 43/43 (100.00%). Both review reports and the branch
commits were checked against the code. All runs below are offline; no real
credentials, provider endpoints, Docker, firewall or service operations used.

### Failed accept and interrupted provisioning

- Grok 5: real. Observer connection failure now tears down resources and records
  failed status instead of leaving a live graph. The periodic collector also
  removes provisioning interrupted more than five minutes ago, even with TTL off.
  The lifecycle lock prevents collecting an accept still running in this process.
  No new environment variables.

Eval-first: both added lifecycle cases failed, 43/45 (95.56%). Eval score before
-> after: 43/45 (95.56%) -> 54/54 (100.00%) in the integrated working tree,
including independently added config/agent/proxy cases. Original 43 cases still
pass. All 176 tests pass (one existing Starlette/httpx deprecation warning).
ASGI accept and real cleanup code exercised captured Docker commands and status
transitions; actual container cleanup was not run.
