"use strict";
// Token arrives in the URL fragment (never sent to servers / logged) and lives only in sessionStorage.
if (location.hash.startsWith("#token=")) { sessionStorage.setItem("t", location.hash.slice(7)); history.replaceState(null, "", location.pathname); }
let TOKEN = sessionStorage.getItem("t") || "";
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const toast = (m, ms = 3000) => { const t = $("#toast"); t.textContent = m; t.style.display = "block"; clearTimeout(toast._t); toast._t = setTimeout(() => t.style.display = "none", ms); };
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function refreshToken() {
  try { const r = await fetch("/bootstrap", { cache: "no-store" }); if (r.ok) { TOKEN = (await r.json()).token; sessionStorage.setItem("t", TOKEN); tokenBad = false; return true; } } catch { }
  return false;
}
async function api(path, method = "GET", body, raw, retried) {
  const r = await fetch("/api" + path, { method, cache: "no-store", headers: { "X-RAM-Token": TOKEN, "Content-Type": "application/json" }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (r.status === 401 && !retried && await refreshToken()) return api(path, method, body, raw, true);
  if (raw) return r;
  let d = null; try { d = await r.json(); } catch { }
  if (!r.ok) throw new Error((d && d.detail) || r.statusText);
  return d;
}

var S = null, devices = [], tokenBad = false;
const remoteSel = () => { const id = $("#device").value; const d = devices.find(x => x.id === id); return d && !d.this_pc ? d : null; };

// ---------------------------------------------------------------- Markdown (safe) for agent replies
function md(src) {
  const inline = s => esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/__([^_]+)__/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, "$1<i>$2</i>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  const lines = String(src ?? "").replace(/\r/g, "").split("\n");
  let out = "", i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) { let buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++; out += `<pre>${esc(buf.join("\n"))}</pre>`; continue; }
    if (/^\s*\|.*\|\s*$/.test(l)) {
      const rows = []; while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++]);
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      const body = rows.filter(r => !/^[\s|:-]+$/.test(r));
      out += "<table>" + body.map((r, k) => "<tr>" + cells(r).map(c => k === 0 ? `<th>${inline(c)}</th>` : `<td>${inline(c)}</td>`).join("") + "</tr>").join("") + "</table>";
      continue;
    }
    if (/^\s*([-*•]|\d+[.)])\s+/.test(l)) {
      const ordered = /^\s*\d+[.)]\s+/.test(l); const items = [];
      while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
      out += (ordered ? "<ol>" : "<ul>") + items.map(t => `<li>${inline(t)}</li>`).join("") + (ordered ? "</ol>" : "</ul>");
      continue;
    }
    const h = l.match(/^#{1,6}\s+(.*)$/);
    if (h) { out += `<p class="h">${inline(h[1])}</p>`; i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(l) || !l.trim()) { i++; continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\s*\||\s*([-*•]|\d+[.)])\s+)/.test(lines[i])) para.push(lines[i++]);
    out += `<p>${para.map(inline).join("<br>")}</p>`;
  }
  return out;
}

// ---------------------------------------------------------------- tabs
for (const v of ["main", "sched", "hist", "set"]) $("#tab-" + v).onclick = () => {
  for (const x of ["main", "sched", "hist", "set"]) { $("#view-" + x).classList.toggle("hidden", x !== v); $("#tab-" + x).classList.toggle("active", x === v); }
  if (v === "hist") loadHistory(); if (v === "set") renderSettings(); if (v === "sched") loadSchedules();
};

// ---------------------------------------------------------------- schedules
(function initSchedForm() {
  const days = $("#sc-days"); days.innerHTML = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].map(d => `<label class="inline"><input type="checkbox" value="${d}"> ${d[0].toUpperCase() + d.slice(1)}</label>`).join(" ");
  const kind = $("#sc-kind");
  const sync = () => {
    const k = kind.value;
    $("#sc-date").classList.toggle("hidden", k !== "once"); $("#sc-days").classList.toggle("hidden", k !== "weekly");
    $("#sc-dom").classList.toggle("hidden", k !== "monthly"); $("#sc-every").classList.toggle("hidden", k !== "interval");
    $("#sc-time").classList.toggle("hidden", k === "interval");
  };
  kind.onchange = sync; sync();
  const d = new Date(Date.now() + 86400000); $("#sc-date").value = d.toISOString().slice(0, 10);
  $("#sc-add").onclick = async () => {
    const msg = $("#sc-msg");
    const body = { title: $("#sc-title").value.trim(), instruction: $("#sc-ins").value.trim(), kind: kind.value, time: $("#sc-time").value,
      date: $("#sc-date").value, days: [...days.querySelectorAll("input:checked")].map(x => x.value), day_of_month: +$("#sc-dom").value,
      every_minutes: +$("#sc-every").value, allowed: $("#sc-allowed").value };
    if (body.instruction.length < 10) { msg.className = "msg err"; msg.textContent = "Describe what it should do."; return; }
    if (body.allowed && !confirm("This schedule may send to: " + body.allowed + "\nwithout asking you each time. OK?")) return;
    try { const r = await api("/schedules", "POST", body); msg.className = "msg ok"; msg.textContent = `Scheduled “${r.title}” — ${r.when}. First run: ${r.next_run_text}.`; $("#sc-ins").value = ""; $("#sc-title").value = ""; loadSchedules(); }
    catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
  };
  $("#sc-auto").onchange = async e => { try { const r = await api("/autostart", "POST", { enabled: e.target.checked }); toast(r.enabled ? "RAM AI will start when you sign in to Windows" : "Autostart off"); } catch (x) { toast(x.message); } };
})();

async function loadSchedules() {
  try { $("#sc-auto").checked = (await api("/autostart")).enabled; } catch { }
  const list = await api("/schedules"), box = $("#sc-list");
  box.innerHTML = list.length ? list.map(s => `<div class="sched ${s.enabled ? "" : "off"}" data-id="${esc(s.id)}">
      <div class="sched-top"><b>${esc(s.title)}</b><span class="pill ${s.enabled ? "on" : ""}">${s.enabled ? "active" : "paused"}</span><span class="spacer"></span>
        <button class="ghost s-run">Run now</button><button class="ghost s-tog">${s.enabled ? "Pause" : "Resume"}</button><button class="ghost s-del">Delete</button></div>
      <div class="muted small">🕘 ${esc(s.when)} · next: <b>${esc(s.enabled ? s.next_run_text : "—")}</b> · last: ${esc(s.last_run_text)}${s.last_status ? " (" + esc(s.last_status) + ")" : ""}</div>
      <div class="small">${esc(s.instruction)}</div>
      <div class="muted small">Sends without asking to: ${s.allowed.length ? esc(s.allowed.join(", ")) : "nobody — asks you first"}</div></div>`).join("")
    : '<p class="muted">No schedules yet.</p>';
  box.querySelectorAll(".sched").forEach(el => {
    const id = el.dataset.id;
    el.querySelector(".s-run").onclick = async () => { const r = await api(`/schedules/${id}/run`, "POST"); toast("Started — see the Agent tab"); trackTask(r.task_id); };
    el.querySelector(".s-tog").onclick = async () => { await api(`/schedules/${id}/enabled`, "POST", { enabled: el.classList.contains("off") }); loadSchedules(); };
    el.querySelector(".s-del").onclick = async () => { if (confirm("Delete this schedule?")) { await api(`/schedules/${id}`, "DELETE"); loadSchedules(); } };
  });
}
async function trackTask(id) {
  if (tasks.has(id)) return;
  try { const r = await api(`/tasks/${id}/events`);
    const t = { id, instruction: r.instruction, device: r.device, events: r.events, lastId: r.events.length ? r.events[r.events.length - 1].id : 0, state: r.state, result: r.result, error: r.error, created: r.created };
    tasks.set(id, t); renderTask(t); } catch { }
}
// scheduled runs start on their own: pick them up in the Agent feed
setInterval(async () => { try { for (const r of (await api("/tasks")).slice(0, 5)) if (!tasks.has(r.id) && !["Completed", "Failed", "Cancelled"].includes(r.state)) trackTask(r.id); } catch { } }, 5000);

