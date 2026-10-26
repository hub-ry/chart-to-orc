const $ = (id) => document.getElementById(id);
const msg = (text, isErr) => {
  $("msg").textContent = text;
  $("msg").className = isErr ? "error" : "muted";
};

async function health() {
  try {
    const r = await fetch("/health");
    const h = await r.json();
    $("health").textContent =
      `llm:${h.llm_ready ? "ready" : "no key"} · vision:${h.vision_ready ? "ready" : "no key"} · manus:${h.manus_ready ? "ready" : "no key"}`;
    $("compile-drawing").disabled = !h.vision_ready;
  } catch { $("health").textContent = "backend unreachable"; }
}

function renderGraph(g) {
  $("result").hidden = false;
  const agents = g.agents.map((a) =>
    `<div class="agent"><b>${esc(a.name || a.id)}</b> <span class="muted">${esc(a.id)}</span>` +
    `<div class="muted">${esc(a.system || "")}</div>` +
    `<div class="muted">tools: ${esc(a.tools.join(", "))}</div></div>`).join("");
  const channels = g.channels.map((c) =>
    `<div class="channel">${esc(c.from)} <span class="arrow">→</span> ${esc(c.to)}` +
    `<span class="muted"> · ${esc(c.name || c.id)} · filter: ${esc(c.filter.kinds.join(","))}, ≤${c.filter.max_bytes}B, ${c.filter.rate_per_min}/min</span></div>`).join("");
  $("graph").innerHTML =
    `<div class="muted">${esc(g.name)} — ${g.agents.length} agents, ${g.channels.length} channels — ${g.status}</div>` +
    `<div class="agents">${agents}</div>${channels || '<div class="muted">no channels</div>'}`;
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

$("drawing").addEventListener("change", () => {
  const f = $("drawing").files[0];
  if (f) {
    $("preview").hidden = false;
    $("preview").src = URL.createObjectURL(f);
  }
});

$("compile-drawing").addEventListener("click", async () => {
  const f = $("drawing").files[0];
  if (!f) return msg("pick a drawing first", true);
  const fd = new FormData();
  fd.append("image", f);
  msg("parsing…");
  try {
    const r = await fetch("/api/graphs", { method: "POST", body: fd });
    const body = await r.json();
    if (!r.ok) return msg(body.detail || "failed", true);
    msg("compiled");
    renderGraph(body);
    listGraphs();
  } catch (e) { msg(String(e), true); }
});

$("compile-spec").addEventListener("click", async () => {
  const fd = new FormData();
  fd.append("spec", $("spec").value);
  msg("validating…");
  try {
    const r = await fetch("/api/graphs", { method: "POST", body: fd });
    const body = await r.json();
    if (!r.ok) return msg(body.detail || "failed", true);
    msg("compiled");
    renderGraph(body);
    listGraphs();
  } catch (e) { msg(String(e), true); }
});

async function listGraphs() {
  const r = await fetch("/api/graphs");
  const { graphs } = await r.json();
  $("graphs").innerHTML = graphs.map((g) =>
    `<li data-name="${esc(g.name)}">${esc(g.name)} <span class="muted">· ${g.agents} agents, ${g.channels} channels · ${g.status}</span></li>`).join("");
}

$("graphs").addEventListener("click", async (ev) => {
  const li = ev.target.closest("li");
  if (!li) return;
  const r = await fetch(`/api/graphs/${li.dataset.name}`);
  renderGraph(await r.json());
});

health();
listGraphs();
