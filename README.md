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
   exactly the drawn edges. The orchestrator then has no runtime role.
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
uv run uvicorn app.main:app --host 0.0.0.0 --port 8787
```

Open `http://hub:8787` (or the tailnet IP). Deploy as a service with
`deploy/chart-to-orc.service` (`sudo cp` to `/etc/systemd/system/`, `systemctl enable --now`).

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
with shell + workspace tools in its own container; per-agent prompt + live traffic.
See [DESIGN.md](DESIGN.md) proof table P1–P7.
