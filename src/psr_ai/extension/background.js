/**
 * PSR.ai Hidden Context Layer - MV3 service worker.
 *
 * Collects three classes of "backend" browser context and ships them to the
 * local psr-ai listener while a recording session is active:
 *
 *   1. Failed network requests  - chrome.webRequest observational events
 *                                 (4xx/5xx) plus net::ERR_* failures.
 *   2. Uncaught console errors  - relayed up from inject.js in the MAIN world.
 *   3. Clicked element context  - accessibility snapshot from content.js.
 *
 * Design notes for MV3:
 *   - Every listener is registered at the TOP LEVEL. A service worker is torn
 *     down after ~30s idle and restarted by the next event; listeners attached
 *     inside an async callback would be missed on that restart.
 *   - The pending buffer lives in chrome.storage.session, so a teardown between
 *     "event captured" and "batch flushed" loses nothing.
 *   - Nothing is transmitted unless the listener reports an active recording.
 */

const ENDPOINT_DEFAULT = "http://127.0.0.1:8765";
const FLUSH_DEBOUNCE_MS = 1000;
const KEEPALIVE_PERIOD_MINUTES = 0.5;   // 30s - Chrome's minimum alarm period
const HEALTH_TTL_MS = 30000;            // re-probe the listener this often
const MAX_BUFFERED_EVENTS = 2000;       // ring buffer ceiling
const MAX_BODY_CHARS = 2048;
const MAX_URL_CHARS = 500;
const DEDUPE_WINDOW_MS = 2000;

// Header/field names whose values must never leave the machine in cleartext.
const SECRET_KEY_RE = /(authorization|cookie|set-cookie|x-api-key|api[-_]?key|token|password|passwd|secret|session[-_]?id)/i;
// A JWT-shaped string anywhere inside a value.
const JWT_RE = /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g;

// ---------------------------------------------------------------------------
// Settings + cached listener state
// ---------------------------------------------------------------------------

async function getSettings() {
  const stored = await chrome.storage.local.get({
    endpoint: ENDPOINT_DEFAULT,
    token: "",
    enabled: true,
  });
  return stored;
}

async function getHealth(force = false) {
  const { health } = await chrome.storage.session.get({ health: null });
  if (!force && health && Date.now() - health.checkedAt < HEALTH_TTL_MS) {
    return health;
  }
  const settings = await getSettings();
  const fresh = { recording: false, sessionId: null, checkedAt: Date.now() };

  if (settings.enabled) {
    try {
      const response = await fetch(`${settings.endpoint}/v1/health`, {
        method: "GET",
        headers: authHeaders(settings.token),
        cache: "no-store",
      });
      if (response.ok) {
        const body = await response.json();
        fresh.recording = Boolean(body.recording);
        fresh.sessionId = body.session_id || null;
      }
    } catch (err) {
      // Listener not running: stay silent, this is the normal idle state.
    }
  }

  await chrome.storage.session.set({ health: fresh });
  return fresh;
}

function authHeaders(token) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

/** Identify this browser to the listener, for `sa context doctor`. */
function clientInfo() {
  const agent = navigator.userAgent || "";
  const match = agent.match(/(Edg|Chrome|Brave)\/([\d.]+)/);
  return {
    browser: match ? (match[1] === "Edg" ? "Edge" : match[1]) : "Unknown",
    version: match ? match[2] : "",
    extension_version: chrome.runtime.getManifest().version,
  };
}

/**
 * Tell the listener the layer is alive.
 *
 * Without this, a quiet page is indistinguishable from a switched-off
 * extension: both produce zero events. Chrome disables developer-mode
 * extensions after browser updates, so "switched off" is a state users will
 * genuinely hit - and it must be visible rather than silent.
 */
