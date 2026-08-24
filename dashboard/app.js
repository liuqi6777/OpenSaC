"use strict";

const MAX_EVENTS = 1000;
const MAX_SAMPLES = 900;
const SAMPLE_WINDOW_MS = 15 * 60 * 1000;
const PHASES = [
  "session_queue",
  "preparing",
  "sandbox_queue",
  "sandbox_running",
  "postprocessing",
];

const state = {
  apiKey: "",
  active: new Map(),
  counters: {},
  events: [],
  samples: [],
  reconnectDelay: 1000,
  streamAbort: null,
  reconnectTimer: null,
  snapshot: null,
};

const elements = {
  authPanel: document.querySelector("#auth-panel"),
  authForm: document.querySelector("#auth-form"),
  apiKey: document.querySelector("#api-key"),
  authError: document.querySelector("#auth-error"),
  streamStatus: document.querySelector("#stream-status"),
  workerLabel: document.querySelector("#worker-label"),
  lastUpdated: document.querySelector("#last-updated"),
  activeCount: document.querySelector("#active-count"),
  activeExecutions: document.querySelector("#active-executions"),
  eventLog: document.querySelector("#event-log"),
  eventFilter: document.querySelector("#event-filter"),
  clearEvents: document.querySelector("#clear-events"),
  chartWorkload: document.querySelector("#chart-workload"),
  chartQueue: document.querySelector("#chart-queue"),
  chartMemory: document.querySelector("#chart-memory"),
};

function headers() {
  const result = { Accept: "application/json" };
  if (state.apiKey) {
    result.Authorization = `Bearer ${state.apiKey}`;
  }
  return result;
}

function setConnection(label, statusClass) {
  elements.streamStatus.textContent = label;
  elements.streamStatus.className = `status-pill ${statusClass}`;
}

function showAuth(message = "") {
  elements.authPanel.hidden = false;
  elements.authError.textContent = message;
  setConnection("Locked", "status-offline");
  window.setTimeout(() => elements.apiKey.focus(), 0);
}

function hideAuth() {
  elements.authPanel.hidden = true;
  elements.authError.textContent = "";
}

