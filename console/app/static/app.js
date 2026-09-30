"use strict";
// NodeWatch 대시보드 (SPEC §10). 빌드 단계·프레임워크·CDN 없음. API는 상대경로만 사용한다.

// ------------------------------------------------------------------ 상수

const NODE_POLL_MS = 3000;
const JOB_POLL_MS = 2000;
const HISTORY_POLL_MS = 5000;
const MIN_WINDOW_MS = 5 * 60 * 1000;   // 시계열 최소 표시 창. 보유 샘플이 더 길면 창을 늘린다 (SAMPLES_MAX)
const TICK_STEPS_MIN = [1, 2, 5, 10, 15, 30, 60, 120, 180, 360, 720]; // x축 눈금 후보 (분)
const SERIES_GAP_MS = 12500;            // 샘플 간격이 이보다 크면 수집 실패 구간으로 보고 선을 끊는다

// 표시용 임계선. SPEC §4.3 / §5 기본값과 같다.
// console env로 임계치를 바꾸면 판정은 바뀌지만 이 표시선은 바뀌지 않는다 (API가 임계치를 제공하지 않음).
const THRESHOLDS = {
  cpu: { warn: 80, crit: 95 },
  mem: { warn: 85, crit: 95 },
  disk: { warn: 80, crit: 90 },
};
const SLOW_MS = 1500;

const STATUS_LABEL = {
  HEALTHY: "정상", WARNING: "경고", CRITICAL: "장애", UNREACHABLE: "장애 · 통신두절", UNKNOWN: "확인 중",
};
const RESULT_LABEL = {
  PENDING: "대기", RUNNING: "실행 중", SUCCESS: "성공", FAILED: "실패", UNKNOWN: "결과 미확인",
};
const JOB_LABEL = {
  RUNNING: "실행 중", COMPLETED: "완료", PARTIAL: "부분 성공", FAILED: "실패", INTERRUPTED: "중단됨",
};
const ERROR_LABEL = {
  CONNECT_ERROR: "연결 실패 (미전달)",
  TIMEOUT: "응답 없음 (결과 미확인)",
  AGENT_ERROR: "agent 오류",
  EXEC_ERROR: "실행 실패",
  INTERRUPTED: "console 재기동",
};
const RISK_LABEL = { HIGH: "위험도 높음", MEDIUM: "위험도 중간", LOW: "위험도 낮음", NONE: "읽기 전용" };
const SERIES_COLORS = ["#5794f2", "#f2994a", "#b877d9", "#3fb9a5", "#e05f8a", "#c9a227", "#8ab8ff", "#9ccc65"];

// ------------------------------------------------------------------ 상태

const state = {
  nodes: [],
  samples: {},          // node_id → [{ts, cpu, mem, disk, latency}]
  actions: [],
  selected: new Set(),  // 일괄 제어 대상
  lastOk: null,         // 마지막으로 console 응답을 받은 시각
  connected: true,
  jobDetailId: null,
  openOutputs: new Set(),
  reconcileUntil: 0,
  hover: null,          // {key, clientX, clientY}
};

// ------------------------------------------------------------------ DOM 헬퍼

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