// ---------------------------------------------------------------- status + setup wizard
async function loadStatus() {
  try { S = await api("/status"); tokenBad = false; }
  catch (e) { if (!tokenBad) { tokenBad = true; showSystem("Cannot reach the agent (" + e.message + "). Close this window and re-open RAM AI from Start_RAM_AI.bat."); } return; }
  if (!S.configured) $("#wizard").classList.remove("hidden");
}
function showSystem(text) { const d = document.createElement("div"); d.className = "final err"; d.textContent = text; $("#ap-misc").appendChild(d); }
$("#w-prov").onchange = () => { const l = $("#w-prov").value === "local"; $("#w-ds").classList.toggle("hidden", l); $("#w-lo").classList.toggle("hidden", !l); $("#w-keylbl").firstChild.textContent = { openai: "OpenAI API key", sarvam: "Sarvam API subscription key" }[$("#w-prov").value] || "DeepSeek API key"; };
$("#w-go").onclick = async () => {
  const msg = $("#w-msg"), key = $("#w-key").value.trim(), prov = $("#w-prov").value, local = prov === "local";
  msg.className = "msg"; msg.textContent = "Saving…";
  try {
    if (!$("#w-user").value.trim()) { msg.className = "msg err"; msg.textContent = "Please enter your name."; return; }
    await api("/settings", "POST", { device_name: $("#w-name").value.trim() || "My PC", user_name: $("#w-user").value.trim(), llm_provider: prov,
      ...(local ? { local_base_url: $("#w-lurl").value.trim(), local_model: $("#w-lmodel").value.trim() } : {}) });
    if (!local) { await api("/secrets/" + ({ openai: "openai", sarvam: "sarvam" }[prov] || "deepseek"), "POST", { value: key }); $("#w-key").value = ""; }
    const r = await api("/llm/test", "POST", { provider: prov });
    if (!r.ok) { msg.className = "msg err"; msg.textContent = "Connection failed: " + r.error; return; }
    if (!r.selected_available && r.models.length) {
      const pick = local ? r.models[0] : (r.models.find(m => /chat|flash/i.test(m)) || r.models[0]);
      await api("/llm/select", "POST", { provider: prov, model: pick });
    }
    msg.className = "msg ok"; msg.textContent = "Connected. " + r.models.length + " model(s) available.";
    setTimeout(() => { $("#wizard").classList.add("hidden"); boot(); }, 900);
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
};

// ---------------------------------------------------------------- devices + model switch
async function loadDevices() {
  try { devices = await api("/devices"); } catch { return; }
  const sel = $("#device"), cur = sel.value;
  sel.innerHTML = devices.map(d => `<option value="${esc(d.id)}">🖥 ${esc(d.name)}${d.this_pc ? " (this PC)" : ""}${d.online ? "" : " — offline"}</option>`).join("");
  if (devices.some(d => d.id === cur)) sel.value = cur;
  const d = devices.find(x => x.id === sel.value);
  const p = $("#devstate"); p.textContent = d ? (d.online ? "online" : "offline") : "…"; p.className = "pill " + (d && d.online ? "on" : "off");
}
$("#device").onchange = () => { loadDevices(); winlog.length = 0; renderWinlog(); };
async function loadModelSwitch() {
  try {
    const st = await api("/status"); const sel = $("#llm");
    sel.innerHTML = st.settings.llm_providers.map(p => `<option value="${esc(p.id)}" ${p.active ? "selected" : ""} ${p.ready ? "" : "disabled"}>✦ ${esc(p.label)}${p.model ? " · " + esc(p.model) : ""}${p.ready ? "" : " (not set up)"}</option>`).join("");
  } catch { }
}
$("#llm").onchange = async () => { try { const r = await api("/llm/select", "POST", { provider: $("#llm").value }); toast("AI model: " + r.model); } catch (e) { toast(e.message); } loadModelSwitch(); };

// ================================================================ RIGHT PANE: conversation timeline
const tasks = new Map();   // id -> {id, instruction, device, remote, events[], lastId, state, result, error, thinking_since, el}
let followBottom = true;
$("#timeline").addEventListener("scroll", e => { const t = e.target; followBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 80; });
const scrollDown = () => { if (followBottom) $("#timeline").scrollTop = 1e9; };
const base = p => String(p || "").split(/[\\/]/).pop();
const short = (s, n = 60) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
const parse = d => { try { return JSON.parse(d); } catch { return {}; } };
const keysTxt = k => (k || []).map(x => x.length === 1 ? x.toUpperCase() : x[0].toUpperCase() + x.slice(1)).join("+");

function stepLabel(tool, a) {
  a = a || {};
  const L = {
    screenshot: () => "Looked at the screen", describe_screen: () => "Analysed the screen",
    click: () => `${a.double ? "Double-clicked" : a.button === "right" ? "Right-clicked" : "Clicked"} at (${a.x}, ${a.y})`,
    mouse_move: () => `Moved the pointer to (${a.x}, ${a.y})`, type_text: () => `Typed “${short(a.text, 50)}”`,
    hotkey: () => `Pressed ${keysTxt(a.keys)}`, scroll: () => `Scrolled ${a.amount > 0 ? "up" : "down"}`, wait: () => `Waited ${a.seconds}s`,
    launch_app: () => `Opened ${a.name}${a.prefer_web ? " (web)" : ""}`, focus_window: () => `Switched to “${short(a.title_contains, 40)}”`,
    list_windows: () => "Checked open windows", screen_info: () => "Checked the displays",
    ui_elements: () => "Read the window's controls", read_screen_text: () => "Read the text on screen", click_text: () => `Clicked “${short(a.text, 40)}”`, ui_click: () => `Clicked “${short(a.name, 40)}”`, ui_type: () => `Typed into “${short(a.name, 30)}”`,
    open_url: () => `Opened ${short(a.url, 50)}`,
    whatsapp_send_message: () => `WhatsApp ${a.contact}: “${short(a.text, 50)}”`, whatsapp_prepare_message: () => `Prepared WhatsApp to ${a.contact}`,
    whatsapp_send_prepared: () => `Sent WhatsApp to ${a.contact}`, whatsapp_find_chat: () => `Searched WhatsApp for “${a.query}”`,
    whatsapp_read_chat: () => `Read WhatsApp chat ${a.contact}`, whatsapp_open: () => "Opened WhatsApp",
    send_email: () => `Email to ${(a.to || []).join(", ") || "reply"}: “${short(a.subject || a.body, 40)}”`,
    outlook_search: () => `Searched Outlook for “${a.query}”`, outlook_list_messages: () => `Listed mail ${a.since === "today" || !a.since ? "received today" : "since " + a.since}${a.sender ? " from " + a.sender : ""}`,
    outlook_read_many: () => `Read ${(a.ids || []).length} email${(a.ids || []).length === 1 ? "" : "s"}`, outlook_read: () => "Read an email", outlook_find_person: () => `Looked up ${a.name} in Outlook`,
    outlook_create_draft: () => "Drafted an email", send_email_draft: () => "Sent the email draft", outlook_download_attachment: () => "Downloaded an attachment",
    list_folder: () => `Looked in ${base(a.path) || a.path}`, search_files: () => `Searched files for “${a.query}”`, read_file: () => `Read ${base(a.path)}`,
    read_sheet: () => `Read sheet ${base(a.path)}`, create_folder: () => `Created folder ${base(a.path)}`, move_files: () => `Moved ${(a.moves || []).length} file(s)`,
    move_file: () => `Moved ${base(a.src)}`, copy_file: () => `Copied ${base(a.src)}`, delete_file: () => `Deleted ${base(a.path)} (Recycle Bin)`,
    write_text_file: () => `Wrote ${base(a.path)}`, create_workbook: () => `Created ${base(a.path)}`, calculate: () => `Calculated ${short(a.expression, 40)}`,
    weighted_average: () => "Calculated a weighted average", list_approved_folders: () => "Checked allowed folders",
    remember: () => `Remembered “${a.key}”`, create_document: () => `Created ${base(a.path)}`, edit_workbook: () => `Edited ${base(a.path)}`,
    web_search: () => `Searched the web for “${short(a.query, 50)}”`, web_fetch: () => `Read ${short((a.url || "").replace(/^https?:\/\//, ""), 50)}`, recall: () => "Checked saved preferences", forget: () => `Forgot “${a.key}”`,
    set_plan: () => `Planned ${(a.steps || []).length} steps`,
    ask_chatgpt: () => `Asked ChatGPT: “${short(a.prompt, 50)}”`,
    run_command: () => `Ran ${a.shell === "cmd" ? "cmd" : "PowerShell"}: ${short(a.command, 60)}`, run_python: () => `Ran a Python script${(a.packages || []).length ? " (+" + a.packages.join(", ") + ")" : ""}`,
    install_package: () => `Installed ${a.name} (${a.kind})`, list_skills: () => "Checked skills", load_skill: () => `Loaded skill “${a.name}”`, save_skill: () => `Saved skill “${a.name}”`, schedule_create: () => `Scheduled “${short(a.title, 40)}”`, schedule_list: () => "Checked schedules",
    schedule_delete: () => "Deleted a schedule", copy_window_text: () => `Copied the text of ${a.title_contains ? "“" + short(a.title_contains, 30) + "”" : "the window"}`, ask_user: () => "Asked you a question",
    list_devices: () => "Checked paired computers", transfer_file: () => `Sent ${base(a.path)} to ${a.peer}`,
  };
  return (L[tool] || (() => tool.replace(/_/g, " ")))();
}

function buildItems(t) {
  const items = [];
  const open = [];
  for (const e of t.events) {
    const d = parse(e.data);
    switch (e.event) {
      case "thought": items.push({ k: "thought", text: d.text, secs: d.secs }); break;
      case "tool_call": { const it = { k: "step", tool: e.tool, args: d.args || {}, st: "run", ts: e.ts }; items.push(it); open.push(it); break; }
      case "tool_result": { const it = open.find(x => x.tool === e.tool && x.st === "run"); if (it) { it.st = "ok"; it.ms = d.ms; open.splice(open.indexOf(it), 1); } break; }
      case "tool_denied": case "tool_error": case "tool_timeout": {
        const why = d.reason || d.error || "timed out";
        const it = open.find(x => x.tool === e.tool && x.st === "run");
        if (it) { it.st = "fail"; it.why = why; open.splice(open.indexOf(it), 1); } else items.push({ k: "step", tool: e.tool, args: {}, st: "fail", why });
        break;
      }
      case "tool_invalid_args": items.push({ k: "note", cls: "bad", text: `Retried a malformed ${e.tool} call` }); break;
      case "tool_not_approved": items.push({ k: "note", cls: "bad", text: `Not approved: ${stepLabel(e.tool, {})}` }); break;
      case "auto_approved": items.push({ k: "note", cls: "good", text: `✓ Auto-approved ${e.tool === "control_session" ? "mouse & keyboard control" : e.tool.replace(/_/g, " ")}` }); break;
      case "approval_approved": items.push({ k: "note", cls: "good", text: "✓ You approved" }); break;
      case "approval_rejected": items.push({ k: "note", cls: "bad", text: "✗ You rejected" }); break;
      case "question_approved": items.push({ k: "note", text: `↳ You answered: “${short(d.answer, 80)}”` }); break;
      case "authorization_granted": items.push({ k: "note", cls: "good", text: "🔓 Authorization granted — folder limits lifted, no approval prompts for this task" }); break;
      case "task_pre_approved": items.push({ k: "note", cls: "good", text: "Pre-approved: actions in this task run without prompts" }); break;
      case "task_cancelled": items.push({ k: "note", cls: "bad", text: "Stopped" }); break;
      case "scheduled_run": items.push({ k: "note", cls: "good", text: `🕘 Scheduled run — may send without asking to: ${(d.allowed || []).join(", ") || "nobody"}` }); break;
    }
  }
  return items;
}

const fmtDur = sec => { sec = Math.max(0, Math.round(sec)); const m = Math.floor(sec / 60), s2 = sec % 60; return m ? `${m}m ${String(s2).padStart(2, "0")}s` : `${s2}s`; };
const nowSec = () => Date.now() / 1000;

function renderTask(t) {
  if (!t.el) {
    t.el = document.createElement("div"); t.el.className = "task"; t.el.id = "task-" + t.id;
    t.el.innerHTML = `<div class="u"></div><div class="meta"></div><div class="worklog"><button class="wl-head" type="button"></button><div class="steps"></div></div><div class="aps"></div><div class="fin"></div>`;
    t.el.querySelector(".u").textContent = t.instruction;
    t.el.querySelector(".wl-head").onclick = () => { t.open = !t.open; t.showAll = t.open; renderTask(t); };
    $("#tasks-feed").appendChild(t.el); $("#empty-hint").classList.add("hidden");
  }
  const running = !["Completed", "Failed", "Cancelled"].includes(t.state);
  t.el.querySelector(".meta").textContent = `${t.device || ""}${t.remote ? " (remote)" : ""} · ${new Date((t.created || nowSec()) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  const items = buildItems(t);
  const nSteps = items.filter(x => x.k === "step").length;
  const lastTs = t.events.length ? t.events[t.events.length - 1].ts : (t.created || nowSec());
  const dur = (running ? nowSec() + (t.skew || 0) : lastTs) - (t.created || lastTs);
  if (t.open === undefined) t.open = running;
  if (running) t.open = t.open || true;
  const head = t.el.querySelector(".wl-head");
  head.classList.toggle("live", running);
  head.innerHTML = running
    ? `<span class="spin"></span> Working · ${fmtDur(dur)} · ${nSteps} step${nSteps === 1 ? "" : "s"}`
    : `<span class="chev ${t.open ? "up" : ""}">›</span> Worked for ${fmtDur(dur)} · ${nSteps} step${nSteps === 1 ? "" : "s"}`;
  t.el.querySelector(".worklog").classList.toggle("hidden", !items.length && !running);
  const VISIBLE = 8;
  let shown = items, hiddenCount = 0;
  if (running && !t.showAll && items.length > VISIBLE) { hiddenCount = items.length - VISIBLE; shown = items.slice(-VISIBLE); }
  let html = hiddenCount ? `<button class="more" type="button">Show ${hiddenCount} earlier step${hiddenCount === 1 ? "" : "s"}</button>` : "";
  const lastThought = shown.map(x => x.k).lastIndexOf("thought");
  html += shown.map((it, idx) => {
    if (it.k === "thought") return `<div class="thought ${idx !== lastThought || !running ? "collapsed" : ""}"><span class="tt">✦ Thought${it.secs ? " for " + it.secs + "s" : ""}</span><div class="tx">${esc(it.text)}</div></div>`;
    if (it.k === "note") return `<div class="note ${it.cls || ""}">${esc(it.text)}</div>`;
    const t2 = it.ms != null ? (it.ms >= 1000 ? (it.ms / 1000).toFixed(1) + "s" : it.ms + "ms") : "";
    return `<div class="step ${it.st}"><span class="ic">${it.st === "ok" ? "✓" : it.st === "fail" ? "!" : ""}</span><span class="lbl">${esc(stepLabel(it.tool, it.args))}${it.why ? `<span class="why">${esc(short(it.why, 220))}</span>` : ""}</span><span class="t">${t2}</span></div>`;
  }).join("");
  if (running && t.thinking_since) html += `<div class="thinking"><span class="shimmer">✦ Thinking… ${Math.max(0, Math.round(nowSec() - t.thinking_since + (t.skew || 0)))}s</span></div>`;
  else if (running && t.state === "Awaiting approval") html += `<div class="thinking"><span class="shimmer">Waiting for you…</span></div>`;
  else if (running && !items.length) html += `<div class="thinking"><span class="shimmer">✦ Starting…</span></div>`;
  const steps = t.el.querySelector(".steps");
  steps.classList.toggle("hidden", !t.open);
  if (steps._h !== html) {
    steps.innerHTML = html; steps._h = html;
    steps.querySelectorAll(".thought .tt").forEach(x => x.onclick = () => x.parentElement.classList.toggle("collapsed"));
    const more = steps.querySelector(".more"); if (more) more.onclick = () => { t.showAll = true; renderTask(t); };
  }
  const fin = t.el.querySelector(".fin");
  const fh = t.state === "Completed" ? `<div class="final"><div class="who"><span class="logo">◆</span> RAM AI</div>${md(t.result)}</div>`
    : t.state === "Failed" ? `<div class="final err">${esc(t.error || "Failed")}</div>` : t.state === "Cancelled" ? `<div class="final err">${esc(t.error || "Stopped.")}</div>` : "";
  if (fin._h !== fh) { fin.innerHTML = fh; fin._h = fh; if (fh && t._wasRunning) { t.open = false; renderTask(t); } }
  t._wasRunning = running;
  const cur = [...items].reverse().find(x => x.k === "step" && x.st === "run");
  if (running) setCaption(cur ? stepLabel(cur.tool, cur.args) + "…" : t.thinking_since ? "Thinking…" : t.state === "Awaiting approval" ? "Waiting for your approval" : "");
  $("#stop").classList.toggle("idle", !activeTasks().length);
  scrollDown();
}

// approvals: rendered in the owning task (or at the top) and only rebuilt when they change, so typed answers survive
let apKey = "";
function renderApprovals(list, remote) {
  const key = JSON.stringify(list.map(a => [a.id, a.summary])) + (remote ? remote.id : "");
  if (key === apKey) return; apKey = key;
  document.querySelectorAll(".aps").forEach(x => x.innerHTML = ""); $("#ap-misc").querySelectorAll(".ap").forEach(x => x.remove());
  for (const a of list) {
    const el = document.createElement("div"); el.className = "ap"; el.dataset.id = a.id;
    const canAll = a.kind !== "question" && !["pair_device", "remember"].includes(a.tool);
    el.innerHTML = `<div class="tag">${a.kind === "question" ? "❓ QUESTION" : "⚠ APPROVAL NEEDED"}${remote ? " · on " + esc(remote.name) : ""}</div>
      <pre>${esc(a.summary)}</pre>${a.kind === "question" ? '<input class="ans" placeholder="Type your answer… (you can also say “authorization granted”)">' : ""}
      <div class="row">${a.kind === "question" ? '<button data-d="approve">Answer</button>' : '<button data-d="approve">Approve</button>'}
      <button class="ghost" data-d="reject">${a.kind === "question" ? "Skip" : "Reject"}</button>
      ${canAll ? '<button class="ghost" data-d="approve" data-s="task">Approve all for this task</button>' : ""}</div>`;
    const host = document.querySelector(`#task-${CSS.escape(a.task_id || "")} .aps`) || $("#ap-misc");
    host.appendChild(el);
    el.querySelector(".ans")?.addEventListener("keydown", e => { if (e.key === "Enter") el.querySelector('[data-d="approve"]').click(); });
    el.querySelectorAll("button").forEach(b => b.onclick = async () => {
      const body = { decision: b.dataset.d, answer: el.querySelector(".ans")?.value, scope: b.dataset.s || "once" };
      if (el.querySelector(".ans") && b.dataset.d === "approve" && !body.answer.trim()) { toast("Type an answer first"); return; }
      if (body.scope === "task" && !confirm("Approve this action AND every remaining action in this task without asking again?")) return;
      try {
        if (remote) await api(`/peers/${remote.id}/rpc`, "POST", { method: "decide_approval", params: { id: el.dataset.id, ...body } });
        else await api("/approvals/" + el.dataset.id, "POST", body);
        el.remove(); apKey = "";
      } catch (e) { toast(e.message); }
    });
    el.querySelector(".ans")?.focus();
  }
  if (list.length) scrollDown();
}

