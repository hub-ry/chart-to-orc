# chart-to-orc

Draw a chart. Get a running graph of blind agents — each in its own Docker container,
reachable only through filtered channels, each a clickable markdown file you can prompt.
Lives on hub, single user, tailnet-visible.

## Outcome (in-your-own-words)

1. Upload a drawing of a chart (photo/sketch of boxes + arrows) or paste a JSON spec.
2. It compiles to a validated graph spec: agents, channels, filters. You review and accept.
3. A one-shot orchestrator provisions: per-agent container + workspace + NATS credentials
   scoped to exactly that agent's edges. Provisioning ends; a host LLM proxy meters requests.
4. Each agent appears as a markdown file. Click it, prompt it. Messages cross only the
   channels drawn, through the filters drawn. The UI shows the filtered traffic.

## Proof (prove-it-works)

| # | Check | How |
|---|-------|-----|
| P1 | drawing → valid graph JSON | upload the smoke drawing; parser returns schema-valid graph; pytest covers schema |
| P2 | accept → live containers | `docker ps` shows nats + one agentd container per agent; each on its own bridge network |
| P3 | blindness: no addressability | from agent A's container: DNS/IP of agent B unresolved; NATS publish to a subject outside its allow-list is refused by the broker (not by convention) |
| P4 | blindness: no shared state | no shared volumes; the only file mounted per agent is its own workspace |
| P5 | filtered traffic | prompt A via its markdown file → message appears on the drawn channel with filter applied; B's reply crosses only its own channel; UI live view shows exactly those messages |
| P6 | provisioner has no runtime role | provisioning ends after accept; a separate host proxy retains provider keys and meters graph tokens |
| P7 | idempotent provision | re-running provision on the same graph converges (make-operations-idempotent) |

## Contracts (boundary-discipline)

### graph.json — the compiled spec (single source of truth: `app/schema.py`)

```json
{
  "name": "ship-docs",
  "agents": [
    {"id": "researcher", "name": "Researcher", "system": "…", "tools": ["workspace"], "memory_mb": 512, "cpus": 1.0},
    {"id": "writer", "name": "Writer", "system": "…", "tools": ["workspace"]}
  ],
  "channels": [
    {"id": "c1", "from": "researcher", "to": "writer", "name": "drafts",
     "filter": {"max_bytes": 16384, "rate_per_min": 30, "kinds": ["text", "artifact"]}}
  ]
}
```

Rules (enforced by `Graph`, not prose): ids are slugs `^[a-z][a-z0-9_-]{0,31}$`, unique;
channel endpoints must be agents; no self-loops; tools ⊆ {workspace, browser, shell};
kinds ⊆ {text, artifact, result}; ≥1 agent. Channels are directional — a two-way
conversation is two arrows, two channels.

### Message envelope (agentd ↔ NATS ↔ observer)

```json
{"v":1, "msg_id":"<uuid4>", "ts":"<iso>", "src":"<agent-id>",
 "ch":"ch.c1 | in.researcher | out.researcher", "kind":"text|artifact|result",
 "body":{…}, "prev":null, "conv":"<root-msg-id>"}
```

`body` is a free JSON object; `kind` names its shape for filters. Agentd validates the
envelope against the channel's filter on publish (size, kind, rate) and re-validates on
receive. The UI's user account may only publish `in.*` prompts.
Replies and sends retain `conv`; messages without it start a conversation using their
`msg_id`. Agent history is isolated per conversation: at most 50 recently used
conversations, each retaining at most 20 messages.


### NATS subject layout (per graph, broker-enforced)

| Subject | Publisher | Subscriber |
|---|---|---|
| `orc.<g>.ch.<channel>` | the channel's `from` agent | the channel's `to` agent |
| `orc.<g>.in.<agent>` | user (UI) | that agent |
| `orc.<g>.out.<agent>` | that agent | user (UI, live view) |

Agent users get exactly their edges — no wildcards. The user account gets `in.>` publish
and `>.>` subscribe (the eye of god, held by the app, never by an agent). Responses are
not allowed (`allow_responses` off). This is the **encode-lessons-in-structure** fix:
blindness is a broker ACL, not an instruction to the agent.

## Blindness invariants (what "completely blind" means here)

- I1. No agent container shares a network with another agent container. Each agent lives
  on its own bridge network; only the per-graph NATS container is attached to all of them.
  → agents cannot resolve or route to each other at L2/L3.
- I2. NATS user per agent: publish allowed only on `ch.*` where it is `from` plus its own
  `out.*`; subscribe only on `ch.*` where it is `to` plus its own `in.*`. No wildcards.