async function sayHello() {
  const settings = await getSettings();
  if (!settings.enabled) return;
  try {
    await fetch(`${settings.endpoint}/v1/hello`, {
      method: "POST",
      headers: authHeaders(settings.token),
      body: JSON.stringify({ client: clientInfo(), sent_at: new Date().toISOString() }),
      cache: "no-store",
    });
  } catch {
    // Listener not running - the normal idle state, nothing to report.
  }
}

// ---------------------------------------------------------------------------
// Redaction + normalisation
// ---------------------------------------------------------------------------

function truncate(value, limit) {
  if (typeof value !== "string") return value;
  return value.length > limit ? `${value.slice(0, limit)}…[+${value.length - limit}]` : value;
}

function scrubText(value) {
  if (typeof value !== "string") return value;
  return value.replace(JWT_RE, "[REDACTED_JWT]");
}

/** Strip secret-bearing query parameters without destroying the URL shape. */
function scrubUrl(rawUrl) {
  if (typeof rawUrl !== "string") return "";
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return truncate(scrubText(rawUrl), MAX_URL_CHARS);
  }
  for (const key of [...url.searchParams.keys()]) {
    if (SECRET_KEY_RE.test(key)) url.searchParams.set(key, "[REDACTED]");
  }
  return truncate(scrubText(url.toString()), MAX_URL_CHARS);
}

function scrubHeaders(headers) {
  if (!Array.isArray(headers)) return [];
  return headers
    .filter((header) => header && header.name)
    .map((header) => ({
      name: header.name,
      value: SECRET_KEY_RE.test(header.name)
        ? "[REDACTED]"
        : truncate(scrubText(header.value ?? ""), 200),
    }));
}

// ---------------------------------------------------------------------------
// Buffer
// ---------------------------------------------------------------------------

async function enqueue(event) {
  const health = await getHealth();
  if (!health.recording) return;   // Privacy gate: no session, no transmission.

  const { pending, seen } = await chrome.storage.session.get({ pending: [], seen: {} });

  // Collapse the duplicate every failed fetch produces: once from webRequest,
  // once from the MAIN-world wrapper. The wrapper's copy wins because it
  // carries the response body.
  const key = event.dedupe_key;
  const now = Date.now();
  if (key) {
    const previous = seen[key];
    if (previous && now - previous.at < DEDUPE_WINDOW_MS) {
      if (event.kind !== "network_failure" || !event.response_body) return;
      const index = pending.findIndex((item) => item.dedupe_key === key);
      if (index >= 0) pending.splice(index, 1);
    }
    seen[key] = { at: now };
    for (const [oldKey, meta] of Object.entries(seen)) {
      if (now - meta.at > DEDUPE_WINDOW_MS * 5) delete seen[oldKey];
    }
  }

  pending.push(event);
  if (pending.length > MAX_BUFFERED_EVENTS) {
    pending.splice(0, pending.length - MAX_BUFFERED_EVENTS);
  }

  await chrome.storage.session.set({ pending, seen });
  scheduleFlush();
}

let flushTimer = null;

function scheduleFlush() {
  if (flushTimer !== null) return;
  flushTimer = setTimeout(() => {
    flushTimer = null;
    flush().catch(() => {});
  }, FLUSH_DEBOUNCE_MS);
}

async function flush() {
  const { pending } = await chrome.storage.session.get({ pending: [] });
  if (pending.length === 0) return;

  const settings = await getSettings();
  if (!settings.enabled) {
    await chrome.storage.session.set({ pending: [] });
    return;
  }

  // Clear before the request: a failed POST must not replay stale context into
  // a later session, and the browser is the wrong place to retry indefinitely.
  await chrome.storage.session.set({ pending: [] });

  try {
    const response = await fetch(`${settings.endpoint}/v1/ingest`, {
      method: "POST",
      headers: authHeaders(settings.token),
      body: JSON.stringify({ events: pending, sent_at: new Date().toISOString() }),
      cache: "no-store",
    });
    if (response.ok) {
      const body = await response.json().catch(() => ({}));
      await chrome.storage.session.set({
        health: {
          recording: Boolean(body.recording),
          sessionId: body.session_id || null,
          checkedAt: Date.now(),
        },
      });
    } else if (response.status === 401 || response.status === 403) {
      // Bad or missing pairing token - stop trying until the user fixes it.
      await chrome.storage.session.set({
        health: { recording: false, sessionId: null, checkedAt: Date.now() },
      });
    }
  } catch (err) {
    // Listener went away mid-session. Dropping is correct: the recording is
    // over or psr-ai crashed, and buffering forever would leak memory.
  }
}