// ---------------------------------------------------------------- sending tasks
$("#form").onsubmit = async e => {
  e.preventDefault();
  const ins = $("#instr").value.trim(); if (!ins) return;
  const rd = remoteSel();
  if (rd && !rd.online) { toast("That computer is offline. The task was NOT queued."); return; }
  $("#instr").value = ""; followBottom = true;
  try {
    const r = await api("/tasks", "POST", { instruction: ins, device_id: rd ? rd.id : undefined, pre_approve: $("#preapp").checked });
    const t = { id: r.task_id, instruction: ins, device: r.device, remote: rd || null, events: [], lastId: 0, state: "Planning", created: Date.now() / 1000 };
    tasks.set(t.id, t); renderTask(t);
  } catch (e) { $("#instr").value = ins; toast(e.message, 5000); }
};
// ---------------------------------------------------------------- hand-over: you fix something, the agent carries on
async function pollTakeover() {
  let st; try { st = await api("/takeover"); } catch { return; }
  $("#takectl").classList.toggle("on", st.paused);
  $("#takectl").textContent = st.paused ? "▶ Resume" : "🖐 Take control";
  const w = st.waiting, bar = $("#tkbar");
  if (w || st.paused) {
    $("#tkmsg").textContent = w ? (w.paused ? "You have control. RAM AI is waiting - press Resume (or Ctrl+Alt+P) when you're done."
      : "You're using the mouse/keyboard, so RAM AI paused. It continues 3 s after you stop, and checks the screen first.")
      : "Take control is on: RAM AI will pause before its next screen action. Press Resume when you're done.";
    bar.hidden = false;
  } else bar.hidden = true;
}
async function setTakeover(on) { try { await api("/takeover", "POST", { on }); } catch (e) { toast(e.message); } pollTakeover(); }
$("#takectl").onclick = () => setTakeover(!$("#takectl").classList.contains("on"));
$("#tkresume").onclick = () => setTakeover(false);
setInterval(() => { if (activeTasks().length || !$("#tkbar").hidden || $("#takectl").classList.contains("on")) pollTakeover(); }, 1000);