- I3. No shared volumes; no docker socket; each agent mounts only its own workspace and
  read-only creds. No host paths beyond its own data dir.
- I4. The graph spec is never mounted into agent containers — each agent receives only its
  own slice (id, system, tools, subjects) as env/creds. It cannot read the graph file.
- I5. Provisioning is one-shot. Provider credentials stay in the host app's separate
  LLM proxy; agents receive only a graph-scoped bearer token and proxy endpoints.
  The main API stays on loopback, outside the agent-facing listener.
- I6. Agents communicate with peers only through broker subjects listed above.
  The shared LLM proxy forwards independent requests without sharing agent history;
  text and vision consume one persisted budget per graph.

## Known limits

Agents hold their own NATS passwords. A compromised agent can publish directly
within its subject ACL and bypass agentd's send-side kind, size and rate checks.
The generated per-graph NATS server config already sets `max_payload: 1048576`,
a broker-enforced 1 MiB ceiling on the entire encoded message, not a per-channel
body limit. Receiving agents still enforce each channel's kinds and `max_bytes`.
Send rate limits remain cooperative: there is no broker-side per-channel rate
limit, so direct publishing can flood allowed subjects. Subject ACLs still apply.


## Design space (exhaust-the-design-space)

| Fork | Options | Choice | Why |
|---|---|---|---|
| isolation unit | Docker container / KVM microVM | container | 15G/6c host; seconds to provision; blindness needs information isolation, which netns+ACLs give; user chose container |
| broker | NATS / MQTT / in-process | NATS 2.x, one container per graph | per-user publish/subscribe allow-lists are exactly the channel filter; single static binary; no plugins |
| agent↔agent reachability | shared bridge / per-agent bridge | per-agent bridge + nats multi-homed | makes cross-agent IP reachability structurally impossible (I1) |
| state | DB / shared KV / none | none | subtract-before-you-add: graph.json + per-agent markdown files + NATS are enough; a DB would become a de-facto shared brain and a blindness leak |
| chart parse | vision LLM / only JSON paste | both | vision is the product UX; JSON paste is the escape hatch when no vision-capable key is configured |
| computer use | remote sandbox API / shared browser MCP (Bladebro) / Playwright per agent | Playwright inside each agent's container | no remote provider available; a shared browser would share cookies/sessions and break blindness (I1); per-agent Chromium keeps isolation structural — also the stack Lauren Tan's pstack uses |

## Layout on hub (follows hub convention)

- Repo: `~/chart-to-orc` (git). Data: `~/chart-to-orc/data/<graph>/{graph.json,status.json,drawing.*,agents/<id>.md}`.
- Secrets: `~/.config/chart-to-orc/env` (chmod 600) — `DEEPSEEK_API_KEY`, optional
  `OPENAI_API_KEY` / `ORC_VISION_*` for the vision parser.
- Serve: main uvicorn API on loopback `127.0.0.1:8787`, separate proxy on
  `0.0.0.0:8788` by default. See README for bridge ingress and private UI access.
- Agent brain: DeepSeek (`deepseek-chat`, OpenAI-compatible) — the key already present on
  hub for spons (`/etc/spons/environment`); copy into the chart-to-orc env file.

## Stolen from pstack (cursor/plugins, Lauren Tan)

| Principle | Applied as |
|---|---|
| model-the-domain | `app/schema.py` encodes agents/channels/filters/envelope; NATS conf and subject names are derived artifacts of the model, never re-derived elsewhere |
| boundary-discipline | graph.json + envelope are the only cross-module types; every module imports them from `schema.py` |
| encode-lessons-in-structure | blindness = ACLs + netns (I1–I6), not prompt instructions |
| prove-it-works | P1–P7 above; every phase ends with the observable check, not "it compiles" |
| test-behavior-not-implementation | schema tests assert outcomes (permissions generated, invalid graphs rejected), not internals |
| attack-the-premise | I1–I6 were produced by asking "how would an agent break blindness?" first |
| subtract-before-you-add | no DB, no KV, no queue beyond NATS, no homegrown computer use |
| make-operations-idempotent | provision converges on re-run (P7) |
| outcome-oriented-execution | phases land in verifiable slices: schema → parse → provision → agentd → E2E |

## Open items

- Vision parsing: DeepSeek `deepseek-flash` accepts image input on the standard chat
  endpoint — same key as the agent brain. Default vision model is `deepseek-flash`.
- Computer use: each agent owns a headless Chromium via Playwright inside its container
  (`browser_goto` / `content` / `click` / `fill` / `screenshot` + vision describe via
  `deepseek-flash`). No external provider needed.
