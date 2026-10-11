/** Options/popup logic: pair with the local listener and show live status. */

const DEFAULTS = { endpoint: "http://127.0.0.1:8765", token: "", enabled: true };

const $ = (id) => document.getElementById(id);

async function load() {
  const settings = await chrome.storage.local.get(DEFAULTS);
  $("endpoint").value = settings.endpoint;
  $("token").value = settings.token;
  $("enabled").checked = settings.enabled;
  test();
}

async function save() {
  await chrome.storage.local.set({
    endpoint: $("endpoint").value.trim().replace(/\/+$/, "") || DEFAULTS.endpoint,
    token: $("token").value.trim(),
    enabled: $("enabled").checked,
  });
  // Force the service worker to re-probe with the new settings.
  await chrome.storage.session.remove("health");
  setStatus("Saved.");
  test();
}

function setStatus(text) {
  $("status").textContent = text;
}

async function test() {
  const settings = await chrome.storage.local.get(DEFAULTS);
  if (!settings.enabled) {
    setStatus("Capture is off.");
    return;
  }
  setStatus("Checking…");
  try {
    const headers = settings.token ? { Authorization: `Bearer ${settings.token}` } : {};
    const response = await fetch(`${settings.endpoint}/v1/health`, { headers, cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      setStatus("Listener reachable, token rejected.");
      return;
    }
    if (!response.ok) {
      setStatus(`Listener returned HTTP ${response.status}.`);
      return;
    }
    const body = await response.json();
    setStatus(
      body.recording
        ? `Recording: ${body.session_id || "active"} · ${body.events_received ?? 0} events`
        : "Listener up. No active recording (run: sa start).",
    );
  } catch {
    setStatus("Listener not running. Start one with: sa start");
  }
}

$("save").addEventListener("click", save);
$("test").addEventListener("click", test);
load();