// ---------------------------------------------------------------- voice input: HOLD 🎤 (or Ctrl+M) while you speak, let go to send
// Edge speech recognition (no Windows setting needed); Windows speech as a fallback.
const voice = { rec: null, held: false, via: "", base: "", finalText: "", interim: "", failed: false, t0: 0 };
function micUI(on) { $("#mic").classList.toggle("on", on); $("#instr").classList.toggle("listening", on); $("#mic").textContent = on ? "● REC" : "🎤"; }
function showVoiceText() { $("#instr").value = (voice.base + " " + voice.finalText + voice.interim).replace(/\s+/g, " ").trim(); }
async function voiceServer() {   // Windows' own recognizer: one sentence, then sends
  micUI(true); toast("Listening… (Windows speech)", 6000);
  try { const r = await api("/voice/listen", "POST", { lang: S?.settings?.voice_lang }); micUI(false); $("#instr").value = (voice.base + " " + r.text).trim(); if (r.text) $("#form").requestSubmit(); }
  catch (e) { micUI(false); toast(e.message, 9000); }
}
function makeRec() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const r = new SR();
  r.lang = S?.settings?.voice_lang || "en-IN"; r.continuous = true; r.interimResults = true;
  r.onresult = e => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) { const t = e.results[i][0].transcript; if (e.results[i].isFinal) voice.finalText += t + " "; else interim += t; }
    voice.interim = interim; showVoiceText();
  };
  r.onerror = e => {
    if (e.error === "not-allowed" || e.error === "service-not-allowed") { voice.failed = true; toast("Microphone blocked. Allow it for RAM AI (Edge asks once) and in Windows Settings > Privacy > Microphone.", 9000); }
    else if (e.error === "network") { voice.failed = "network"; }
    else if (e.error !== "no-speech" && e.error !== "aborted") toast("Voice input: " + e.error, 4000);
  };
  r.onend = () => {
    if (voice.rec !== r) return;
    if (voice.held && !voice.failed) {          // Edge ends a session after a pause or ~1 min: keep listening while the button is held
      if (voice.interim) { voice.finalText += voice.interim + " "; voice.interim = ""; }
      try { voice.rec = makeRec(); voice.rec.start(); return; } catch { }
    }
    voice.rec = null; finishVoice();
  };
  return r;
}
function startHold(via) {
  if (voice.held || voice.rec) return;
  Object.assign(voice, { held: true, via, base: $("#instr").value.trim(), finalText: "", interim: "", failed: false, t0: Date.now() });
  micUI(true);
  if (!(window.SpeechRecognition || window.webkitSpeechRecognition)) { voice.held = false; return voiceServer(); }
  try { voice.rec = makeRec(); voice.rec.start(); } catch { voice.held = false; voice.rec = null; voiceServer(); }
}
function endHold() {
  if (!voice.held) return;
  voice.held = false;
  setTimeout(() => { try { voice.rec ? voice.rec.stop() : finishVoice(); } catch { finishVoice(); } }, 400);  // don't cut off the last word
}
function finishVoice() {
  micUI(false);
  if (voice.interim) { voice.finalText += voice.interim; voice.interim = ""; }
  showVoiceText();
  if (voice.failed === "network") { voice.failed = false; return voiceServer(); }
  if (!voice.finalText.trim()) {
    toast(Date.now() - voice.t0 < 800 ? "Hold the 🎤 button (or Ctrl+M) while you speak, and let go to send." : "I didn't catch that - hold the button and speak a little closer to the microphone.", 5000);
    return;
  }
  if ($("#instr").value.trim()) $("#form").requestSubmit();
}
const micBtn = $("#mic");
micBtn.addEventListener("pointerdown", e => { if (e.button !== 0) return; e.preventDefault(); try { micBtn.setPointerCapture(e.pointerId); } catch { } startHold("pointer"); });
for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) micBtn.addEventListener(ev, () => { if (voice.via === "pointer") endHold(); });
micBtn.addEventListener("contextmenu", e => e.preventDefault());
document.addEventListener("keydown", e => { if (e.ctrlKey && !e.altKey && (e.key === "m" || e.key === "M")) { e.preventDefault(); if (!e.repeat) startHold("key"); } });
document.addEventListener("keyup", e => { if (voice.via === "key" && voice.held && (e.key === "m" || e.key === "M" || e.key === "Control")) endHold(); });
window.addEventListener("blur", () => endHold());
$("#instr").addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#form").requestSubmit(); } });
document.querySelectorAll(".hint-row").forEach(h => h.onclick = () => { $("#instr").value = h.dataset.q; $("#instr").focus(); });
$("#stop").onclick = async () => {
  try {
    await api("/stop", "POST");
    for (const t of tasks.values()) if (t.remote && !["Completed", "Failed", "Cancelled"].includes(t.state)) await api(`/peers/${t.remote.id}/rpc`, "POST", { method: "cancel_task", params: { id: t.id } });
  } catch { }
  toast("Stopped.");
};