async function requestSnapshot() {
  const response = await fetch("/dashboard/api/snapshot", {
    headers: headers(),
    cache: "no-store",
  });
  if (!response.ok) {
    const error = new Error(`Snapshot request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function formatBytes(value) {
  const bytes = Number(value || 0);
  const units = ["B", "KiB", "MiB", "GiB"];
  let size = bytes;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  const precision = index === 0 || size >= 100 ? 0 : size >= 10 ? 1 : 2;
  return `${size.toFixed(precision)} ${units[index]}`;
}

function formatDuration(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value.toFixed(value < 10 ? 1 : 0)}s`;
  const minutes = Math.floor(value / 60);
  const remainder = Math.floor(value % 60);
  if (minutes < 60) return `${minutes}m ${remainder}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function setText(id, text) {
  const element = document.querySelector(`#${id}`);
  if (element) element.textContent = text;
}

function cacheHitRate(cache) {
  const attempts = Number(cache.hits || 0) + Number(cache.misses || 0);
  return attempts ? (Number(cache.hits || 0) / attempts) * 100 : 0;
}

function applySnapshot(snapshot, recordSample = true) {
  state.snapshot = snapshot;
  state.counters = snapshot.counters || {};
  state.active = new Map((snapshot.executions || []).map((item) => [item.task_id, item]));

  const health = snapshot.health || {};
  const process = health.process || {};
  const sessions = health.sessions || {};
  const sandbox = health.sandbox || {};
  const warm = health.warm;
  const broker = health.broker || {};
  const cache = health.provider_cache || {};
  const counters = snapshot.counters || {};
  const queueDepth = Number(sandbox.waiting || 0) + Number(broker.waiting || 0);

  setText("metric-service", health.state || "unknown");
  setText("metric-uptime", `Uptime ${formatDuration(process.uptime_seconds)}`);
  setText("metric-sessions", `${sessions.active || 0}`);
  setText(
    "metric-session-detail",
    `${sessions.executing || 0} executing · ${sessions.capacity || "∞"} capacity`,
  );
  setText("metric-execs", `${health.inflight_execs || 0}`);
  setText(
    "metric-exec-detail",
    `${counters.succeeded || 0} ok · ${counters.failed || 0} failed`,
  );
  setText("metric-sandbox", `${sandbox.active || 0}/${sandbox.capacity || 0}`);
  setText(
    "metric-sandbox-detail",
    warm
      ? `${sandbox.waiting || 0} waiting · ${warm.containers || 0} warm`
      : `${sandbox.waiting || 0} waiting · ${health.sandbox_mode || "cold"}`,
  );
  setText("metric-broker", `${broker.active || 0}/${broker.capacity || 0}`);
  setText("metric-broker-detail", `${broker.waiting || 0} waiting`);
  setText("metric-queue", `${queueDepth}`);
  setText("metric-rss", formatBytes(process.rss_bytes));
  setText("metric-fds", `FDs ${process.fd_count ?? "—"}`);
  setText("metric-cache", cache.enabled ? `${cacheHitRate(cache).toFixed(1)}%` : "Off");
  setText(
    "metric-cache-detail",
    cache.enabled
      ? `${formatBytes(cache.current_bytes)} · ${cache.entries || 0} entries`
      : "Disabled",
  );
  elements.workerLabel.textContent = health.worker_id
    ? `${health.worker_id} · ${health.build?.search_backend || "unknown backend"}`
    : "Waiting for worker";
  elements.lastUpdated.textContent = snapshot.generated_at
    ? `Updated ${new Date(snapshot.generated_at).toLocaleTimeString()}`
    : "Updated now";

  if (recordSample) {
    const now = Date.now();
    state.samples.push({
      at: now,
      workload: Number(health.inflight_execs || 0),
      queue: queueDepth,
      memory: Number(process.rss_bytes || 0),
    });
    state.samples = state.samples
      .filter((sample) => now - sample.at <= SAMPLE_WINDOW_MS)
      .slice(-MAX_SAMPLES);
  }

  renderActive();
  renderCharts();
}

function previewDetails(label, preview) {
  if (!preview || typeof preview.text !== "string") return null;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const suffix = preview.truncated
    ? ` · truncated from ${formatBytes(preview.original_bytes)}`
    : "";
  summary.textContent = `${label}${suffix}`;
  const pre = document.createElement("pre");
  pre.textContent = preview.text;
  details.append(summary, pre);
  return details;
}

function capabilityNode(capability) {
  const row = document.createElement("div");
  const status = capability.status || "unknown";
  row.className = `capability-row capability-${status}`;
  const summary = document.createElement("div");
  summary.className = "capability-summary";
  const method = document.createElement("span");
  method.textContent = `#${capability.sequence} ${capability.method}`;
  const duration = document.createElement("span");
  duration.textContent = status === "running"
    ? "running"
    : formatDuration(capability.duration_seconds);
  summary.append(method, duration);
  row.append(summary);
  for (const [label, preview] of [
    ["Parameters", capability.params],
    ["Queries", capability.queries],
    ["Result", capability.result],
    ["Error", capability.error],
  ]) {
    const details = previewDetails(label, preview);
    if (details) row.append(details);
  }
  return row;
}

function phaseTrack(current) {
  const track = document.createElement("div");
  track.className = "phase-track";
  const activeIndex = PHASES.indexOf(current);
  for (const [index, phase] of PHASES.entries()) {
    const step = document.createElement("span");
    step.className = "phase-step";
    step.title = phase;
    if (index < activeIndex) step.classList.add("phase-step-complete");
    if (index === activeIndex) step.classList.add("phase-step-active");
    track.append(step);
  }
  return track;
}

function renderActive() {
  elements.activeExecutions.replaceChildren();
  const executions = [...state.active.values()];
  elements.activeCount.textContent = `${executions.length} active`;
  if (!executions.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "Waiting for work.";
    elements.activeExecutions.append(empty);
    return;
  }

  executions.sort((left, right) => left.started_at.localeCompare(right.started_at));
  for (const execution of executions) {
    const card = document.createElement("article");
    card.className = "execution-card";
    const header = document.createElement("div");
    header.className = "execution-header";
    const title = document.createElement("div");
    title.className = "execution-title";
    const identifier = document.createElement("strong");
    identifier.textContent = execution.exec_id || execution.task_id;
    const meta = document.createElement("p");
    meta.className = "execution-meta";
    meta.textContent = `${execution.session_id} · ${execution.execution_mode} · ${formatDuration(execution.elapsed_seconds)}`;
    title.append(identifier, meta);
    const phase = document.createElement("span");
    phase.className = "phase-pill";
    phase.textContent = execution.phase.replaceAll("_", " ");
    header.append(title, phase);
    card.append(header, phaseTrack(execution.phase));

    const code = previewDetails("Generated code", execution.code);
    if (code) card.append(code);
    const capabilities = [
      ...(execution.active_capabilities || []),
      ...(execution.capabilities || []).slice(-8),
    ];
    if (capabilities.length) {
      const list = document.createElement("div");
      list.className = "capability-list";
      for (const capability of capabilities) list.append(capabilityNode(capability));
      card.append(list);
    }
    elements.activeExecutions.append(card);
  }
}

function appendEvent(event) {
  state.events.unshift(event);
  state.events = state.events.slice(0, MAX_EVENTS);
  renderEvents();
}

function eventIsError(event) {
  const payload = event.payload || {};
  return event.type === "gap"
    || payload.succeeded === false
    || payload.capability?.status === "error"
    || payload.capability?.status === "cancelled";
}

function eventMatches(event, filter) {
  if (filter === "errors") return eventIsError(event);
  if (filter === "execs") return event.type.startsWith("exec.");
  if (filter === "capabilities") return event.type.startsWith("capability.");
  return true;
}

function eventTitle(event) {
  const payload = event.payload || {};
  if (event.type === "exec.completed") {
    return `${payload.exec_id || payload.task_id} · ${payload.succeeded ? "completed" : "failed"}`;
  }
  if (event.type === "capability.completed") {
    const capability = payload.capability || {};
    return `${capability.method || "capability"} · ${capability.status || "completed"}`;
  }
  if (event.type === "gap") return "Event stream gap · snapshot resync required";
  return event.type;
}

function eventNode(event) {
  const row = document.createElement("article");
  row.className = `event-row${eventIsError(event) ? " event-row-error" : ""}`;
  const header = document.createElement("div");
  header.className = "event-header";
  const kind = document.createElement("span");
  kind.className = "event-kind";
  kind.textContent = event.type;
  const title = document.createElement("span");
  title.className = "event-title";
  title.textContent = eventTitle(event);
  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = new Date(event.at).toLocaleTimeString();
  header.append(kind, title, time);
  row.append(header);

  const payload = event.payload || {};
  const body = document.createElement("div");
  body.className = "event-body";
  if (event.type === "exec.completed") {
    const summary = document.createElement("p");
    summary.textContent = `${formatDuration(payload.elapsed_seconds)} · exit ${payload.exit_code ?? "—"} · ${payload.capabilities?.length || 0} capability calls`;
    body.append(summary);
    for (const [label, preview] of [
      ["Generated code", payload.code],
      ["Standard output", payload.stdout],
      ["Standard error", payload.stderr],
      ["Structured output", payload.output],
      ["Error", payload.error],
      ["Citations", payload.citations],
    ]) {
      const details = previewDetails(label, preview);
      if (details) body.append(details);
    }
  } else if (event.type === "capability.completed") {
    body.append(capabilityNode(payload.capability || {}));
  } else if (event.type === "gap") {
    const message = document.createElement("p");
    message.textContent = "The browser fell behind the live stream. Active state was refreshed; individual events may be missing.";
    body.append(message);
  }
  row.append(body);
  return row;
}

function renderEvents() {
  elements.eventLog.replaceChildren();
  const filter = elements.eventFilter.value;
  const events = state.events.filter((event) => eventMatches(event, filter));
  if (!events.length) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No matching events in this tab.";
    elements.eventLog.append(empty);
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const event of events.slice(0, 200)) fragment.append(eventNode(event));
  elements.eventLog.append(fragment);
}

function updateActiveFromEvent(event) {
  const payload = event.payload || {};
  if (event.type === "exec.started") {
    const execution = payload.execution;
    if (execution) state.active.set(execution.task_id, execution);
  } else if (event.type === "exec.phase") {
    const execution = state.active.get(payload.task_id);
    if (execution) {
      execution.phase = payload.phase;
      execution.elapsed_seconds = payload.elapsed_seconds;
      execution.phase_elapsed_seconds = 0;
    }
  } else if (event.type === "capability.started") {
    const execution = state.active.get(payload.task_id);
    if (execution) {
      execution.active_capabilities ||= [];
      execution.active_capabilities.push(payload.capability);
    }
  } else if (event.type === "capability.completed") {
    const execution = state.active.get(payload.task_id);
    if (execution) {
      execution.active_capabilities = (execution.active_capabilities || []).filter(
        (item) => item.sequence !== payload.capability.sequence,
      );
      execution.capabilities ||= [];
      execution.capabilities.push(payload.capability);
    }
  } else if (event.type === "exec.completed") {
    state.active.delete(payload.task_id);
  }
}

async function handleEvent(event) {
  if (event.type === "snapshot" || event.type === "metrics") {
    applySnapshot(event.payload, true);
    return;
  }
  updateActiveFromEvent(event);
  renderActive();
  if (["exec.completed", "capability.completed", "gap"].includes(event.type)) {
    appendEvent(event);
  }
  if (event.type === "gap") {
    try {
      applySnapshot(await requestSnapshot(), false);
    } catch (_error) {
      // The reconnect loop will recover the snapshot.
    }
  }
}

function parseEventBlock(block) {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  if (!data) return null;
  return JSON.parse(data);
}

async function connectStream() {
  if (state.streamAbort) state.streamAbort.abort();
  const controller = new AbortController();
  state.streamAbort = controller;
  setConnection("Connecting", "status-connecting");
  try {
    const response = await fetch("/dashboard/api/events", {
      headers: { ...headers(), Accept: "text/event-stream" },
      cache: "no-store",
      signal: controller.signal,
    });
    if (response.status === 401) {
      showAuth(state.apiKey ? "The API key was rejected." : "An API key is required.");
      return;
    }
    if (!response.ok || !response.body) {
      throw new Error(`Event stream failed (${response.status})`);
    }
    hideAuth();
    state.reconnectDelay = 1000;
    setConnection("Live", "status-live");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replaceAll("\r\n", "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseEventBlock(block);
        if (event) await handleEvent(event);
        boundary = buffer.indexOf("\n\n");
      }
    }
    throw new Error("Event stream closed");
  } catch (error) {
    if (controller.signal.aborted) return;
    setConnection("Reconnecting", "status-connecting");
    window.clearTimeout(state.reconnectTimer);
    state.reconnectTimer = window.setTimeout(connectStream, state.reconnectDelay);
    state.reconnectDelay = Math.min(state.reconnectDelay * 2, 10000);
  }
}

