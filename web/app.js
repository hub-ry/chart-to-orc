const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { name: null, status: "pending", agentId: null };
let pollTimer = null;
let trafficNext = 0;

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

/* ---- markdown renderer (escape-first, no external libs) ---- */
function inlineMd(s) {
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  s = s.replace(/\*([^*]+)\*/g, "<i>$1</i>");
  s = s.replace(/_([^_]+)_/g, "<i>$1</i>");
  return s;
}

function renderMarkdown(md) {
  const lines = String(md).split("\n");
  const out = [];
  let inCode = false, codeBuf = [], listBuf = [];
  const flushList = () => {
    if (listBuf.length) {
      out.push("<ul>" + listBuf.map((li) => `<li>${li}</li>`).join("") + "</ul>");
      listBuf = [];
    }
  };
  for (const raw of lines) {
    if (raw.trimStart().startsWith("```")) {
      if (inCode) {
        out.push(`<pre><code>${esc(codeBuf.join("\n"))}</code></pre>`);
        codeBuf = [];
        inCode = false;
      } else { flushList(); inCode = true; }
      continue;
    }
    if (inCode) { codeBuf.push(raw); continue; }
    const h = raw.match(/^(#{1,3})\s+(.*)$/);
    if (h) { flushList(); out.push(`<h${h[1].length}>${inlineMd(esc(h[2]))}</h${h[1].length}>`); continue; }
    const li = raw.match(/^\s*[-*]\s+(.*)$/);
    if (li) { listBuf.push(inlineMd(esc(li[1]))); continue; }
    flushList();
    if (!raw.trim()) continue;
    out.push(`<p>${inlineMd(esc(raw))}</p>`);
  }
  flushList();
  if (inCode) out.push(`<pre><code>${esc(codeBuf.join("\n"))}</code></pre>`);
  return out.join("");
}

/* ---- detail view ---- */
function renderDetail(g) {
  $("result").hidden = false;
  state.name = g.name;
  state.status = g.status || "pending";
  const live = state.status === "live";
  const badge = `<span class="badge ${live ? "live" : "pending"}">${esc(state.status)}</span>`;
  const agents = g.agents.map((a) =>
    `<div class="agent${live ? " clickable" : ""}" data-agent-id="${esc(a.id)}">` +
    `<b>${esc(a.name || a.id)}</b> <span class="muted">${esc(a.id)}</span>` +
    `<div class="muted">${esc(a.system || "")}</div>` +
    `<div class="muted">tools: ${esc((a.tools || []).join(", "))}</div>` +
    (live ? `<div class="hint muted">click to open</div>` : "") + `</div>`).join("");
  const channels = g.channels.map((c) => {
    const f = c.filter || {};
    return `<div class="channel">${esc(c.from ?? c.from_)} <span class="arrow">→</span> ${esc(c.to)}` +
      `<span class="muted"> · ${esc(c.name || c.id)} · ${esc((f.kinds || []).join(","))}, ≤${f.max_bytes}B, ${f.rate_per_min}/min</span></div>`;
  }).join("");
  $("graph").innerHTML =
    `<div class="detail-head">` +
    `<span class="muted">${esc(g.name)} — ${g.agents.length} agents, ${g.channels.length} channels</span> ${badge}` +
    `<button id="accept" class="primary">accept</button>` +
    `<button id="delete" class="danger">delete</button>` +
    `</div>` +
    `<p id="detail-error" class="error" hidden></p>` +
    `<div class="agents">${agents}</div>` +
    (channels || `<div class="muted">no channels</div>`);
  $("accept").onclick = acceptGraph;
  $("delete").onclick = deleteGraph;
  $("graph").querySelectorAll(".agent").forEach((el) => {
    el.onclick = () => { if (state.status === "live") openPanel(el.dataset.agentId); };
  });
}

async function acceptGraph() {
  const btn = $("accept");
  btn.disabled = true;
  try {
    const r = await fetch(`/api/graphs/${state.name}/accept`, { method: "POST" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return showDetailError(body.detail || `accept failed (${r.status})`);
    clearDetailError();
    const g = await fetch(`/api/graphs/${state.name}`).then((x) => x.json());
    renderDetail(g);
    listGraphs();
  } catch (e) { showDetailError(String(e)); }
  finally { btn.disabled = false; }
}

async function deleteGraph() {
  if (!confirm(`Delete graph "${state.name}"?`)) return;
  const btn = $("delete");
  btn.disabled = true;
  try {
    const r = await fetch(`/api/graphs/${state.name}`, { method: "DELETE" });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return showDetailError(body.detail || `delete failed (${r.status})`);
    closePanel();
    $("result").hidden = true;
    state.name = null;
    listGraphs();
  } catch (e) { showDetailError(String(e)); }
  finally { btn.disabled = false; }
}

const showDetailError = (t) => { const e = $("detail-error"); e.textContent = t; e.hidden = false; };
const clearDetailError = () => { const e = $("detail-error"); e.textContent = ""; e.hidden = true; };

/* ---- agent panel ---- */
async function openPanel(agentId) {
  state.agentId = agentId;
  $("panel-title").textContent = `agent · ${agentId}`;
  $("panel-error").hidden = true;
  $("traffic").innerHTML = "";
  trafficNext = 0;
  $("agent-panel").hidden = false;
  loadMarkdown();
  pollTraffic();
  $("agent-panel").scrollIntoView({ behavior: "smooth", block: "start" });
}

function closePanel() {
  stopPolling();
  $("agent-panel").hidden = true;
  state.agentId = null;
}

async function loadMarkdown() {
  const view = $("md-view");
  view.textContent = "loading…";
  try {
    const r = await fetch(`/api/graphs/${state.name}/agents/${state.agentId}`);
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      view.innerHTML = `<span class="error">${esc(body.detail || `failed (${r.status})`)}</span>`;
      return;
    }
    view.innerHTML = renderMarkdown(await r.text());
  } catch (e) {
    view.innerHTML = `<span class="error">${esc(String(e))}</span>`;
  }
}

async function sendPrompt() {
  const text = $("prompt").value;
  if (!text.trim()) return;
  const btn = $("send-prompt");
  btn.disabled = true;
  try {
    const r = await fetch(`/api/graphs/${state.name}/agents/${state.agentId}/prompt`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: text }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      $("panel-error").textContent = body.detail || `prompt failed (${r.status})`;
      $("panel-error").hidden = false;
      return;
    }
    $("panel-error").hidden = true;
    $("prompt").value = "";
    loadMarkdown();
  } catch (e) {
    $("panel-error").textContent = String(e);
    $("panel-error").hidden = false;
  } finally { btn.disabled = false; }
}

async function pollTraffic() {
  if (!state.agentId) return;
  try {
    const r = await fetch(`/api/graphs/${state.name}/traffic?after=${trafficNext}`);
    if (!state.agentId) return; // panel closed mid-poll
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      if (!$("traffic").children.length) {
        $("traffic").innerHTML = `<li class="muted">${esc(body.detail || `traffic unavailable (${r.status})`)}</li>`;
      }
    } else {
      const data = await r.json();
      for (const line of data.lines || []) appendTraffic(line);
      trafficNext = data.next ?? trafficNext;
    }
  } catch (e) { /* transient; keep polling */ }
  if (!state.agentId) return; // panel closed mid-poll
  pollTimer = setTimeout(pollTraffic, 2000);
}

function appendTraffic(line) {
  let preview;
  if (line.body && typeof line.body.text === "string") preview = line.body.text;
  else if (line.body !== undefined) preview = JSON.stringify(line.body);
  else preview = "";
  if (preview.length > 200) preview = preview.slice(0, 200) + "…";
  const li = document.createElement("li");
  li.className = "traffic-line";
  li.innerHTML =
    `<span class="muted">${esc(line.ts || "")}</span> <b>${esc(line.src || "")}</b>` +
    `<span class="arrow">→</span> <span class="muted">${esc(line.ch || "")}</span>` +
    `<span class="kind">${esc(line.kind || "")}</span>` +
    `<div class="body muted">${esc(preview)}</div>`;
  $("traffic").appendChild(li);
}

function stopPolling() {
  if (pollTimer) { clearTimeout(pollTimer); pollTimer = null; }
}

/* ---- flows ---- */
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
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return msg(body.detail || "failed", true);
    msg("compiled");
    renderDetail(body);
    listGraphs();
  } catch (e) { msg(String(e), true); }
});