const activeTasks = () => [...tasks.values()].filter(t => !["Completed", "Failed", "Cancelled"].includes(t.state));

async function pollTasks() {
  for (const t of activeTasks()) {
    try {
      if (t.remote) {
        const st = await api(`/peers/${t.remote.id}/rpc`, "POST", { method: "state" });
        const r = st.tasks.find(x => x.id === t.id); if (r) Object.assign(t, { state: r.state, result: r.result, error: r.error });
        renderApprovals(st.approvals, t.remote);
      } else {
        const r = await api(`/tasks/${t.id}/events?after=${t.lastId}`);
        if (r.events.length) { t.events.push(...r.events); t.lastId = r.events[r.events.length - 1].id; onNewEvents(r.events); }
        Object.assign(t, { state: r.state, result: r.result, error: r.error, thinking_since: r.thinking_since, skew: r.now - Date.now() / 1000 });
      }
      renderTask(t);
      if (!activeTasks().length) setCaption("");
    } catch { }
  }
}
async function pollApprovals() { if (remoteSel()) return; try { renderApprovals(await api("/approvals"), null); } catch { } }

// previous conversation (last few tasks) so the panel isn't empty after a restart
async function loadRecent() {
  try {
    const list = (await api("/tasks")).slice(0, 4).reverse();
    for (const r of list) {
      if (tasks.has(r.id)) continue;
      const ev = await api(`/tasks/${r.id}/events`);
      const t = { id: r.id, instruction: r.instruction, device: r.device, events: ev.events, lastId: ev.events.length ? ev.events[ev.events.length - 1].id : 0,
        state: ev.state, result: ev.result, error: ev.error, created: r.created, open: false };
      tasks.set(t.id, t); renderTask(t);
    }
  } catch { }
}

