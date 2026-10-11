# Hidden Context Layer - Architecture

Feed Claude the backend browser context (failed requests, console errors, the
accessibility shape of what was clicked) **without an F12 window ever appearing
on screen**, and enrich the final `data.json` with OS-level metadata.

---

## 1. The constraint that shapes everything

Every "give the AI browser context" tool on the market gets its data one of two
ways, and both of them are visible:

| Approach | Why it is visible |
|---|---|
| DevTools panel extension (`chrome.devtools.*`) | The panel APIs only exist **while DevTools is open**. Close F12 and capture stops. |
| `chrome.debugger` / CDP (`Network.getResponseBody`, `Accessibility.getFullAXTree`) | Attaching raises the yellow **"… is debugging this browser"** infobar and blocks real DevTools use. |

The Hidden Context Layer uses **neither**. It stays on APIs that work with the
browser looking completely normal:

- `chrome.webRequest` - **observational** events (`onCompleted`, `onErrorOccurred`)
  are unchanged in MV3; only *blocking* was removed. No infobar, no panel.
- A `world: "MAIN"` content script - patches `console.error`, `fetch`, and
  `XMLHttpRequest` inside the page to recover what `webRequest` cannot see
  (response bodies, uncaught exception stacks).
- A plain content script - computes the accessibility snapshot of the clicked
  element from the DOM, so no `chrome.debugger` and no `chrome.automation`
  (which is ChromeOS-only anyway).

**Cost of this choice:** no response bodies for requests the page did not make
through `fetch`/`XHR` (e.g. `<img>`, `<script>`, navigations). We still record
their method, URL, status, type, and timing from `webRequest` - just not the body.
That is the right trade: bodies of *API* calls are the ones worth having, and
those all go through `fetch`/`XHR`.

---

## 2. Data flow

```
                    ┌──────────────────────── Chrome tab ────────────────────────┐
                    │                                                            │
   MAIN world  ───► │  inject.js      console.error / window.onerror /            │
   (page JS)        │                 unhandledrejection / fetch / XHR wrappers   │
                    │        │ window.postMessage                                 │
   ISOLATED    ───► │  content.js     click → a11y snapshot (role/name/states),    │
   world            │                 selector path, ancestor chain, page URL      │
                    └────────┬───────────────────────────────────────────────────┘
                             │ chrome.runtime.sendMessage
                             ▼
        ┌─────────────────── background.js (MV3 service worker) ──────────────────┐
        │  chrome.webRequest.onCompleted     → status >= 400                      │
        │  chrome.webRequest.onErrorOccurred → net::ERR_*                         │
        │  ring buffer (chrome.storage.session, survives worker termination)      │
        │  dedupe + redact + debounce ~1s                                         │
        └────────┬────────────────────────────────────────────────────────────────┘
                 │  POST http://127.0.0.1:8765/v1/ingest   (Bearer token)
                 ▼
        ┌─────────────────── psr-ai context listener (Python, stdlib) ────────────┐
        │  ThreadingHTTPServer bound to 127.0.0.1 only                            │
        │  /v1/health  → { recording, session_id }                                │
        │  /v1/ingest  → appends JSONL to <session>/context.jsonl                 │
        │  drops everything when no recording session is active                   │
        └────────┬────────────────────────────────────────────────────────────────┘
                 │  sa stop
                 ▼
        ┌─────────────────── merge into data.json ────────────────────────────────┐
        │  events correlated to the nearest preceding step by timestamp           │
        │  step["browser_context"] = { network_failures, console_errors, element } │
        │  data["os_context"]      = { active_window, clipboard? }                 │
        └─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Why HTTP POST and not a WebSocket

`browser-tools-mcp` (the closest prior art) keeps a WebSocket open, because since
Chrome 116 WebSocket traffic resets the service worker's 30-second idle timer.
That works, but it means writing a WebSocket server - frame parsing and all - in
the Python core, and it fights the MV3 lifecycle instead of using it.

The listener is **event-driven instead**. An MV3 worker is woken by the very
events we care about (`webRequest`, `runtime.onMessage`), so:

1. an event wakes the worker,
2. the worker appends to a buffer in `chrome.storage.session`,
3. a ~1s debounce flushes the buffer with one `fetch()` POST,
4. the worker is free to die - the next event wakes it again, and the buffer
   survived in session storage.

A 25-second `chrome.alarms` keepalive only refreshes the cached "is a session
recording?" flag. Nothing is lost if the worker is torn down mid-session, and the
Python side stays pure stdlib.

**Privacy gate:** while no recording is active, the extension sends *nothing* -
it only learns the state from the cheap `/v1/health` probe. Payloads flow only
between `sa start` and `sa stop`.

---

## 4. Security model

| Risk | Mitigation |
|---|---|
| Any local process could POST fake context | Bearer token, generated once into `~/.psr_ai/context_token` (0600), pasted into the extension options page. |
| Any web page could reach the listener | Bound to `127.0.0.1`; `Origin` must be a `chrome-extension://` scheme; CORS is not granted to web origins. |
| Secrets in captured payloads | `redact()` scrubs `authorization`, `cookie`, `set-cookie`, `token`, `api[-_]key`, `password`, `secret`, and JWT-shaped strings before anything is written. |
| Unbounded disk/memory | Ring buffer capped at 2000 events / 4 MB per session; bodies truncated to 2 KB; URLs to 500 chars. |
| Clipboard is extremely sensitive | **Opt-in only** (`sa stop --clipboard`), redacted, and truncated. Never captured by default. |