// ---------------------------------------------------------------------------
// 1. Failed network requests (observational webRequest - no debugger, no panel)
// ---------------------------------------------------------------------------

chrome.webRequest.onCompleted.addListener(
  (details) => {
    if (details.statusCode < 400) return;
    enqueue({
      kind: "network_failure",
      source: "webRequest",
      dedupe_key: `${details.method} ${details.url} ${details.statusCode}`,
      timestamp: new Date().toISOString(),
      tab_id: details.tabId,
      url: scrubUrl(details.url),
      method: details.method,
      status: details.statusCode,
      resource_type: details.type,
      from_cache: details.fromCache === true,
      response_headers: scrubHeaders(details.responseHeaders),
    });
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"],
);

chrome.webRequest.onErrorOccurred.addListener(
  (details) => {
    enqueue({
      kind: "network_failure",
      source: "webRequest",
      dedupe_key: `${details.method} ${details.url} ${details.error}`,
      timestamp: new Date().toISOString(),
      tab_id: details.tabId,
      url: scrubUrl(details.url),
      method: details.method,
      status: 0,
      resource_type: details.type,
      error: details.error,           // e.g. net::ERR_CONNECTION_REFUSED
    });
  },
  { urls: ["<all_urls>"] },
);

// ---------------------------------------------------------------------------
// 2 + 3. Console errors and clicked-element context, relayed by content.js
// ---------------------------------------------------------------------------

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.channel !== "psr-ai") {
    return false;
  }

  const event = {
    ...message.payload,
    timestamp: message.payload?.timestamp || new Date().toISOString(),
    tab_id: sender.tab?.id ?? -1,
    page_url: scrubUrl(sender.tab?.url || message.payload?.page_url || ""),
    frame_url: scrubUrl(sender.url || ""),
  };

  if (typeof event.message === "string") event.message = truncate(scrubText(event.message), MAX_BODY_CHARS);
  if (typeof event.stack === "string") event.stack = truncate(scrubText(event.stack), MAX_BODY_CHARS);
  if (typeof event.response_body === "string") {
    event.response_body = truncate(scrubText(event.response_body), MAX_BODY_CHARS);
  }
  if (typeof event.url === "string") event.url = scrubUrl(event.url);

  enqueue(event);

  // Tell the page whether it is worth computing further context.
  getHealth().then((health) => sendResponse({ recording: health.recording }));
  return true;   // async sendResponse
});

// ---------------------------------------------------------------------------
// Keepalive: refresh the recording flag and drain anything left behind by a
// service-worker teardown that happened mid-debounce.
// ---------------------------------------------------------------------------

chrome.alarms.create("psr-ai-keepalive", { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== "psr-ai-keepalive") return;
  getHealth(true)
    .then((health) => Promise.all([flush(), health.recording ? sayHello() : null]))
    .catch(() => {});
});

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create("psr-ai-keepalive", { periodInMinutes: KEEPALIVE_PERIOD_MINUTES });
  announce();
});

chrome.runtime.onStartup.addListener(announce);

/** Probe the listener and check in, on every service-worker cold start. */
function announce() {
  getHealth(true)
    .then((health) => (health.recording ? sayHello() : null))
    .catch(() => {});
}

// The worker is also started by events (webRequest, onMessage) rather than by
// onInstalled/onStartup, so announce at module scope too. This runs on every
// cold start, which is exactly when the listener needs to hear from us.
announce();