// ================================================================ LEFT PANE: live screen
const winlog = [];
let screenSize = null, lastCaption = "";
function setCaption(t) {
  const c = $("#action-cap");
  if (t === lastCaption) return; lastCaption = t;
  c.textContent = t ? "● " + t : ""; c.classList.toggle("hidden", !t);
}
function mapPoint(x, y) {
  const img = $("#screen"), wrap = $("#screen-wrap");
  if (!screenSize || !img.naturalWidth) return null;
  const ir = img.getBoundingClientRect(), wr = wrap.getBoundingClientRect();
  return { left: ir.left - wr.left + x * ir.width / screenSize.w, top: ir.top - wr.top + y * ir.height / screenSize.h };
}
function ripple(x, y) {
  const p = mapPoint(x, y); if (!p) return;
  const r = document.createElement("div"); r.className = "ripple"; r.style.left = p.left + "px"; r.style.top = p.top + "px";
  $("#ripples").appendChild(r); setTimeout(() => r.remove(), 1000);
}
function onNewEvents(evs) {
  for (const e of evs) {
    if (e.event !== "tool_call") continue;
    const a = parse(e.data).args || {};
    if ((e.tool === "click" || e.tool === "mouse_move") && a.x != null) ripple(a.x, a.y);
  }
}
function renderWinlog() {
  $("#winlog").innerHTML = winlog.length ? '<span class="lab">Windows:</span>' + winlog.map(w => `<span class="w" title="${esc(w.t)}"><b>${esc(short(w.t, 34))}</b> · ${w.at}</span>`).join("") : '<span class="lab">Window changes will appear here</span>';
}
let lastUrl = null;
async function screenLoop() {
  for (;;) {
    const on = $("#live-on").checked && !$("#view-main").classList.contains("hidden") && !document.hidden;
    const busy = activeTasks().length > 0;
    if (on) {
      try {
        const rd = remoteSel();
        let blobUrl = null, w = null, h = null, cx = null, cy = null, title = "", ctl = false;
        if (rd) {
          const r = await api(`/peers/${rd.id}/rpc`, "POST", { method: "screenshot" });
          blobUrl = "data:image/png;base64," + r.png_b64; w = r.width; h = r.height; title = rd.name;
        } else {
          const r = await api("/screenshot?view=" + $("#view-mode").value, "GET", undefined, true);
          if (r.status === 403) { $("#screen-empty").textContent = "🔒 A private window is in front — not shown"; $("#screen").classList.add("hidden"); }
          else if (r.ok) {
            blobUrl = URL.createObjectURL(await r.blob());
            w = +r.headers.get("X-Screen-W"); h = +r.headers.get("X-Screen-H");
            cx = +r.headers.get("X-Cursor-X"); cy = +r.headers.get("X-Cursor-Y");
            title = decodeURIComponent(r.headers.get("X-Window") || ""); ctl = r.headers.get("X-Control") === "1";
          }
        }
        if (blobUrl) {
          const img = $("#screen"); img.classList.remove("hidden"); $("#screen-empty").textContent = "";
          await new Promise(res => { img.onload = res; img.onerror = res; img.src = blobUrl; });
          if (lastUrl && lastUrl.startsWith("blob:")) URL.revokeObjectURL(lastUrl); lastUrl = blobUrl;
          screenSize = { w, h };
          const cur = $("#cursor");
          if (cx != null && cx >= 0 && cy >= 0 && cx <= w && cy <= h) { const p = mapPoint(cx, cy); if (p) { cur.style.left = p.left + "px"; cur.style.top = p.top + "px"; cur.classList.remove("hidden"); } }
          else cur.classList.add("hidden");
          $("#win-chip").textContent = title || "—"; $("#win-chip").title = title;
          if (title && (!winlog.length || winlog[winlog.length - 1].t !== title)) {
            winlog.push({ t: title, at: new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) });
            if (winlog.length > 8) winlog.shift(); renderWinlog();
          }
          img.classList.toggle("ctl", ctl);
          $("#live-dot").className = "dot " + (ctl ? "ctl" : busy ? "work" : "idle");
          $("#live-status").textContent = ctl ? "Controlling mouse & keyboard" : busy ? "Agent is working" : (rd ? "Watching " + rd.name : "Idle");
        }
      } catch (e) { $("#screen-empty").textContent = remoteSel() ? "Remote screen not available (enable 'allow remote screenshots' on that PC)" : "Live view unavailable"; }
    } else {
      $("#live-dot").className = "dot " + (busy ? "work" : "idle");
      $("#live-status").textContent = busy ? "Agent is working (live view off)" : "Live view off";
    }
    await sleep(busy ? +$("#fps").value : 1500);
  }
}
window.addEventListener("resize", () => { $("#cursor").classList.add("hidden"); });
renderWinlog();

// ---------------------------------------------------------------- history
async function loadHistory() {
  const ts = await api("/tasks");
  $("#tasks").innerHTML = ts.map(t => `<div class="t" data-id="${esc(t.id)}"><b>${esc(t.state)}</b> · ${esc(t.device)} · ${esc(t.instruction.slice(0, 110))}</div>`).join("") || '<p class="muted">No tasks yet.</p>';
  document.querySelectorAll("#tasks .t").forEach(el => el.onclick = async () => {
    const a = await api("/audit?limit=300&task_id=" + el.dataset.id);
    $("#audit").textContent = a.reverse().map(x => `${new Date(x.ts * 1000).toLocaleTimeString()}  ${x.event}  ${x.tool || ""}  ${x.data || ""}`).join("\n");
  });
  const a = await api("/audit?limit=100");
  $("#audit").textContent = a.map(x => `${new Date(x.ts * 1000).toLocaleTimeString()}  ${x.event}  ${x.tool || ""}  ${x.data || ""}`).join("\n");
}
$("#clearhist").onclick = async () => { if (confirm("Delete all task history and audit logs on this PC?")) { await api("/history", "DELETE"); loadHistory(); } };
$("#export").onclick = async () => { const d = await api("/export"); const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(d, null, 2)], { type: "application/json" })); a.download = "ramai-export.json"; a.click(); };