function drawSeries(canvas, values, color) {
  const context = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * ratio));
  canvas.height = Math.max(1, Math.floor(rect.height * ratio));
  context.scale(ratio, ratio);
  const width = rect.width;
  const height = rect.height;
  context.clearRect(0, 0, width, height);
  context.strokeStyle = "rgba(167, 218, 194, 0.12)";
  context.lineWidth = 1;
  for (let line = 1; line <= 3; line += 1) {
    const y = (height / 4) * line;
    context.beginPath();
    context.moveTo(0, y);
    context.lineTo(width, y);
    context.stroke();
  }
  if (!values.length) return;
  const maximum = Math.max(...values, 1);
  const denominator = Math.max(values.length - 1, 1);
  context.beginPath();
  values.forEach((value, index) => {
    const x = (index / denominator) * width;
    const y = height - 7 - (Number(value || 0) / maximum) * (height - 16);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.strokeStyle = color;
  context.lineWidth = 2;
  context.lineJoin = "round";
  context.lineCap = "round";
  context.stroke();
}

function renderCharts() {
  drawSeries(elements.chartWorkload, state.samples.map((sample) => sample.workload), "#72f1b8");
  drawSeries(elements.chartQueue, state.samples.map((sample) => sample.queue), "#68d8e8");
  drawSeries(elements.chartMemory, state.samples.map((sample) => sample.memory), "#f4ca6d");
  const latest = state.samples.at(-1) || { workload: 0, queue: 0, memory: 0 };
  setText("chart-workload-value", `${latest.workload}`);
  setText("chart-queue-value", `${latest.queue}`);
  setText("chart-memory-value", formatBytes(latest.memory));
}

async function bootstrap() {
  try {
    const snapshot = await requestSnapshot();
    hideAuth();
    applySnapshot(snapshot, true);
    await connectStream();
  } catch (error) {
    if (error.status === 401) {
      showAuth(state.apiKey ? "The API key was rejected." : "An API key is required.");
    } else {
      setConnection("Reconnecting", "status-connecting");
      state.reconnectTimer = window.setTimeout(bootstrap, state.reconnectDelay);
      state.reconnectDelay = Math.min(state.reconnectDelay * 2, 10000);
    }
  }
}

elements.authForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  state.apiKey = elements.apiKey.value;
  elements.apiKey.value = "";
  elements.authError.textContent = "";
  await bootstrap();
});

elements.eventFilter.addEventListener("change", renderEvents);
elements.clearEvents.addEventListener("click", () => {
  state.events = [];
  renderEvents();
});

window.addEventListener("resize", renderCharts);
window.addEventListener("beforeunload", () => state.streamAbort?.abort());

bootstrap();
