"use strict";

// ---------------------------------------------------------------- helpers
const $ = (id) => document.getElementById(id);
const store = {
  get: (k, d) => { try { return JSON.parse(localStorage.getItem("n50." + k)) ?? d; } catch { return d; } },
  set: (k, v) => { try { localStorage.setItem("n50." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
  return el;
}

async function api(path) {
  const r = await fetch(path, { headers: { Accept: "application/json" } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

async function post(path, data) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-N50": "1" },
    body: JSON.stringify(data || {}),
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

const fmt = new Intl.NumberFormat("en");
const dateFmt = new Intl.DateTimeFormat("en", { month: "short", day: "numeric" });
const timeFmt = new Intl.DateTimeFormat("en", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

function ago(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.round(s / 86400)} days ago`;
  return dateFmt.format(ts * 1000);
}

function clock(sec) {
  sec = Math.max(0, Math.round(sec));
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

let toastTimer;
function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isErr ? 7000 : 3500);
}

// ---------------------------------------------------------------- state
const S = {
  ov: null,
  engine: store.get("engine", null),
  run: { active: false },
  lines: [],
  replay: null,            // run id being viewed, or null for live
  milestone: "",
  ledgerShown: 60,
  openSet: null,
};
const engineById = (id) => (S.ov?.engines || []).find((e) => e.id === id);

// ---------------------------------------------------------------- overview
async function loadOverview() {
  try {
    S.ov = await api("/api/overview");
  } catch (e) {
    $("panel-note").textContent = `Couldn't load the series: ${e.message}. Reload to try again.`;
    return;
  }
  if (!engineById(S.engine)) S.engine = S.ov.default;
  renderHeader();
  renderEngines();
  if (!S.run.active) renderPanelIdle();
  renderLedger();
  renderRuns(S.ov.runs);
  syncButtons();
}

function renderHeader() {
  const s = S.ov.series;
  const tag = $("tagline");
  tag.replaceChildren(
    `${fmt.format(s.completed)} sets and ${fmt.format(s.used)} repos published to `,
    s.parentUrl ? h("a", { href: s.parentUrl, target: "_blank", rel: "noopener", text: s.parent || "Notion" }) : (s.parent || "Notion"),
    ", fifty at a time.",
  );
  $("next-num").textContent = S.ov.next;
}

function engineRow(e) {
  const st = S.ov.stats[e.id] || {};
  const spark = h("span", { class: "spark", "aria-hidden": "true" },
    (st.recent || []).map((r) => {
      const f = Math.min(1, r.count / r.size);
      return h("i", { class: f >= 1 ? "full" : "short", style: `height:${Math.max(12, f * 100)}%`,
                      title: `Set ${r.n}: ${r.count} of ${r.size}` });
    }));
  let meta = "Not used yet";
  if (st.sets) {
    meta = `${st.sets} ${st.sets === 1 ? "set" : "sets"} published`;
    if (st.avgFill != null) meta += `, recent ones ${Math.round(st.avgFill * 100)}% full`;
    if (st.lastAt) meta += `, last ${ago(st.lastAt)}`;
  }
  const input = h("input", { type: "radio", name: "engine", value: e.id, checked: e.id === S.engine,
                             onchange: () => { S.engine = e.id; store.set("engine", e.id); syncButtons(); } });
  return h("label", { class: "engine" + (e.id === S.ov.default ? " default" : "") },
    input,
    h("span", { class: "engine-name" }, h("span", { class: "id", text: e.id }), e.name),
    spark,
    h("span", { class: "engine-meta", text: meta + (e.size !== 50 ? `. Sets of ${e.size}.` : "") }),
    h("span", { class: "engine-about", text: e.about }),
  );
}

function renderEngines() {
  const main = S.ov.engines.filter((e) => e.group === "main");
  const older = S.ov.engines.filter((e) => e.group !== "main");
  $("engines-main").replaceChildren(...main.map(engineRow));
  $("engines-older").replaceChildren(...older.map(engineRow));
  if (older.some((e) => e.id === S.engine)) $("older").open = true;
}

// ---------------------------------------------------------------- panel
function buildPorts() {
  const panel = $("panel");
  if (panel.children.length !== 50) {
    panel.replaceChildren(...Array.from({ length: 50 }, (_, i) =>
      h("span", { class: "port", style: `animation-delay:${(i % 25) * 0.06 + Math.floor(i / 25) * 0.03}s` })));
  }
  return [...panel.children];
}

function setPorts(count, size, animate) {
  const ports = buildPorts();
  const lit = Math.round(Math.min(1, count / size) * 50);
  const full = count >= size;
  $("panel").classList.remove("scan");
  $("panel").setAttribute("aria-label", `${count} of ${size} picks`);
  ports.forEach((p, i) => {
    p.style.transitionDelay = animate ? `${i * 16}ms` : "0ms";
    p.classList.toggle("on", i < lit && full);
    p.classList.toggle("part", i < lit && !full);
  });
}

function renderPanelIdle() {
  const pv = S.ov.preview;
  const note = $("panel-note");
  if (pv) {
    const eng = engineById(pv.engine);
    const size = eng?.size || 50;
    setPorts(pv.count, size, false);
    note.replaceChildren(
      `Last dry run, ${pv.engine || "engine"} ${ago(pv.at)}: `,
      h("strong", { text: `${pv.count} of ${size}` }), " found.",
      pv.count < size && pv.publishable === false ? " Too few to publish a full set." : "",
    );
  } else {
    setPorts(0, 50, false);
    const last = S.ov.sets.find((s) => !s.special);
    note.textContent = last
      ? `No dry run yet for Set ${S.ov.next}. Set ${last.n} shipped ${last.count} of ${last.size}${last.engine ? ` with ${last.engine}` : ""}.`
      : `No dry run yet for Set ${S.ov.next}.`;
  }
}

function renderPanelRunning() {
  buildPorts().forEach((p) => p.classList.remove("on", "part"));
  $("panel").classList.add("scan");
  $("panel").setAttribute("aria-label", "Engine running");
  $("panel-note").textContent = `${S.run.engine} ${S.run.mode === "publish" ? "is publishing" : "dry run in progress"}…`;
}

// ---------------------------------------------------------------- run controls
function syncButtons() {
  const active = S.run.active;
  const eng = engineById(S.engine);
  $("btn-dry").disabled = active || !eng;
  $("btn-pub").disabled = active || !eng;
  $("btn-stop").hidden = !(active && S.run.mode === "dry");
  document.querySelectorAll('input[name="engine"]').forEach((i) => (i.disabled = active));
  if (!countdown && eng) $("btn-pub").querySelector(".btn-text").textContent = `Publish Set ${S.ov?.next ?? ""}`;
  $("hint").replaceChildren(...(active
    ? [S.run.mode === "dry" ? "Stop ends the dry run; nothing is written either way." : "Publishing writes the page to Notion and updates the tracker."]
    : ["Dry runs never write to Notion. Press ", h("kbd", { text: "D" }), " to start one."]));
}

async function startRun(mode) {
  if (S.run.active) return;
  try {
    await post("/api/run", { engine: S.engine, mode });
    if (S.replay) exitReplay();
    showTab("console");
  } catch (e) {
    toast(e.message, true);
  }
}

let countdown = null;
function publishClick() {
  const btn = $("btn-pub");
  if (countdown) return cancelCountdown();
  let left = 3;
  const label = btn.querySelector(".btn-text");
  label.textContent = `Publishing in ${left}… click to cancel`;
  btn.classList.add("counting");
  countdown = setInterval(() => {
    left -= 1;
    if (left > 0) { label.textContent = `Publishing in ${left}… click to cancel`; return; }
    cancelCountdown(true);
    startRun("publish");
  }, 1000);
}
function cancelCountdown(silent) {
  clearInterval(countdown);
  countdown = null;
  $("btn-pub").classList.remove("counting");
  syncButtons();
  if (!silent) toast("Publish cancelled.");
}

// ---------------------------------------------------------------- console
const MILESTONES = [
  [/^\[sync\] latest in Notion=(\d+)/, (m) => `Synced with Notion, latest set ${m[1]}`],
  [/next: (Set \d+)/, (m) => `Preparing ${m[1]}`],
  [/sliced discovery: (\d+) slices/, (m) => { progress(0); return `Searching GitHub across ${m[1]} slices`; }],
  [/(\d+)\/(\d+) slices/, (m) => { progress(m[1] / m[2]); return `Searching GitHub, ${m[1]} of ${m[2]} slices`; }],
  [/querying Trendshift|trendshift discovery/i, () => "Reading Trendshift trending feeds"],
  [/cheap gate: \d+ of (\d+)/, (m) => { progress(1); return `Screening ${m[1]} unused candidates`; }],
  [/gate: (\d+) pass of (\d+)/, (m) => `${m[1]} of ${m[2]} candidates passed the quality gate`],
  [/selected (\d+)\/(\d+)/, (m) => `Selected ${m[1]} of ${m[2]}`],
  [/would publish: Set (\d+).*\((\d+) repos\)/, (m) => `Dry run ready: ${m[2]} repos for Set ${m[1]}`],
  [/published (Set \d+).*\((\d+) repos\)/i, (m) => `Published ${m[1]} with ${m[2]} repos`],
];

function progress(f) {
  $("progress").hidden = false;
  $("progress-bar").style.width = `${Math.round(Math.min(1, f) * 100)}%`;
}

function classify(line) {
  if (line.startsWith("$ ")) return "l-cmd";
  if (/traceback|\berror:|exception|refus|\bfailed\b|not publishing/i.test(line) && !/errors 0/.test(line)) return "l-err";
  if (/✅|ALL PASSED|would publish|\bpublished\b/i.test(line)) return "l-ok";
  if (/\bwarn|rate-limit|\bskipp?ed\b|unavailable/i.test(line)) return "l-warn";
  if (/^\s{6,}\S/.test(line)) return "l-dim";
  return "";
}

let pending = [];
let flushQueued = false;
function addLines(lines) {
  pending.push(...lines);
  if (!flushQueued) { flushQueued = true; requestAnimationFrame(flushLines); }
}
function flushLines() {
  flushQueued = false;
  const log = $("log");
  const q = $("log-filter").value.trim().toLowerCase();
  const frag = document.createDocumentFragment();
  for (const line of pending) {
    S.lines.push(line);
    const span = h("span", { class: classify(line) || null, text: line + "\n" });
    if (q && !line.toLowerCase().includes(q)) span.classList.add("x");
    frag.append(span);
    if (!S.replay) for (const [re, fn] of MILESTONES) { const m = line.match(re); if (m) { S.milestone = fn(m); break; } }
  }
  pending = [];
  log.append(frag);
  if ($("follow").checked) log.scrollTop = log.scrollHeight;
  renderStatus();
}
function clearLog() {
  S.lines = [];
  pending = [];
  $("log").replaceChildren();
}

function applyFilter() {
  const q = $("log-filter").value.trim().toLowerCase();
  for (const span of $("log").children) span.classList.toggle("x", !!q && !span.textContent.toLowerCase().includes(q));
}

function renderStatus() {
  if (S.replay) return;
  const r = S.run;
  const led = $("led");
  let text = "Idle";
  led.className = "led";
  if (r.active) {
    led.classList.add("run");
    text = S.milestone || `Running ${r.engine} ${r.mode === "publish" ? "publish" : "dry run"}`;
  } else if (r.id) {
    const lastErr = [...S.lines].reverse().find((l) => classify(l) === "l-err");
    if (r.stopped) { led.classList.add("err"); text = "Dry run stopped"; }
    else if (r.rc === 0) {
      led.classList.add("ok");
      text = r.mode === "publish" ? (S.milestone || "Published") : (r.result ? `Dry run finished: ${r.result.count} picks for Set ${r.result.set}` : "Dry run finished");
    } else {
      led.classList.add("err");
      text = `Exited with code ${r.rc}` + (lastErr ? `: ${lastErr.replace(/^\[v\d+\]\s*/, "").slice(0, 140)}` : "");
    }
  }
  $("status-text").textContent = text;
  $("status-text").title = text;
  if (!r.active) $("status-time").textContent = r.elapsed != null ? `took ${clock(r.elapsed)}${r.ended ? `, ${ago(r.ended)}` : ""}` : "";
}

let tick = null;
function startTicker() {
  stopTicker();
  tick = setInterval(() => {
    const el = Date.now() / 1000 - S.run.started;
    $("status-time").textContent = clock(el);
    document.title = `▶ ${clock(el)} ${S.run.engine} · n50`;
  }, 500);
}
function stopTicker() { clearInterval(tick); tick = null; }

// ---------------------------------------------------------------- live stream
let es = null;
function connect() {
  if (es) return;
  es = new EventSource("/api/stream");
  es.addEventListener("hello", (ev) => {
    const d = JSON.parse(ev.data);
    if (!S.replay) {
      clearLog();
      S.milestone = "";
      addLines(d.lines || []);
    }
    applyState(d.state, false);
  });
  es.addEventListener("line", (ev) => { if (!S.replay) addLines([JSON.parse(ev.data)]); });
  es.addEventListener("state", (ev) => {
    const st = JSON.parse(ev.data);
    if (st.active && !S.run.active && !S.replay) { clearLog(); S.milestone = ""; $("progress").hidden = true; }
    applyState(st, false);
  });
  es.addEventListener("done", (ev) => applyState(JSON.parse(ev.data), true));
  es.onerror = () => {
    if (es.readyState === EventSource.CLOSED) { es = null; setTimeout(connect, 3000); }
  };
}
function disconnect() { if (es) { es.close(); es = null; } }

function applyState(st, finished) {
  const wasActive = S.run.active;
  S.run = st || { active: false };
  if (S.run.active) {
    renderPanelRunning();
    startTicker();
  } else {
    stopTicker();
    $("progress").hidden = true;
    if (wasActive || finished) onFinished();
  }
  syncButtons();
  renderStatus();
}

async function onFinished() {
  const r = S.run;
  const res = r.result;
  const okRun = r.rc === 0;
  document.title = (okRun ? "✓ " : "✕ ") + (res ? `Set ${res.set} · ` : "") + "n50";
  await loadOverview();
  if (res && !S.replay) {
    setPorts(res.count, engineById(r.engine)?.size || 50, true);
    await loadPicks(res.set, res.kind);
    if (activeTab() === "console") showTab("picks");
  }
  if (store.get("notify", false) && document.hidden && "Notification" in window && Notification.permission === "granted") {
    new Notification(okRun ? "n50 run finished" : "n50 run ended with an error", {
      body: $("status-text").textContent, icon: "/static/favicon.svg",
    });
  }
}

// ---------------------------------------------------------------- picks
const CAT_COLORS = ["#5bd49b", "#f0a53a", "#7fb6f2", "#e58bb3", "#c9b45a", "#9a8cf0", "#5fc6c9", "#ef8a5e", "#a8c76b", "#c792ea", "#8f9cab"];
let picksFilter = null;

async function loadPicks(n, kind) {
  let d;
  try { d = await api(`/api/set?n=${n}`); } catch (e) { toast(e.message, true); return; }
  picksFilter = null;
  renderPicks(d, kind);
}

function catColor(cats) {
  const m = new Map();
  cats.forEach((c, i) => m.set(c, CAT_COLORS[Math.min(i, CAT_COLORS.length - 1)]));
  return m;
}

function pickItem(p, color) {
  return h("li", { class: "pick" },
    h("span", { class: "pick-rank", text: p.rank }),
    h("span", { class: "pick-name" }, h("a", { href: p.url, target: "_blank", rel: "noopener", text: p.repo })),
    h("span", { class: "pick-num", text: [p.stars != null ? `★ ${fmt.format(p.stars)}` : "", p.score != null ? `score ${Math.round(p.score)}` : ""].filter(Boolean).join("   ") }),
    p.description ? h("span", { class: "pick-desc", text: p.description }) : null,
    h("span", { class: "pick-tags" },
      h("span", { class: "cat", style: color ? `color:${color}` : null, text: p.category || "Uncategorised" }),
      p.compose ? h("span", { text: "compose" }) : p.docker ? h("span", { text: "Dockerfile" }) : null,
      p.language ? h("span", { text: p.language }) : null),
  );
}

function renderPicks(d, kind) {
  const root = $("picks");
  const picks = d.picks || [];
  const published = !!d.set;
  const size = d.set?.size || engineById(d.audit?.engine)?.size || 50;
  $("picks-count").textContent = picks.length ? String(picks.length) : "";
  if (!picks.length) {
    root.replaceChildren(h("p", { class: "empty", text: "No picks to show yet. Run a dry run and its picks will appear here before you publish." }));
    return;
  }
  const counts = new Map();
  picks.forEach((p) => counts.set(p.category || "Uncategorised", (counts.get(p.category || "Uncategorised") || 0) + 1));
  const cats = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  const colors = catColor(cats.map((c) => c[0]));
  const engine = d.set?.engine || d.audit?.engine;
  const when = d.set?.at || d.audit?.mtime;

  let verdict = null;
  if (!published) {
    const full = picks.length >= size;
    verdict = h("p", { class: "verdict " + (full ? "good" : "bad") },
      full ? `A full set of ${size}. Publishing runs the engine again, so the final list can differ slightly.`
           : `${picks.length} of ${size} found. Every engine publishes only a full set, so a publish now would stop without writing. Try another engine, or run again later as new repos appear.`);
  }

  const list = h("ol", { class: "picklist" });
  const fill = () => list.replaceChildren(...picks
    .filter((p) => !picksFilter || (p.category || "Uncategorised") === picksFilter)
    .map((p) => pickItem(p, colors.get(p.category || "Uncategorised"))));
  fill();

  const LEGEND_MAX = 8;
  const legend = h("ul", { class: "mix-legend" }, cats.map(([c, n], i) => {
    const b = h("button", { type: "button", "aria-pressed": "false", onclick: () => {
      picksFilter = picksFilter === c ? null : c;
      legend.querySelectorAll("button:not(.more)").forEach((x) => x.setAttribute("aria-pressed", String(x === b && !!picksFilter)));
      fill();
    } }, h("b", { style: `background:${colors.get(c)}` }), `${c} ${n}`);
    return h("li", { hidden: i >= LEGEND_MAX }, b);
  }));
  if (cats.length > LEGEND_MAX) {
    const more = h("button", { type: "button", class: "more", onclick: () => {
      legend.querySelectorAll("li[hidden]").forEach((li) => (li.hidden = false));
      more.parentElement.remove();
    } }, `Show all ${cats.length} categories`);
    legend.append(h("li", {}, more));
  }

  root.replaceChildren(
    h("div", { class: "picks-head" },
      h("h2", { text: `Set ${d.n}${published ? "" : " preview"}` }),
      h("p", {}, `${picks.length} picks${engine ? `, ${engine}` : ""}${when ? `, ${published ? "published" : "dry run"} ${ago(when)}` : ""}${d.timing ? `, ${Math.round(d.timing)}s` : ""}`,
        published && d.set.url ? h("span", {}, ". ", h("a", { href: d.set.url, target: "_blank", rel: "noopener", text: "Open in Notion" })) : null)),
    verdict,
    h("div", { class: "mix", role: "img", "aria-label": "Category mix" },
      cats.map(([c, n]) => h("i", { style: `flex:${n};background:${colors.get(c)}`, title: `${c}: ${n}` }))),
    legend,
    list,
  );
}

// ---------------------------------------------------------------- ledger
function setRow(s) {
  const f = Math.min(1, s.count / s.size);
  const li = h("li", { class: "set-row", tabindex: "0", role: "button", "data-n": s.n, "aria-expanded": "false" },
    h("span", { class: "set-n", text: s.special ? `Special` : `Set ${s.n}` }),
    h("span", { class: "fill", title: `${s.count} of ${s.size}` }, h("i", { class: f < 1 ? "short" : null, style: `width:${f * 100}%` })),
    h("span", { class: "set-count", text: `${s.count} of ${s.size}` }),
    h("span", { class: "chip", title: s.engine ? null : "No engine recorded for this set", text: s.engine || "—" }),
    h("span", { class: "set-date", title: s.repaired ? `Picks repaired ${s.repaired}` : null, text: s.at ? dateFmt.format(s.at * 1000) : "" }),
    s.url ? h("a", { class: "set-open", href: s.url, target: "_blank", rel: "noopener", text: "Notion", onclick: (e) => e.stopPropagation() }) : h("span"),
  );
  const toggle = () => toggleSet(li, s.n);
  li.addEventListener("click", toggle);
  li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  return li;
}

async function toggleSet(li, n) {
  const next = li.nextElementSibling;
  if (next?.classList.contains("set-detail")) { next.remove(); li.setAttribute("aria-expanded", "false"); return; }
  li.setAttribute("aria-expanded", "true");
  const box = h("li", { class: "set-detail" }, h("p", { class: "empty", text: "Loading picks…" }));
  li.after(box);
  try {
    const d = await api(`/api/set?n=${n}`);
    box.replaceChildren(d.picks.length
      ? h("ol", { class: "picklist" }, d.picks.map((p) => pickItem(p)))
      : h("p", { class: "empty", text: "No pick list survives for this set." }));
  } catch (e) {
    box.replaceChildren(h("p", { class: "empty", text: e.message }));
  }
}

function ledgerSets() {
  const f = $("ledger-filter").value;
  return S.ov.sets.filter((s) => !f || (f === "legacy" ? !engineById(s.engine) : s.engine === f));
}

function renderLedger() {
  const sel = $("ledger-filter");
  if (sel.options.length === 1) {
    for (const e of S.ov.engines) sel.append(h("option", { value: e.id, text: `${e.id} ${e.name}` }));
    sel.append(h("option", { value: "legacy", text: "Earlier or unrecorded" }));
  }
  const sets = ledgerSets();
  const shown = sets.slice(0, S.ledgerShown);
  const full = sets.filter((s) => s.count >= s.size).length;
  $("ledger-summary").textContent = `${fmt.format(sets.length)} sets, ${fmt.format(full)} of them full. Click a set to see its repos.`;
  $("ledger").replaceChildren(...shown.map(setRow));
  $("ledger-more").hidden = shown.length >= sets.length;
}

function revealSet(n) {
  showTab("sets");
  $("ledger-filter").value = "";
  const idx = S.ov.sets.findIndex((s) => String(s.n) === String(n));
  if (idx >= S.ledgerShown) S.ledgerShown = idx + 20;
  renderLedger();
  const li = $("ledger").querySelector(`[data-n="${n}"]`);
  if (li) { li.scrollIntoView({ block: "center" }); toggleSet(li, n); li.focus({ preventScroll: true }); }
}

// ---------------------------------------------------------------- runs
function renderRuns(runs) {
  const ol = $("runs");
  if (!runs?.length) { ol.replaceChildren(h("li", { class: "empty", text: "No runs recorded yet." })); return; }
  ol.replaceChildren(...runs.map((r) => {
    const res = r.result ? `${r.result.count} picks for Set ${r.result.set}` : (r.stopped ? "stopped" : "no result file");
    const li = h("li", { class: "run-row", tabindex: "0", role: "button" },
      h("span", { class: "run-what", text: `${r.mode === "publish" ? "Publish" : "Dry run"}` }),
      h("span", { class: "chip", text: r.engine }),
      h("span", { class: "set-count", text: r.elapsed != null ? clock(r.elapsed) : "" }),
      h("span", { class: "run-res", text: `${timeFmt.format(r.started * 1000)}, ${res}` }),
      h("span", { class: "run-rc " + (r.rc === 0 ? "ok" : "err"), text: r.rc === 0 ? "ok" : `exit ${r.rc}` }));
    const open = () => openReplay(r.id);
    li.addEventListener("click", open);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } });
    return li;
  }));
}

async function openReplay(id) {
  if (S.run.active) { toast("Wait for the current run to finish before opening an old log."); return; }
  let d;
  try { d = await api(`/api/run-log?id=${encodeURIComponent(id)}`); } catch (e) { toast(e.message, true); return; }
  S.replay = id;
  clearLog();
  addLines(d.lines);
  const r = d.run || {};
  $("replay-text").textContent = `Viewing the log of a ${r.mode === "publish" ? "publish" : "dry run"} with ${r.engine || "?"} from ${r.started ? timeFmt.format(r.started * 1000) : id}.`;
  $("replay").hidden = false;
  $("led").className = "led " + (r.rc === 0 ? "ok" : "err");
  $("status-text").textContent = r.rc === 0 ? "Finished" : `Exited with code ${r.rc}`;
  $("status-time").textContent = r.elapsed != null ? clock(r.elapsed) : "";
  showTab("console");
  if (r.result) loadPicks(r.result.set, r.result.kind);
}

function exitReplay() {
  S.replay = null;
  $("replay").hidden = true;
  disconnect();
  connect();   // hello replays the live/latest buffer
}

// ---------------------------------------------------------------- tabs
const TABS = ["console", "picks", "sets", "runs"];
function activeTab() { return TABS.find((t) => $(`tab-${t}`).getAttribute("aria-selected") === "true"); }
function showTab(name) {
  for (const t of TABS) {
    $(`tab-${t}`).setAttribute("aria-selected", String(t === name));
    $(`tab-${t}`).tabIndex = t === name ? 0 : -1;
    $(`pane-${t}`).hidden = t !== name;
  }
  if (name === "runs") api("/api/runs").then((d) => renderRuns(d.runs)).catch(() => {});
}

// ---------------------------------------------------------------- search
let findTimer = null;
let findIdx = -1;
async function doFind() {
  const q = $("find").value.trim();
  const box = $("find-results");
  if (q.length < 2) { box.hidden = true; return; }
  let d;
  try { d = await api(`/api/search?q=${encodeURIComponent(q)}`); } catch (e) { return; }
  if (q !== $("find").value.trim()) return;
  findIdx = -1;
  box.replaceChildren(...(d.hits.length
    ? [h("p", { text: d.total > d.hits.length ? `${d.total} matches, showing ${d.hits.length}.` : `${d.total} ${d.total === 1 ? "match" : "matches"}.` }),
       ...d.hits.map((x) => h("a", { class: "hit", href: x.url, "data-set": x.set,
         onclick: (e) => { if (e.metaKey || e.ctrlKey) return; e.preventDefault(); closeFind(); revealSet(x.set); } },
         h("span", { class: "hit-set", text: /^\d+$/.test(x.set) ? `Set ${x.set}` : x.set }),
         h("b", { text: x.repo }),
         h("span", { class: "d", text: [x.category, x.description].filter(Boolean).join(". ") })))]
    : [h("p", { text: `Nothing published matches “${q}”. It hasn't appeared in any set yet.` })]));
  box.hidden = false;
}
function closeFind() { $("find-results").hidden = true; findIdx = -1; }
function moveFind(dir) {
  const hits = [...$("find-results").querySelectorAll(".hit")];
  if (!hits.length) return;
  findIdx = (findIdx + dir + hits.length) % hits.length;
  hits.forEach((x, i) => x.classList.toggle("on", i === findIdx));
  hits[findIdx].scrollIntoView({ block: "nearest" });
}

// ---------------------------------------------------------------- wiring
function typing(e) { return /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) && e.target.type !== "radio" && e.target.type !== "checkbox"; }

function wire() {
  $("btn-dry").addEventListener("click", () => startRun("dry"));
  $("btn-pub").addEventListener("click", publishClick);
  $("btn-stop").addEventListener("click", () => post("/api/stop").catch((e) => toast(e.message, true)));
  for (const t of TABS) {
    const tab = $(`tab-${t}`);
    tab.addEventListener("click", () => showTab(t));
    tab.addEventListener("keydown", (e) => {
      const i = TABS.indexOf(t);
      const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      const next = TABS[(to + TABS.length) % TABS.length];
      showTab(next);
      $(`tab-${next}`).focus();
    });
  }
  $("log-filter").addEventListener("input", applyFilter);
  $("follow").addEventListener("change", () => { if ($("follow").checked) $("log").scrollTop = $("log").scrollHeight; });
  $("log").addEventListener("scroll", () => {
    const log = $("log");
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
    if (!atEnd && $("follow").checked && S.run.active) $("follow").checked = false;
  });
  $("log-copy").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(S.lines.join("\n")); toast(`Copied ${S.lines.length} lines.`); }
    catch { toast("The browser blocked clipboard access.", true); }
  });
  $("log-save").addEventListener("click", () => {
    const blob = new Blob([S.lines.join("\n") + "\n"], { type: "text/plain" });
    const a = h("a", { href: URL.createObjectURL(blob), download: `n50-${S.replay || S.run.id || "log"}.log` });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  const nb = $("notify");
  const syncNotify = () => nb.setAttribute("aria-pressed", String(store.get("notify", false) && "Notification" in window && Notification.permission === "granted"));
  if (!("Notification" in window)) nb.hidden = true;
  nb.addEventListener("click", async () => {
    if (store.get("notify", false)) { store.set("notify", false); syncNotify(); toast("Notifications off."); return; }
    const p = await Notification.requestPermission();
    store.set("notify", p === "granted");
    syncNotify();
    toast(p === "granted" ? "You'll get a notification when a run finishes in a background tab." : "Notifications are blocked for this site in the browser settings.", p !== "granted");
  });
  syncNotify();
  $("replay-exit").addEventListener("click", exitReplay);
  $("ledger-filter").addEventListener("change", () => { S.ledgerShown = 60; renderLedger(); });
  $("ledger-more").addEventListener("click", () => { S.ledgerShown += 120; renderLedger(); });

  const find = $("find");
  find.addEventListener("input", () => { clearTimeout(findTimer); findTimer = setTimeout(doFind, 180); });
  find.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); moveFind(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveFind(-1); }
    else if (e.key === "Enter") { const on = $("find-results").querySelector(".hit.on") || $("find-results").querySelector(".hit"); if (on) on.click(); }
    else if (e.key === "Escape") { closeFind(); find.blur(); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".find")) closeFind(); });

  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Escape" && countdown) { cancelCountdown(); return; }
    if (typing(e)) return;
    if (e.key === "/") { e.preventDefault(); find.focus(); find.select(); }
    else if ((e.key === "d" || e.key === "D") && !S.run.active) startRun("dry");
    else if (/^[1-4]$/.test(e.key)) showTab(TABS[+e.key - 1]);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { if (!S.run.active) disconnect(); }
    else {
      if (!S.run.active && /^[✓✕]/.test(document.title)) document.title = "n50";
      connect();
      loadOverview();
    }
  });
}

wire();
TABS.forEach((t, i) => ($(`tab-${t}`).tabIndex = i === 0 ? 0 : -1));
loadOverview().then(() => {
  connect();
  if (S.ov?.preview) loadPicks(S.ov.next);
});
