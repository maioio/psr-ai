---
title: Hidden Context Layer
description: A browser extension that attaches failed requests, console errors and clicked elements to the steps of a recording.
---

A screenshot shows what the user saw. It does not show the request that failed behind it. The Hidden Context Layer is a browser extension plus a small local listener. While a recording is active, the extension reports browser events to the listener, and `sa stop` attaches each event to the step it happened in.

## What it captures

| Event kind | What is recorded |
|---|---|
| `network_failure` | Requests that failed or returned an error status: method, URL, status, error and, for `fetch` and XHR, the start of the response body. |
| `console_error` | `console.error` and `console.warn` calls, uncaught exceptions and unhandled promise rejections: message, source and stack. |
| `resource_error` | Images, scripts and stylesheets that failed to load: URL and tag. |
| `element_click` | The element you pressed: its tag, role, accessible name, states, a CSS selector, test ID, a few attributes, a trimmed copy of its HTML and its parent chain, with the page URL and title. |

The events end up in `data.json` as `browser_context` on each step. [Output format](/psr-ai/output-format/#optional-fields) lists the fields.

## Set it up

```bash
sa context install
```

This creates the pairing token, then opens a browser with the extension already loaded, in a dedicated PSR.ai profile (`~/.psr_ai/browser_profile/<browser>`). Your everyday browser profile is not touched, and logins you make in the PSR.ai profile are kept between sessions. `sa context browser` opens the same browser again later, and takes `--browser edge|brave|chrome` and `--url`.

### Pair the extension

The extension only sends events when it holds the pairing token. `sa context install` does not paste it for you, so do this once:

1. Print the token:

   ```bash
   sa context token
   ```

2. In the browser PSR.ai opened, open the extension's options page (**PSR.ai context layer**).
3. Paste the token into **Pairing token** and save.

Until the token is set, the listener rejects the extension's requests and nothing is captured. `sa context token --rotate` makes a new token and invalidates the old one; paste the new one into the options page again.

### Manual install

If no browser can be launched, or you use Chrome, `sa context install --no-launch` prints the manual steps:

1. Open `chrome://extensions` (or `edge://extensions`).
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose the extension folder the command prints.
4. Paste the pairing token into the extension's options.

## Browser support

The extension is loaded with the browser's `--load-extension` switch. Not every browser honours it.

| Browser | Status |
|---|---|
| Microsoft Edge | Loads automatically. Tested with Edge 151. |
| Brave | Expected to load automatically. Not tested. |
| Playwright's Chromium | Loads the extension. Tested with Chromium 148. |
| Google Chrome | Ignores the switch (tested with Chrome 151). `sa context browser --browser chrome` refuses to start and points you to Edge or to the [manual install](#manual-install). |
| Firefox, Safari | PSR.ai does not launch them, and they are not tested. |

The browser must run with a visible window. In headless mode the extension does not start.

## Check that it works

```bash
sa context doctor
```

`doctor` checks each part and prints one line per part:

| Line | Possible values |
|---|---|
| Listener | `OK`, `DOWN` (not running), `AUTH` (token mismatch), `ERROR` |
| Extension | `OK`, `STALE` (no check-in for 75 seconds; it normally checks in every 30), `DOWN` (never checked in during this session) |
| Session | `OK` (a recording is active) or `IDLE` |
| Events | How many events were captured, by kind |
| Browsers | Which supported browsers were found. Chrome is marked as manual load only. |

It ends with `Context layer is working.` or with `Not ready` and an arrow at each part to fix.

To prove the whole loop end to end, start a recording and run the self-test:

```bash
sa start
sa context test
```

`sa context test` serves a local page that fails a request and logs a console error, then waits up to 25 seconds (`--timeout`) for three signals: the failed request, its response body and the console error. It prints `[PASS]` or `[FAIL]` for each.

:::caution[The test page opens in your default browser]
`sa context test` opens the test page in your system's default browser. If that is not the browser with the extension, copy the test page URL it prints (shown with `sa --verbose context test`) into the PSR.ai browser, or the checks will fail.
:::

## Read the events

```bash
sa context status
sa context events --kind network_failure --limit 20
sa context events --summary
```

- `sa context status` prints `Context layer: listening on 127.0.0.1:8765 · N events captured`, or `Context layer: not running`.
- `sa context events` prints the events of the live session as JSON. `--kind` filters by kind, `--limit N` keeps the last N, `--summary` prints a count per kind, and `--session-dir` reads a finished session instead.

You normally do not need these: `sa stop` merges the events into `data.json` for you.

## Extension options

| Option | Default | Meaning |
|---|---|---|
| Capture while recording | on | Turn capture off without removing the extension. |
| Listener endpoint | `http://127.0.0.1:8765` | Where events are sent. |
| Pairing token | empty | The token from `sa context token`. |

## Security and limits

- **Local only.** The listener binds to `127.0.0.1:8765`. Events go from the extension to your own machine and nowhere else.
- **Token required.** Every request that sends events needs the pairing token as a Bearer token. A wrong token gets `401` and the extension stops sending until you fix it. The health check is the only route without a token.
- **Origin check.** Requests from web pages are refused with `403`. Only requests with no `Origin`, or from a `chrome-extension://` or `moz-extension://` origin, are accepted.
- **Token file.** The token is stored in `~/.psr_ai/context_token`, readable only by you on macOS and Linux.
- **Only during a recording.** The extension sends nothing while no recording is active, and the listener drops anything it receives then.
- **Redaction.** Before an event leaves the browser, the values of request headers and URL query parameters whose names contain `authorization`, `cookie`, `x-api-key`, `api-key`, `token`, `password`, `passwd`, `secret` or `session-id` are replaced with `[REDACTED]`, and strings shaped like JWTs are masked. The listener applies the same rules again, recursively, before anything is written to disk. Response bodies and console messages are scanned only for JWT-shaped strings, so a page that logs a plain password will have it captured.
- **Size limits.** The extension keeps at most 2,000 events in memory, cuts response bodies to 2,048 characters and URLs to 500, and drops identical events repeated within 2 seconds. The listener accepts at most 500 events per request and 20,000 per session.
- **Timing.** Events are sent about once a second. The first few hundred milliseconds after a page starts loading can be missed.

The development switch `PSR_AI_CONTEXT_OPEN=1` turns off the token check. Use it only for local development of the extension.

See also [Privacy and security](/psr-ai/privacy-and-security/).