$("compile-spec").addEventListener("click", async () => {
  const fd = new FormData();
  fd.append("spec", $("spec").value);
  msg("validating…");
  try {
    const r = await fetch("/api/graphs", { method: "POST", body: fd });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return msg(body.detail || "failed", true);
    msg("compiled");
    renderDetail(body);
    listGraphs();
  } catch (e) { msg(String(e), true); }
});

async function listGraphs() {
  try {
    const r = await fetch("/api/graphs");
    const { graphs } = await r.json();
    $("graphs").innerHTML = graphs.map((g) =>
      `<li data-name="${esc(g.name)}">${esc(g.name)} <span class="muted">· ${g.agents} agents, ${g.channels} channels · ${g.status}</span></li>`).join("");
  } catch (e) { $("graphs").innerHTML = `<li class="error">${esc(String(e))}</li>`; }
}

$("graphs").addEventListener("click", async (ev) => {
  const li = ev.target.closest("li");
  if (!li) return;
  try {
    const r = await fetch(`/api/graphs/${li.dataset.name}`);
    const body = await r.json().catch(() => ({}));
    if (!r.ok) return msg(body.detail || `failed (${r.status})`, true);
    closePanel();
    renderDetail(body);
  } catch (e) { msg(String(e), true); }
});

$("panel-close").addEventListener("click", closePanel);
$("send-prompt").addEventListener("click", sendPrompt);

health();
listGraphs();