// ---------------------------------------------------------------- settings
async function renderSettings() {
  await loadStatus(); const s = S.settings, el = $("#settings");
  el.innerHTML = `
  <section><h3>AI model (reasoning)</h3>
    <p class="muted">Pick which AI plans and runs your tasks. You can switch any time here or from the header. Keys are stored encrypted on this PC (Windows DPAPI).</p>
    ${s.llm_providers.map(p => `<div class="prov ${p.active ? "on" : ""}" data-p="${esc(p.id)}">
      <div class="row"><b>${esc(p.label)}</b>${p.active ? '<span class="pill on">in use</span>' : ""}<span class="muted">${p.ready ? "ready" : (p.needs_key && !p.key_set ? "needs API key" : "not set up")}</span></div>
      ${p.id === "custom" ? `<div class="row"><input class="p-label" placeholder="Name shown in the app" value="${esc(s.custom_label)}"></div>` : ""}
      <div class="row"><input class="p-url" placeholder="Base URL (OpenAI-compatible)" value="${esc(p.base_url)}"><input class="p-model" list="ml-${esc(p.id)}" placeholder="Model name, e.g. ${p.id === "openai" ? "gpt-6-luna" : p.id === "deepseek" ? "deepseek-chat" : p.id === "sarvam" ? "deepseekv4.1-flash-1017" : "model"}" value="${esc(p.model)}"><datalist id="ml-${esc(p.id)}"></datalist></div>
      <div class="row"><input class="p-key" type="password" placeholder="${p.key_set ? "•••••• key saved (type a new one to replace)" : (p.needs_key ? "API key" : "API key (optional)")}">
        <button class="p-save">Save</button><button class="ghost p-test">Test &amp; list models</button><button class="ghost p-use">Use this</button>${p.key_set ? '<button class="ghost p-del">Delete key</button>' : ""}</div>
      <div class="msg p-out"></div></div>`).join("")}
    <p class="muted">Tokens used this session: ${S.usage.prompt_tokens} in / ${S.usage.completion_tokens} out (${S.usage.calls} calls). The model must support tool/function calling.</p></section>
  <section><h3>Vision provider (separate from DeepSeek)</h3>
    <p class="muted">When enabled, screenshots taken by describe_screen are sent to the provider below and may leave this PC. Windows matching the exclusion list are never sent.</p>
    <label class="inline"><input type="checkbox" id="v-en" ${s.vision.enabled ? "checked" : ""}> Enable</label>
    <label class="inline"><input type="checkbox" id="v-co" ${s.vision.consent ? "checked" : ""}> I understand screen content is sent to this provider</label>
    <div class="row"><input id="v-url" placeholder="Base URL (OpenAI-compatible), e.g. https://…/v1" value="${esc(s.vision.base_url)}"><input id="v-model" placeholder="Vision model name" value="${esc(s.vision.model)}"></div>
    <div class="row"><input id="v-key" type="password" placeholder="${s.secrets_set.vision ? "•••••• saved" : "Vision API key"}"><button id="v-save">Save</button><button class="ghost" id="v-test">Test</button></div>
    <div class="row"><label class="inline"><input type="checkbox" id="v-pause" ${S.screenshot_sharing ? "" : "checked"}> Pause screenshot sharing</label></div>
    <label>Excluded apps / window-title words (never captured or controlled), one per line<textarea id="s-ex" rows="3">${esc(s.excluded_apps.join("\n"))}</textarea></label>
    <button id="v-ex">Save exclusions</button><div id="v-out" class="msg"></div></section>
  <section><h3>Capabilities</h3>
    <div class="row"><span>Web search &amp; reading web pages</span><label class="inline"><input type="checkbox" id="c-web" ${s.web_access !== false ? "checked" : ""}> enabled</label></div>
    <div class="row"><span>Read my Desktop, Documents, Downloads and OneDrive (read-only)</span><button id="c-quick">Grant read access</button></div>
    <p class="muted">Sending, deleting and overwriting always ask first — unless you say “authorization granted” for a task. Memory is saved automatically and listed below.</p></section>
  <section><h3>Folders RAM AI may access</h3>
    <p class="muted">Read-only unless write is ticked. System folders, browser profiles and credential stores are always blocked. Output folder (always writable): ${esc(s.output_dir)}</p>
    <div id="roots">${s.roots.map(r => `<div class="row"><span>${esc(r.path)} — ${r.write ? "read/write" : "read-only"}</span><button class="ghost" data-rm="${esc(r.path)}">Remove</button></div>`).join("")}</div>
    <div class="row"><input id="r-path" placeholder="C:\\Users\\you\\Documents\\Quotes"><button class="ghost" id="r-pick">Browse…</button><label class="inline"><input type="checkbox" id="r-w"> write</label><button id="r-add">Add</button></div></section>
  <section><h3>Outlook (Microsoft Graph)</h3>
    <p class="muted">Needs your own Entra app registration (see docs/GUIDES.md (section 3)). Status: ${esc(S.outlook.account || S.outlook.status)}${S.outlook.error ? " — " + esc(S.outlook.error) : ""}</p>
    <div class="row"><input id="o-id" placeholder="Application (client) ID" value="${esc(s.ms_client_id)}"><input id="o-tenant" placeholder="Tenant (common / your tenant id)" value="${esc(s.ms_tenant)}"><button id="o-save">Save</button></div>
    <div class="row"><button id="o-in">Sign in with Microsoft</button><button class="ghost" id="o-out">Sign out</button></div></section>
  <section><h3>WhatsApp Web (experimental)</h3>
    <p class="muted">Opens WhatsApp Web in a RAM AI-owned Edge profile. You scan the QR code yourself. Unofficial automation can break or breach WhatsApp terms; messages are only sent after your approval.</p>
    <button id="w-open">Open WhatsApp Web</button><span id="w-st" class="muted"></span></section>
  <section><h3>Two-PC connection (over Tailscale / private VPN)</h3>
    <p class="muted">Paired: ${s.peers.map(p => esc(p.name) + ` <a href="#" data-unpair="${esc(p.id)}">revoke</a>`).join(", ") || "none"}. Listening: ${esc(s.remote.enabled ? s.remote.bind + ":" + s.remote.port : "off")}</p>
    <div class="row"><input id="p-bind" placeholder="This PC's Tailscale IP (100.x.y.z)" value="${esc(s.remote.bind)}"><input id="p-port" value="${esc(s.remote.port)}"><label class="inline"><input type="checkbox" id="p-ss" ${s.remote.allow_screenshot ? "checked" : ""}> allow remote screenshots</label></div>
    <div class="row"><button id="p-on">Start listening</button><button class="ghost" id="p-off">Stop</button><button id="p-code">Generate pairing code</button></div>
    <div class="row"><input id="p-url" placeholder="Other PC's URL, e.g. https://100.64.0.2:8766"><input id="p-c" placeholder="Pairing code"><button id="p-join">Pair</button></div>
    <div id="p-out" class="msg"></div></section>
  <section><h3>Memory (user-approved preferences)</h3><div id="mem"></div></section>
  <section><h3>General</h3><div class="row"><input id="g-user" value="${esc(s.user_name || "")}" placeholder="Your name"><button id="g-usave">Save my name</button></div>
    <div class="row"><input id="g-name" value="${esc(s.device_name)}"><button id="g-save">Rename this PC</button></div>
    <div class="row"><label class="muted" style="flex:0 0 auto">🎤 Voice language</label><select id="g-vlang">${[["en-IN", "English (India)"], ["en-US", "English (US)"], ["en-GB", "English (UK)"], ["hi-IN", "Hindi"], ["te-IN", "Telugu"], ["ta-IN", "Tamil"], ["kn-IN", "Kannada"], ["ml-IN", "Malayalam"], ["mr-IN", "Marathi"], ["bn-IN", "Bengali"], ["gu-IN", "Gujarati"]].map(([v, n]) => `<option value="${v}" ${v === (s.voice_lang || "en-IN") ? "selected" : ""}>${n}</option>`).join("")}</select>
      <span class="muted small" style="flex:0 0 auto">Hold 🎤 (or Ctrl+M) while you speak, let go to send</span></div>
    <p class="muted">Version ${esc(S.version)} · Emergency stop: Ctrl+Alt+Q · Take control: Ctrl+Alt+P · Voice: hold Ctrl+M · Retention: ${esc(s.retention_days)} days</p></section>`;
  const out = (id, ok, m) => { const o = $(id); o.className = "msg " + (ok ? "ok" : "err"); o.textContent = m; };
  const guard = fn => async () => { try { await fn(); } catch (e) { toast(e.message, 5000); } };
  const secretOf = { deepseek: "deepseek", openai: "openai", sarvam: "sarvam", local: "local_llm", custom: "custom_llm" };
  const fieldOf = { deepseek: ["deepseek_base_url", "deepseek_model"], openai: ["openai_base_url", "openai_model"], sarvam: ["sarvam_base_url", "sarvam_model"], local: ["local_base_url", "local_model"], custom: ["custom_base_url", "custom_model"] };
  el.querySelectorAll(".prov").forEach(card => {
    const pid = card.dataset.p, q = c => card.querySelector(c), msg = (ok, t) => { const o = q(".p-out"); o.className = "msg p-out " + (ok ? "ok" : "err"); o.textContent = t; };
    const save = async () => {
      const [u, m] = fieldOf[pid]; const body = { [u]: q(".p-url").value.trim(), [m]: q(".p-model").value.trim() };
      if (q(".p-label")) body.custom_label = q(".p-label").value.trim() || "Other provider";
      await api("/settings", "POST", body);
      if (q(".p-key").value.trim()) { await api("/secrets/" + secretOf[pid], "POST", { value: q(".p-key").value.trim() }); q(".p-key").value = ""; }
    };
    q(".p-save").onclick = guard(async () => { await save(); toast("Saved"); renderSettings(); loadModelSwitch(); });
    q(".p-test").onclick = guard(async () => {
      await save(); msg(true, "Testing…");
      const r = await api("/llm/test", "POST", { provider: pid });
      if (!r.ok) { msg(false, r.error); return; }
      q("datalist").innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join("");
      if (!q(".p-model").value && r.models.length) q(".p-model").value = r.models[0];
      msg(true, `Connected. ${r.models.length} model(s) available${r.hint ? " — " + r.hint : ""}. Pick one in the model box, then Save / Use this.`);
    });
    q(".p-use").onclick = guard(async () => { await save(); const r = await api("/llm/select", "POST", { provider: pid, model: q(".p-model").value.trim() }); toast(r.ready ? "Now using " + pid + " · " + r.model : "Selected, but not ready yet (key/model missing)"); renderSettings(); loadModelSwitch(); });
    if (q(".p-del")) q(".p-del").onclick = guard(async () => { await api("/secrets/" + secretOf[pid], "DELETE"); renderSettings(); loadModelSwitch(); });
  });
  $("#v-save").onclick = guard(async () => {
    await api("/settings", "POST", { vision: { enabled: $("#v-en").checked, consent: $("#v-co").checked, base_url: $("#v-url").value, model: $("#v-model").value } });
    if ($("#v-key").value) await api("/secrets/vision", "POST", { value: $("#v-key").value }); toast("Saved"); renderSettings();
  });
  $("#v-test").onclick = guard(async () => { const r = await api("/vision/test", "POST"); out("#v-out", r.ok, r.ok ? "Vision OK: " + r.reply : r.error); });
  $("#v-pause").onchange = guard(async () => api("/screenshot_sharing", "POST", { enabled: !$("#v-pause").checked }));
  $("#v-ex").onclick = guard(async () => { await api("/settings", "POST", { excluded_apps: $("#s-ex").value.split("\n").map(x => x.trim()).filter(Boolean) }); toast("Saved"); });
  $("#c-web").onchange = guard(async () => { await api("/settings", "POST", { web_access: $("#c-web").checked }); toast("Saved"); });
  $("#c-quick").onclick = guard(async () => { const r = await api("/roots/quick", "POST", { write: false }); toast(r.added.length ? "Read access granted: " + r.added.length + " folders" : "Already granted"); renderSettings(); });
  $("#r-pick").onclick = guard(async () => { const r = await api("/roots/pick", "POST"); if (r.path) $("#r-path").value = r.path.replace(/\//g, "\\"); });
  $("#r-add").onclick = guard(async () => { await api("/roots", "POST", { path: $("#r-path").value, write: $("#r-w").checked }); renderSettings(); });
  el.querySelectorAll("[data-rm]").forEach(b => b.onclick = guard(async () => { await api("/roots", "DELETE", { path: b.dataset.rm }); renderSettings(); }));
  $("#o-save").onclick = guard(async () => { await api("/settings", "POST", { ms_client_id: $("#o-id").value.trim(), ms_tenant: $("#o-tenant").value.trim() || "common" }); toast("Saved"); });
  $("#o-in").onclick = guard(async () => { await api("/outlook/login", "POST"); toast("Complete sign-in in the browser window that opened."); });
  $("#o-out").onclick = guard(async () => { await api("/outlook/logout", "POST"); renderSettings(); });
  $("#w-open").onclick = guard(async () => { $("#w-st").textContent = " opening…"; const r = await api("/whatsapp/open", "POST"); $("#w-st").textContent = " " + r.status + (r.error ? ": " + r.error : ""); });
  $("#p-on").onclick = guard(async () => { const r = await api("/remote/enable", "POST", { bind: $("#p-bind").value, port: +$("#p-port").value, allow_screenshot: $("#p-ss").checked }); out("#p-out", true, "Listening on " + r.listening + " (TLS fingerprint " + r.fingerprint.slice(0, 16) + "…)"); });
  $("#p-off").onclick = guard(async () => { await api("/remote/disable", "POST"); renderSettings(); });
  $("#p-code").onclick = guard(async () => { const r = await api("/pairing/start", "POST"); out("#p-out", true, "Pairing code (valid 10 min, single use): " + r.code + " — enter it on the other PC together with this PC's URL " + (r.url || "(start listening first)")); });
  $("#p-join").onclick = guard(async () => { out("#p-out", true, "Waiting for the other PC's user to approve…"); const r = await api("/pairing/join", "POST", { url: $("#p-url").value, code: $("#p-c").value }); out("#p-out", true, "Paired with " + r.paired); loadDevices(); });
  el.querySelectorAll("[data-unpair]").forEach(a => a.onclick = guard(async e => { e?.preventDefault?.(); await api("/peers/" + a.dataset.unpair, "DELETE"); renderSettings(); loadDevices(); }));
  $("#g-usave").onclick = guard(async () => { await api("/settings", "POST", { user_name: $("#g-user").value.trim() }); toast("Saved"); });
  $("#g-save").onclick = guard(async () => { await api("/settings", "POST", { device_name: $("#g-name").value }); renderSettings(); });
  $("#g-vlang").onchange = guard(async () => { await api("/settings", "POST", { voice_lang: $("#g-vlang").value }); S.settings.voice_lang = $("#g-vlang").value; toast("Voice language saved"); });
  const mem = await api("/memory");
  $("#mem").innerHTML = mem.map(m => `<div class="row"><span>${esc(m.key)} = ${esc(m.value)}</span><button class="ghost" data-mk="${esc(m.key)}">Delete</button></div>`).join("") || '<p class="muted">Empty.</p>';
  el.querySelectorAll("[data-mk]").forEach(b => b.onclick = guard(async () => { await api("/memory/" + encodeURIComponent(b.dataset.mk), "DELETE"); renderSettings(); }));
}


async function boot() { await loadStatus(); await loadDevices(); loadModelSwitch(); await loadRecent(); }
boot().then(() => { screenLoop(); });
(function loop() { Promise.all([pollTasks(), pollApprovals()]).finally(() => setTimeout(loop, activeTasks().length ? 450 : 1500)); })();
setInterval(loadDevices, 15000); setInterval(loadStatus, 10000);
setInterval(() => { for (const t of activeTasks()) renderTask(t); }, 1000);