const SVG_NS = "http://www.w3.org/2000/svg";
function s(tag, attrs, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  applyAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

function applyAttrs(el, attrs) {
  if (!attrs) return;
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
}

function appendChildren(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const $ = (sel) => document.querySelector(sel);

function badge(cls, text) {
  return h("span", { class: `badge ${cls}`, text });
}

// ------------------------------------------------------------------ 시간 (API는 UTC, 표시는 브라우저 로컬)

const pad = (n) => String(n).padStart(2, "0");

function fmtClock(d) {
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtDateTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${fmtClock(d)}`;
}

function fmtAgo(iso) {
  if (!iso) return "수집 이력 없음";
  const sec = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 60) return `${sec}초 전`;
  if (sec < 3600) return `${Math.floor(sec / 60)}분 ${sec % 60}초 전`;
  return `${Math.floor(sec / 3600)}시간 전`;
}

function fmtDuration(ms) {
  if (ms === null || ms === undefined) return "-";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

// ------------------------------------------------------------------ API

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = "GET", body } = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : {},
      body: body !== undefined ? JSON.stringify(body) : undefined,
      cache: "no-store",
    });
  } catch (e) {
    setConnected(false);
    throw new ApiError(0, "콘솔에 연결할 수 없습니다");
  }
  setConnected(true);
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    data = null;
  }
  if (!resp.ok) {
    const detail = data && typeof data.detail === "string" ? data.detail : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, detail);
  }
  return data;
}

function setConnected(ok) {
  if (ok) state.lastOk = new Date();
  if (ok === state.connected && ok) return;
  state.connected = ok;
  const banner = $("#banner");
  document.body.classList.toggle("disconnected", !ok);
  banner.hidden = ok;
  if (!ok) {
    const last = state.lastOk ? fmtClock(state.lastOk) : "없음";
    banner.textContent = `콘솔 연결 끊김 — 마지막 갱신 ${last}`;
  }
}

// ------------------------------------------------------------------ 라우팅 (hash)

const TABS = ["status", "control", "history", "demo"];

function currentRoute() {
  const [tab, id] = location.hash.replace(/^#/, "").split("/");
  return { tab: TABS.includes(tab) ? tab : "status", id: id || null };
}

function onRoute() {
  const { tab, id } = currentRoute();
  for (const t of TABS) $(`#tab-${t}`).hidden = t !== tab;
  document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  if (tab === "status") {
    renderStatus();
    refreshSamples();
  } else if (tab === "control") {
    renderControl();
  } else if (tab === "history") {
    if (id) openJob(id);
    else closeJob();
  } else if (tab === "demo") {
    renderDemo();
  }
}

// ------------------------------------------------------------------ 노드 폴링

async function pollNodes() {
  try {
    state.nodes = await api("/api/nodes");
    const { tab } = currentRoute();
    if (tab === "status") {
      renderStatus();
      await refreshSamples();
    } else if (tab === "control") {
      renderTargets();
    } else if (tab === "demo") {
      updateDemoBadges();
    }
  } catch (e) {
    // 연결 끊김 배너는 api()가 처리한다. 기존 화면 값은 유지.
  } finally {
    setTimeout(pollNodes, NODE_POLL_MS);
  }
}

async function refreshSamples() {
  if (!state.nodes.length) return;
  const results = await Promise.allSettled(state.nodes.map((n) => api(`/api/nodes/${encodeURIComponent(n.node_id)}`)));
  results.forEach((r, i) => {
    if (r.status === "fulfilled") state.samples[state.nodes[i].node_id] = r.value.samples || [];
  });
  if (currentRoute().tab === "status") renderPanels();
}

// 1초마다 "n초 전" 갱신
function tickAgo() {
  document.querySelectorAll("[data-ago]").forEach((el) => {
    el.textContent = fmtAgo(el.dataset.ago || null);
  });
}

// ------------------------------------------------------------------ 탭 1: 상태 현황

function renderStatus() {
  const nodes = state.nodes;
  const count = (pred) => nodes.filter(pred).length;
  const summary = $("#summary");
  summary.replaceChildren(
    stat("ok", "정상", count((n) => n.status === "HEALTHY")),
    stat("warn", "경고", count((n) => n.status === "WARNING")),
    stat("crit", "장애", count((n) => n.status === "CRITICAL" || n.status === "UNREACHABLE")),
    stat("unknown", "확인 중", count((n) => n.status === "UNKNOWN")),
  );
  $("#node-cards").replaceChildren(...nodes.map(nodeCard));
}

function stat(cls, label, n) {
  return h("div", { class: `stat ${cls}` }, h("div", { class: "n", text: n }), h("div", { class: "l", text: label }));
}

function nodeCard(n) {
  const failing = n.consecutive_failures > 0;
  const reasons = n.reasons.length
    ? h("ul", { class: "reasons" }, n.reasons.map((r) => h("li", { text: r })))
    : h("ul", { class: "reasons none" }, h("li", { text: "이상 없음" }));

  const values = h("div", { class: failing ? "stale-values" : null },
    n.metrics
      ? [
        metricRow("CPU", n.metrics.cpu_pct, THRESHOLDS.cpu),
        metricRow("MEM", n.metrics.mem_pct, THRESHOLDS.mem),
        metricRow("DISK", n.metrics.disk_pct, THRESHOLDS.disk),
      ]
      : h("p", { class: "muted", text: "수집된 메트릭 없음" }),
    n.daemons.length
      ? h("ul", { class: "daemons" }, n.daemons.map((d) => h("li", null,
        h("span", { text: d.name }),
        h("span", { class: `d-${d.status}`, text: d.status === "RUNNING" ? `RUNNING · pid ${d.pid}` : d.status }),
      )))
      : null,
  );

  return h("article", { class: `card st-${n.status}` },
    h("div", { class: "card-head" },
      h("div", null, h("span", { class: "name", text: n.node_name }), h("span", { class: "id", text: n.node_id })),
      badge(`s-${n.status}`, STATUS_LABEL[n.status] || n.status),
    ),
    reasons,
    values,
    h("div", { class: "card-foot" },
      h("span", null,
        "응답 ", n.latency_ms !== null ? `${n.latency_ms}ms` : "-",
        " · 마지막 수집 ", h("span", { dataset: { ago: n.last_success_at || "" }, text: fmtAgo(n.last_success_at) }),
      ),
      failing
        ? h("span", { class: "fail-badge", title: n.last_error ? n.last_error.message : "", text: `수집 실패 ${n.consecutive_failures}회` })
        : null,
      n.skipped_cycles > 0 ? h("span", { text: `건너뛴 주기 ${n.skipped_cycles}` }) : null,
    ),
    failing && n.last_error
      ? h("div", { class: "card-foot" }, h("span", { text: `${n.last_error.type}: ${n.last_error.message}` }))
      : null,
  );
}

function metricRow(label, value, thr) {
  const level = value >= thr.crit ? "crit" : value >= thr.warn ? "warn" : "";
  return h("div", { class: "metric" },
    h("span", { class: "k", text: label }),
    h("div", { class: "bar" },
      h("i", { class: level, style: `width:${Math.min(100, value)}%` }),
      h("b", { style: `left:${thr.warn}%`, title: `경고 ${thr.warn}%` }),
      h("b", { style: `left:${thr.crit}%`, title: `장애 ${thr.crit}%` }),
    ),
    h("span", { class: "v", text: `${value.toFixed(1)}%` }),
  );
}

// ---------------------------------------------------------------- 시계열 패널 (SVG)

const PANELS = [
  { key: "cpu", title: "CPU 사용률", unit: "%", max: 100, thr: THRESHOLDS.cpu },
  { key: "mem", title: "메모리 사용률", unit: "%", max: 100, thr: THRESHOLDS.mem },
  { key: "disk", title: "디스크 사용률", unit: "%", max: 100, thr: THRESHOLDS.disk },
  { key: "latency", title: "응답 시간", unit: "ms", max: null, thr: { warn: SLOW_MS }, note: `경고 ≥ ${SLOW_MS}ms` },
];

function nodeColor(nodeId) {
  const i = state.nodes.findIndex((n) => n.node_id === nodeId);
  return SERIES_COLORS[(i < 0 ? 0 : i) % SERIES_COLORS.length];
}

function seriesWindowMs(now) {
  // console이 보유한 가장 오래된 샘플까지 보여 준다 (분 단위 올림, 최소 5분).
  let oldest = now;
  for (const list of Object.values(state.samples)) {
    if (list.length) oldest = Math.min(oldest, new Date(list[0].ts).getTime());
  }
  return Math.max(MIN_WINDOW_MS, Math.ceil((now - oldest) / 60000) * 60000);
}

function fmtWindow(ms) {
  const min = Math.round(ms / 60000);
  return min < 60 ? `${min}분` : `${Math.floor(min / 60)}시간${min % 60 ? ` ${min % 60}분` : ""}`;
}

function renderPanels() {
  const now = Date.now();
  const windowMs = seriesWindowMs(now);
  $("#series-range").textContent = `최근 ${fmtWindow(windowMs)} · 수집 실패 구간은 선이 끊깁니다`;
  $("#legend").replaceChildren(...state.nodes.map((n) =>
    h("span", null, h("i", { style: `background:${nodeColor(n.node_id)}` }), n.node_name)));

  const container = $("#panels");
  if (container.children.length !== PANELS.length) {
    container.replaceChildren(...PANELS.map((p) => h("div", { class: "panel", dataset: { key: p.key } },
      h("h3", null, `${p.title} (${p.unit})`, p.note ? h("span", { class: "muted", text: ` · ${p.note}` }) : null), s("svg", { role: "img", "aria-label": p.title }))));
  }
  for (const p of PANELS) {
    const el = container.querySelector(`[data-key="${p.key}"] svg`);
    drawChart(el, p, now, windowMs);
  }
  if (state.hover) showHover(state.hover.key, state.hover.clientX, state.hover.clientY);
}

function drawChart(svg, panel, now, windowMs) {
  const W = svg.clientWidth || 420;
  const H = 190;
  const m = { l: 40, r: 10, t: 8, b: 22 };
  const iw = W - m.l - m.r;
  const ih = H - m.t - m.b;
  const t0 = now - windowMs;

  const series = state.nodes.map((n) => ({
    id: n.node_id,
    name: n.node_name,
    color: nodeColor(n.node_id),
    points: (state.samples[n.node_id] || [])
      .map((smp) => ({ t: new Date(smp.ts).getTime(), v: smp[panel.key] }))
      .filter((pt) => pt.t >= t0 - SERIES_GAP_MS),
  }));

  let yMax = panel.max;
  if (yMax === null) {
    // 응답 시간은 평소 수 ms라 임계선(1500ms)에 스케일을 맞추면 선이 바닥에 붙는다. 데이터 기준으로 자동 스케일.
    const maxV = Math.max(0, ...series.flatMap((sr) => sr.points.map((pt) => pt.v)));
    yMax = niceCeil(Math.max(20, maxV * 1.2));
  }
  const x = (t) => m.l + ((t - t0) / windowMs) * iw;
  const y = (v) => m.t + ih - (Math.min(v, yMax) / yMax) * ih;

  const g = [];
  // y 격자: 눈금이 정수가 되도록 4 또는 5 등분 (예: 100 → 25 간격, 50 → 10 간격)
  const div = Number.isInteger(yMax / 4) ? 4 : 5;
  for (let i = 0; i <= div; i++) {
    const v = (yMax / div) * i;
    g.push(s("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));
    g.push(s("text", { x: m.l - 6, y: y(v) + 3, "text-anchor": "end" }, Math.round(v)));
  }
  // x 격자: 눈금이 8개 이하가 되는 가장 작은 간격 (로컬 시각 기준 정각에 맞춤)
  const stepMs = (TICK_STEPS_MIN.find((mn) => windowMs / (mn * 60000) <= 8) || 720) * 60000;
  const tzOffsetMs = new Date(now).getTimezoneOffset() * 60000;
  const firstTick = Math.ceil((t0 - tzOffsetMs) / stepMs) * stepMs + tzOffsetMs;
  for (let t = firstTick; t <= now; t += stepMs) {
    const d = new Date(t);
    g.push(s("line", { class: "gridline", x1: x(t), x2: x(t), y1: m.t, y2: m.t + ih }));
    g.push(s("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, `${pad(d.getHours())}:${pad(d.getMinutes())}`));
  }
  // 임계선 (현재 스케일 범위 안에 있을 때만)
  if (panel.thr.warn !== undefined && panel.thr.warn <= yMax) {
    g.push(s("line", { class: "thr warn", x1: m.l, x2: W - m.r, y1: y(panel.thr.warn), y2: y(panel.thr.warn) }));
  }
  if (panel.thr.crit !== undefined && panel.thr.crit <= yMax) {
    g.push(s("line", { class: "thr crit", x1: m.l, x2: W - m.r, y1: y(panel.thr.crit), y2: y(panel.thr.crit) }));
  }

  // 시리즈: 간격이 SERIES_GAP_MS보다 크면 선을 끊는다 (수집 실패 구간을 숨기지 않음)
  const lines = [];
  for (const sr of series) {
    let d = "";
    let prev = null;
    const isolated = [];
    sr.points.forEach((pt, i) => {
      const next = sr.points[i + 1];
      const newSeg = !prev || pt.t - prev.t > SERIES_GAP_MS;
      d += `${newSeg ? "M" : "L"}${x(Math.max(pt.t, t0)).toFixed(1)},${y(pt.v).toFixed(1)}`;
      if (newSeg && (!next || next.t - pt.t > SERIES_GAP_MS)) isolated.push(pt);
      prev = pt;
    });
    if (d) lines.push(s("path", { class: "series", d, stroke: sr.color }));
    for (const pt of isolated) lines.push(s("circle", { cx: x(pt.t), cy: y(pt.v), r: 2, fill: sr.color }));
  }
  const hasData = series.some((sr) => sr.points.length);

  const overlay = s("rect", {
    x: m.l, y: m.t, width: iw, height: ih, fill: "transparent",
    onmousemove: (ev) => {
      state.hover = { key: panel.key, clientX: ev.clientX, clientY: ev.clientY };
      showHover(panel.key, ev.clientX, ev.clientY);
    },
    onmouseleave: () => {
      state.hover = null;
      hideHover();
    },
  });

  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.replaceChildren(
    s("g", { class: "axis" }, g),
    s("g", null, lines),
    hasData ? null : s("text", { class: "empty", x: m.l + iw / 2, y: m.t + ih / 2, "text-anchor": "middle" }, "수집 데이터 없음"),
    s("line", { class: "cursor", x1: 0, x2: 0, y1: m.t, y2: m.t + ih, visibility: "hidden" }),
    overlay,
  );
  svg._chart = { series, x, t0, W, m, iw, panel, windowMs };
}

function niceCeil(v) {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  for (const f of [1, 2, 2.5, 5, 10]) if (f * p >= v) return f * p;
  return 10 * p;
}

function showHover(key, clientX, clientY) {
  const svg = document.querySelector(`#panels [data-key="${key}"] svg`);
  if (!svg || !svg._chart) return;
  const { series, t0, W, m, iw, panel, windowMs } = svg._chart;
  const rect = svg.getBoundingClientRect();
  const px = ((clientX - rect.left) / rect.width) * W; // viewBox 좌표
  if (px < m.l || px > m.l + iw) return hideHover();
  const t = t0 + ((px - m.l) / iw) * windowMs;

  const rows = [];
  for (const sr of series) {
    let best = null;
    for (const pt of sr.points) if (!best || Math.abs(pt.t - t) < Math.abs(best.t - t)) best = pt;
    if (best && Math.abs(best.t - t) <= SERIES_GAP_MS / 2) rows.push({ sr, pt: best });
  }
  const cursor = svg.querySelector(".cursor");
  cursor.setAttribute("x1", px);
  cursor.setAttribute("x2", px);
  cursor.setAttribute("visibility", "visible");

  const tip = $("#tooltip");
  const fmt = (v) => (panel.unit === "%" ? `${v.toFixed(1)}%` : `${v}ms`);
  tip.replaceChildren(
    h("div", { class: "t", text: fmtClock(new Date(t)) }),
    ...(rows.length
      ? rows.map(({ sr, pt }) => h("div", null, h("i", { style: `background:${sr.color}` }), `${sr.name}  ${fmt(pt.v)}`))
      : [h("div", { class: "muted", text: "수집 값 없음" })]),
  );
  tip.hidden = false;
  const tw = tip.offsetWidth;
  tip.style.left = `${clientX + 14 + tw > window.innerWidth ? clientX - tw - 14 : clientX + 14}px`;
  tip.style.top = `${clientY + 12}px`;
}

function hideHover() {
  $("#tooltip").hidden = true;
  document.querySelectorAll("#panels .cursor").forEach((c) => c.setAttribute("visibility", "hidden"));
}

// ------------------------------------------------------------------ 탭 2: 일괄 제어

async function loadActions() {
  if (state.actions.length) return;
  try {
    state.actions = await api("/api/actions");
  } catch (e) {
    $("#run-error").hidden = false;
    $("#run-error").textContent = `액션 목록을 불러오지 못했습니다: ${e.message}`;
  }
}

const actionByKey = (key) => state.actions.find((a) => a.action === key);
const actionName = (key) => (actionByKey(key) ? actionByKey(key).name : key);

async function renderControl() {
  renderTargets();
  await loadActions();
  const sel = $("#action-select");
  if (!sel.options.length && state.actions.length) {
    sel.replaceChildren(...state.actions.map((a) => h("option", { value: a.action, text: `${a.name} (${a.action})` })));
    renderParamForm();
  }
  updateRunButton();
}

function renderTargets() {
  const list = $("#target-list");
  // 노드 목록이 사라지면 선택에서도 뺀다
  const ids = new Set(state.nodes.map((n) => n.node_id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);

  list.replaceChildren(...state.nodes.map((n) => h("label", null,
    h("input", {
      type: "checkbox", value: n.node_id, checked: state.selected.has(n.node_id),
      onchange: (ev) => {
        if (ev.target.checked) state.selected.add(n.node_id);
        else state.selected.delete(n.node_id);
        updateTargetState();
      },
    }),
    h("span", { text: n.node_name }),
    h("span", { class: "id", text: n.node_id }),
    badge(`s-${n.status}`, STATUS_LABEL[n.status] || n.status),
  )));
  updateTargetState();
}

function updateTargetState() {
  const all = $("#select-all");
  all.checked = state.nodes.length > 0 && state.selected.size === state.nodes.length;
  all.indeterminate = state.selected.size > 0 && !all.checked;

  const unreachable = state.nodes.filter((n) => state.selected.has(n.node_id) && n.status === "UNREACHABLE");
  const warn = $("#unreachable-warning");
  warn.hidden = unreachable.length === 0;
  warn.textContent = unreachable.length
    ? `통신두절 노드 포함: ${unreachable.map((n) => n.node_name).join(", ")} — 실행은 가능하지만 연결 실패(FAILED) 또는 결과 미확인(UNKNOWN)으로 기록될 수 있습니다.`
    : "";
  updateRunButton();
}

function renderParamForm() {
  const a = actionByKey($("#action-select").value);
  const form = $("#param-form");
  const meta = $("#action-meta");
  if (!a) {
    form.replaceChildren();
    meta.replaceChildren();
    return;
  }
  meta.replaceChildren(badge(`risk-${a.risk}`, RISK_LABEL[a.risk] || a.risk),
    a.risk === "HIGH" ? h("span", { text: "실행 전 대상 확인 단계를 거칩니다" }) : null);
  form.replaceChildren(...(a.params.length
    ? a.params.map((p) => h("label", { class: "field" },
      h("span", { text: p.label || p.name }),
      h("select", { name: p.name, dataset: { type: typeof p.choices[0] }, onchange: updateRunButton },
        p.default === null ? h("option", { value: "", text: "선택하세요" }) : null,
        p.choices.map((c) => h("option", { value: String(c), text: String(c), selected: c === p.default })),
      ),
    ))
    : [h("p", { class: "muted", text: "파라미터 없음" })]));
  updateRunButton();
}

function collectParams() {
  const params = {};
  for (const el of $("#param-form").querySelectorAll("select")) {
    if (el.value === "") return null;
    params[el.name] = el.dataset.type === "number" ? Number(el.value) : el.value;
  }
  return params;
}

function updateRunButton() {
  const ok = state.selected.size > 0 && $("#action-select").value && collectParams() !== null;
  $("#run-btn").disabled = !ok;
}

async function onRun() {
  const action = $("#action-select").value;
  const a = actionByKey(action);
  const params = collectParams();
  const targets = state.nodes.filter((n) => state.selected.has(n.node_id));
  if (!a || params === null || !targets.length) return;

  if (a.risk === "HIGH") {
    const paramText = Object.entries(params).map(([k, v]) => `${k}=${v}`).join(", ");
    const ok = await confirmDialog(`${a.name} 실행 확인`, [
      h("p", null, "다음 ", h("strong", { text: `${targets.length}개` }), " 노드에서 ",
        h("strong", { text: a.name }), paramText ? ` (${paramText})` : "", "을(를) 실행합니다."),
      h("ul", null, targets.map((n) => h("li", null, `${n.node_name} (${n.node_id}) `,
        badge(`s-${n.status}`, STATUS_LABEL[n.status] || n.status)))),
      h("p", { class: "muted", text: "위험도가 높은 액션입니다. 대상이 맞는지 확인하세요." }),
    ], "실행");
    if (!ok) return;
  }

  const err = $("#run-error");
  err.hidden = true;
  $("#run-btn").disabled = true;
  try {
    const res = await api("/api/jobs", { method: "POST", body: { targets: targets.map((n) => n.node_id), action, params } });
    location.hash = `#history/${res.job_id}`;
  } catch (e) {
    err.hidden = false;
    err.textContent = `실행 요청 실패: ${e.message}`;
  } finally {
    updateRunButton();
  }
}

function confirmDialog(title, body, okText) {
  const dlg = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-body").replaceChildren(...body);
  $("#confirm-ok").textContent = okText;
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

// ------------------------------------------------------------------ 탭 3: 실행 이력

let historyTimer = null;
let detailTimer = null;

async function loadHistory() {
  clearTimeout(historyTimer);
  if (currentRoute().tab !== "history" || state.jobDetailId) return;
  try {
    await loadActions();
    const jobs = await api("/api/jobs?limit=50");
    renderJobTable(jobs);
    if (jobs.some((j) => j.status === "RUNNING")) historyTimer = setTimeout(loadHistory, HISTORY_POLL_MS);
  } catch (e) {
    historyTimer = setTimeout(loadHistory, HISTORY_POLL_MS);
  }
}

function countsView(counts) {
  const order = ["SUCCESS", "FAILED", "UNKNOWN", "RUNNING", "PENDING"];
  const parts = order.filter((k) => counts[k] > 0).map((k) => h("span", { class: `c-${k}`, text: `${RESULT_LABEL[k]} ${counts[k]}` }));
  return h("span", { class: "counts" }, parts.length ? parts : "-");
}

function renderJobTable(jobs) {
  const tbody = $("#job-table tbody");
  if (!jobs.length) {
    tbody.replaceChildren(h("tr", null, h("td", { colspan: 6, class: "muted", text: "실행 이력이 없습니다" })));
    return;
  }
  tbody.replaceChildren(...jobs.map((j) => h("tr", { class: "click", onclick: () => { location.hash = `#history/${j.job_id}`; } },
    h("td", { text: fmtDateTime(j.created_at) }),
    h("td", null, actionName(j.action), j.parent_job_id ? h("span", { class: "muted", text: " (재실행)" }) : null),
    h("td", { text: Object.values(j.counts).reduce((a, b) => a + b, 0) }),
    h("td", null, countsView(j.counts)),
    h("td", { text: j.requested_by }),
    h("td", null, badge(`j-${j.status}`, JOB_LABEL[j.status] || j.status)),
  )));
}

function openJob(jobId) {
  if (state.jobDetailId !== jobId) state.openOutputs.clear();
  state.jobDetailId = jobId;
  clearTimeout(historyTimer);
  $("#job-list-wrap").hidden = true;
  $("#job-detail").hidden = false;
  loadJobDetail();
}

function closeJob() {
  state.jobDetailId = null;
  clearTimeout(detailTimer);
  $("#job-detail").hidden = true;
  $("#job-list-wrap").hidden = false;
  loadHistory();
}

async function loadJobDetail() {
  clearTimeout(detailTimer);
  const jobId = state.jobDetailId;
  if (!jobId || currentRoute().tab !== "history") return;
  try {
    await loadActions();
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (state.jobDetailId !== jobId) return;
    renderJobDetail(job);
    if (job.status === "RUNNING" || Date.now() < state.reconcileUntil) detailTimer = setTimeout(loadJobDetail, JOB_POLL_MS);
  } catch (e) {
    if (e.status === 404) {
      $("#job-detail").replaceChildren(h("p", { class: "error-text", text: "존재하지 않는 job입니다." }),
        h("a", { href: "#history", text: "← 목록" }));
      return;
    }
    detailTimer = setTimeout(loadJobDetail, JOB_POLL_MS);
  }
}

function renderJobDetail(job) {
  const unknown = job.counts.UNKNOWN || 0;
  const failed = job.counts.FAILED || 0;
  const running = job.status === "RUNNING";
  const paramText = Object.keys(job.params).length
    ? Object.entries(job.params).map(([k, v]) => `${k}=${v}`).join(", ")
    : "없음";

  const detail = $("#job-detail");
  detail.replaceChildren(
    h("div", { class: "detail-head" },
      h("div", null,
        h("a", { href: "#history", text: "← 목록" }), "  ",
        h("h2", { style: "display:inline;margin-left:8px" }, actionName(job.action), " "),
        badge(`j-${job.status}`, JOB_LABEL[job.status] || job.status),
      ),
      h("div", { class: "detail-actions" },
        h("button", {
          class: "btn", disabled: unknown === 0 || running, onclick: () => reconcile(job),
          title: "결과 미확인(UNKNOWN) 대상의 실제 결과를 agent에 다시 조회합니다",
        }, `결과 재확인${unknown ? ` (${unknown})` : ""}`),
        h("button", {
          class: "btn", disabled: (failed === 0 && unknown === 0) || running, onclick: () => retry(job),
        }, `실패 대상 재실행${failed ? ` (${failed})` : ""}`),
      ),
    ),
    h("div", { class: "box" },
      h("dl", { class: "detail-meta" },
        metaItem("job_id", h("span", { class: "mono", text: job.job_id })),
        metaItem("파라미터", paramText),
        metaItem("요청자", job.requested_by),
        metaItem("생성", fmtDateTime(job.created_at)),
        metaItem("완료", fmtDateTime(job.finished_at)),
        job.parent_job_id ? metaItem("원본 job", h("a", { href: `#history/${job.parent_job_id}`, class: "mono", text: job.parent_job_id.slice(0, 8) })) : null,
      ),
      h("div", null, countsView(job.counts)),
      unknown
        ? h("p", { class: "note", text: "결과 미확인: 명령이 이미 전달·실행됐을 수 있어 자동 재시도하지 않습니다. [결과 재확인]으로 agent의 실제 결과를 조회하세요." })
        : null,
      job.status === "INTERRUPTED"
        ? h("p", { class: "note", text: "console 재기동으로 중단된 job입니다. 대상별 결과는 재확인으로 갱신될 수 있지만 job 상태는 '중단됨'으로 유지됩니다." })
        : null,
    ),
    h("div", { class: "table-wrap", style: "margin-top:14px" },
      h("table", { class: "table" },
        h("thead", null, h("tr", null, ["노드", "상태", "오류", "종료코드", "소요", "출력"].map((t) => h("th", { text: t })))),
        h("tbody", null, job.results.map(resultRow)),
      ),
    ),
  );
}

function metaItem(label, value) {
  return h("div", null, h("dt", { text: label }), h("dd", null, value));
}

function nodeName(nodeId) {
  const n = state.nodes.find((x) => x.node_id === nodeId);
  return n ? n.node_name : nodeId;
}

function resultRow(r) {
  let output = h("span", { class: "muted", text: "-" });
  if (r.output !== null && r.output !== undefined) {
    const lines = r.output.split("\n").length;
    const det = h("details", {
      class: "output", open: state.openOutputs.has(r.node_id),
      ontoggle: (ev) => {
        if (ev.target.open) state.openOutputs.add(r.node_id);
        else state.openOutputs.delete(r.node_id);
      },
    },
    h("summary", { text: `출력 보기 (${lines}줄)` }),
    h("pre", { text: r.output }),
    r.output_truncated ? h("p", { class: "note", text: "출력 일부 생략 (저장 상한 초과)" }) : null,
    );
    output = det;
  }
  return h("tr", null,
    h("td", null, nodeName(r.node_id), h("div", { class: "muted mono", text: r.command_id.slice(0, 8) })),
    h("td", null, badge(`r-${r.status}`, RESULT_LABEL[r.status] || r.status),
      r.reconciled ? h("div", { class: "muted", text: "재확인됨" }) : null),
    h("td", null,
      r.error_type ? h("div", { text: ERROR_LABEL[r.error_type] || r.error_type }) : "-",
      r.error_message ? h("div", { class: "muted", text: r.error_message }) : null),
    h("td", { text: r.exit_code ?? "-" }),
    h("td", { text: fmtDuration(r.duration_ms) }),
    h("td", null, output),
  );
}

async function reconcile(job) {
  try {
    await api(`/api/jobs/${encodeURIComponent(job.job_id)}/reconcile`, { method: "POST" });
    state.reconcileUntil = Date.now() + 10000; // reconcile은 비동기라 잠시 폴링한다
    loadJobDetail();
  } catch (e) {
    alert(`결과 재확인 요청 실패: ${e.message}`);
  }
}

async function retry(job) {
  const failedTargets = job.results.filter((r) => r.status === "FAILED");
  const unknownTargets = job.results.filter((r) => r.status === "UNKNOWN");
  const includeBox = unknownTargets.length ? h("input", { type: "checkbox", id: "include-unknown" }) : null;
  const ok = await confirmDialog("실패 대상 재실행", [
    h("p", null, "새 job으로 ", h("strong", { text: actionName(job.action) }), "을(를) 다시 실행합니다."),
    failedTargets.length
      ? h("div", null, h("p", { text: "실패(FAILED) 대상:" }), h("ul", null, failedTargets.map((r) => h("li", { text: `${nodeName(r.node_id)} — ${ERROR_LABEL[r.error_type] || r.error_type || ""}` }))))
      : h("p", { class: "muted", text: "실패(FAILED) 대상이 없습니다." }),
    includeBox
      ? h("label", { class: "check" }, includeBox,
        `결과 미확인(UNKNOWN) ${unknownTargets.length}개도 포함 — 이미 실행됐을 수 있어 중복 실행될 수 있습니다. 먼저 [결과 재확인]을 권장합니다.`)
      : null,
  ], "재실행");
  if (!ok) return;
  const includeUnknown = includeBox ? includeBox.checked : false;
  if (!failedTargets.length && !includeUnknown) return;
  try {
    const res = await api(`/api/jobs/${encodeURIComponent(job.job_id)}/retry`, { method: "POST", body: { include_unknown: includeUnknown } });
    location.hash = `#history/${res.job_id}`;
  } catch (e) {
    alert(`재실행 요청 실패: ${e.message}`);
  }
}

// ------------------------------------------------------------------ 탭 4: 데모 제어

async function renderDemo() {
  await loadActions();
  const restart = actionByKey("RESTART_DAEMON");
  const daemons = restart ? restart.params.find((p) => p.name === "daemon").choices : [];
  $("#chaos-cards").replaceChildren(...state.nodes.map((n) => chaosCard(n, daemons)));
  state.nodes.forEach((n) => loadChaos(n.node_id));
}

function chaosCard(n, daemons) {
  const id = n.node_id;
  const latency = h("input", { type: "number", min: 0, max: 60000, step: 100, value: 0, dataset: { f: "latency" } });
  const rate = h("input", { type: "number", min: 0, max: 1, step: 0.1, value: 0, dataset: { f: "rate" } });
  const daemon = h("select", null, daemons.map((d) => h("option", { value: d, text: d })));
  return h("article", { class: `card st-${n.status}`, dataset: { chaos: id } },
    h("div", { class: "card-head" },
      h("div", null, h("span", { class: "name", text: n.node_name }), h("span", { class: "id", text: id })),
      h("span", { dataset: { chaosBadge: id } }, badge(`s-${n.status}`, STATUS_LABEL[n.status] || n.status)),
    ),
    h("div", { class: "chaos-row" }, h("span", { text: "응답 지연" }), latency, h("span", { class: "muted", text: "ms" }),
      h("button", { class: "btn small", onclick: () => postChaos(id, { latency_ms: Number(latency.value) }) }, "적용")),
    h("div", { class: "chaos-row" }, h("span", { text: "오류율" }), rate, h("span", { class: "muted", text: "0~1" }),
      h("button", { class: "btn small", onclick: () => postChaos(id, { error_rate: Number(rate.value) }) }, "적용")),
    h("div", { class: "chaos-row" }, h("span", { text: "blackhole" }),
      h("button", { class: "btn small danger", onclick: () => postChaos(id, { blackhole: true }) }, "켜기"),
      h("button", { class: "btn small", onclick: () => postChaos(id, { blackhole: false }) }, "끄기")),
    h("div", { class: "chaos-row" }, h("span", { text: "데몬 중지" }), daemon,
      h("button", { class: "btn small danger", onclick: () => postChaos(id, { stop_daemon: daemon.value }) }, "중지")),
    h("div", { class: "chaos-row" }, h("span", { text: "" }),
      h("button", { class: "btn small", onclick: () => postChaos(id, { reset: true }) }, "전체 초기화"),
      h("span", { class: "muted", text: "(데몬 상태는 유지)" })),
    h("div", { class: "chaos-state", dataset: { chaosState: id }, text: "조회 중…" }),
  );
}

function renderChaosState(nodeId, c, error) {
  const el = document.querySelector(`[data-chaos-state="${nodeId}"]`);
  if (!el) return;
  if (error) {
    el.className = "chaos-state active";
    el.textContent = `agent 응답 없음: ${error}`;
    return;
  }
  const active = [];
  if (c.latency_ms) active.push(`지연 ${c.latency_ms}ms`);
  if (c.error_rate) active.push(`오류율 ${c.error_rate}`);
  if (c.blackhole) active.push("blackhole ON");
  el.className = `chaos-state${active.length ? " active" : ""}`;
  el.textContent = active.length ? `적용 중: ${active.join(" · ")}` : "장애 주입 없음";
  const card = document.querySelector(`[data-chaos="${nodeId}"]`);
  if (card) {
    const lat = card.querySelector('[data-f="latency"]');
    const rate = card.querySelector('[data-f="rate"]');
    if (lat && document.activeElement !== lat) lat.value = c.latency_ms;
    if (rate && document.activeElement !== rate) rate.value = c.error_rate;
  }
}

async function loadChaos(nodeId) {
  try {
    renderChaosState(nodeId, await api(`/api/nodes/${encodeURIComponent(nodeId)}/chaos`));
  } catch (e) {
    renderChaosState(nodeId, null, e.message);
  }
}

async function postChaos(nodeId, body) {
  try {
    renderChaosState(nodeId, await api(`/api/nodes/${encodeURIComponent(nodeId)}/chaos`, { method: "POST", body }));
  } catch (e) {
    renderChaosState(nodeId, null, e.message);
  }
}

function updateDemoBadges() {
  for (const n of state.nodes) {
    const slot = document.querySelector(`[data-chaos-badge="${n.node_id}"]`);
    if (slot) slot.replaceChildren(badge(`s-${n.status}`, STATUS_LABEL[n.status] || n.status));
    const card = document.querySelector(`[data-chaos="${n.node_id}"]`);
    if (card) card.className = `card st-${n.status}`;
  }
}

// ------------------------------------------------------------------ 시작

function init() {
  $("#select-all").addEventListener("change", (ev) => {
    state.selected = ev.target.checked ? new Set(state.nodes.map((n) => n.node_id)) : new Set();
    renderTargets();
  });
  $("#action-select").addEventListener("change", renderParamForm);
  $("#run-btn").addEventListener("click", onRun);
  $("#history-refresh").addEventListener("click", loadHistory);
  window.addEventListener("hashchange", onRoute);
  window.addEventListener("resize", () => {
    if (currentRoute().tab === "status") renderPanels();
  });
  setInterval(tickAgo, 1000);
  onRoute();
  pollNodes();
}

init();