---

## 5. Open-source prior art considered

| Project | License | Verdict |
|---|---|---|
| [AgentDeskAI/browser-tools-mcp](https://github.com/AgentDeskAI/browser-tools-mcp) | MIT | Closest architecture (extension → `127.0.0.1:3025` aggregator → MCP). Node/TS, and its capture path relies on a **DevTools panel** - exactly what we must avoid. Pattern adopted, code not vendored. |
| [ChromeDevTools/chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Apache-2.0 | Official, excellent a11y snapshots - but drives CDP over a debug-flagged Chrome. Visible and heavyweight. Not usable for a hidden layer. |
| [network-capture-pro-chrome-extension](https://github.com/connornominative2175/network-capture-pro-chrome-extension) | see repo | Useful reference for MV3 network capture with bodies. Export-to-file oriented, no local agent channel. |
| [float-console](https://github.com/milad-hub/float-console) | see repo | Proves console capture without DevTools; renders an on-screen overlay, which we explicitly do not want. |

Net: **no MIT-compatible component was worth vendoring**; the whole extension is
~450 lines of first-party code. What we *did* take from prior art: the
localhost-aggregator topology, the 4xx/5xx + console split, and the MV3
service-worker lifetime facts from
[Chrome's own docs](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).

---

## 6. Files

```
extension/
  manifest.json     MV3 manifest - webRequest, storage, alarms, <all_urls>
  background.js     service worker: webRequest capture, buffer, flush, keepalive
  content.js        ISOLATED world: click a11y snapshot, error relay
  inject.js         MAIN world: console/fetch/XHR patches
  options.html/.js  paste the pairing token, toggle capture

src/psr_ai/core/context_server.py   local listener + merge into data.json
src/psr_ai/core/os_context.py       active window title + clipboard (opt-in)
```

CLI surface:

```bash
sa context install    # opens a browser with the layer already loaded
sa context browser    # same, with --browser / --url control
sa context status     # is the listener up? how many events?
sa context events     # dump captured context as JSON (for any non-MCP tool)
sa context token      # pairing token for manual installs
sa context serve      # run the listener in the foreground (normally automatic)

sa start              # starts the listener automatically (--no-context to skip)
sa stop               # merges context.jsonl into data.json
sa stop --clipboard   # additionally capture clipboard text into os_context
```

MCP surface (what Claude Code and any other MCP host see):

| Tool | Purpose |
|---|---|
| `psr_open_context_browser` | Open an instrumented browser |
| `psr_get_browser_context` | Read captured failures/errors/clicked elements |
| `psr_context_status` | Is the layer live? how many events? which browsers? |

---

## 7. Browser support - measured, not assumed

`--load-extension` is the mechanism that lets PSR.ai load the layer with no
store and no manual clicking. It is being withdrawn across Chromium. Measured
on Windows, 2026-08:

| Browser | Version | `--load-extension` | Notes |
|---|---|---|---|
| Microsoft Edge | 151 | **works** | the default choice |
| Playwright's bundled Chromium | 148 | **works** | available with `psr-ai[capture]` |
| Google Chrome | 151 | **silently ignored** | accepts the flag, starts without the extension, prints no error |
| Any browser, headless | - | **never loads extensions** | zero service workers register under `--headless=new` |

For Chrome, `--enable-automation`, `--test-type`,
`--disable-extensions-except`, and `--enable-unsafe-extension-debugging` were
all tested; none restore the behaviour. This is deliberate hardening against
malware side-loading, not a bug to work around.

Consequences, all handled in code:

* `find_browsers()` sorts auto-load-capable browsers first, so Edge wins over
  Chrome by default.
* `launch()` raises `UnsupportedBrowserError` rather than opening a Chrome
  window that would silently capture nothing. Pass `allow_unsupported=True`
  to override.
* Chrome users get the one-time manual path from `sa context install --no-launch`
  (chrome://extensions -> Developer mode -> Load unpacked). That still works
  and is free; it is a user action, not a command-line flag.

### Known limitation

Events fired in the first few hundred milliseconds of the **very first page
load while the browser itself is still starting** can be missed - the extension
is still initialising. Reloading the page captures everything. Once the browser
is up, no events are lost: `content.js` retries any message that races the
service worker's cold start (250ms / 1s / 3s).
