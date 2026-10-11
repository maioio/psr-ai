---
title: Limitations
description: Known issues and gaps in the current version of PSR.ai, with the workaround for each one.
---

This page lists what does not work yet in the current version, or works differently from what you might expect. Each item says how to work around it. [Troubleshooting](/psr-ai/troubleshooting/) has step-by-step fixes.

## Installation

- **Not on PyPI yet.** `pip install psr-ai` does not work. Install from a clone of the repository, as in [Getting started](/psr-ai/getting-started/#install). Some help texts and error messages still say `pip install psr-ai[capture]`; use `pip install -e ".[capture]"` instead.
- **The `analyze` extra is not enough on its own** for `sa video-analyze`. It also needs the `ai` extra, for the Anthropic client.

## Recording

- **macOS and Linux: `sa start` and `sa stop` do not work together.** The capture runs inside the `sa start` process and stops when it exits, so `sa stop` finds nothing. The MCP tools `psr_start_recording` and `psr_stop_and_parse` have the same problem. Record on Windows for now. [Details](/psr-ai/platforms/#macos-and-linux-recording-from-the-cli).
- **No bundled Steps Recorder.** The repository ships a shortcut to `System32\psr.exe`, not a copy of it. When Windows has no `psr.exe`, the polling recorder is used.
- **The engine is hard to see.** The switch to the polling recorder is silent, `sa --verbose start` can still name `WindowsRecorder`, and `sa status` says `psr.exe is running` for either engine. Look for `~/psr_output/.polling_session.json` instead. [Details](/psr-ai/platforms/#polling-recorder).
- **Unsupported systems get a traceback.** On a system other than Windows, macOS or Linux, `sa start` ends with a Python traceback rather than a short message.

## sa stop

- **Element HTML is fetched without asking.** When Playwright is installed, `sa stop` opens every browser URL it finds in the recording in a headless Chromium. There is no option to turn this off; uninstall Playwright from that environment to skip it. [Details](/psr-ai/privacy-and-security/#things-that-act-without-asking).
- **The recovery hint after a parse failure is wrong.** It suggests `sa pub` on the ZIP, but `sa pub` only takes a folder. [What to do instead](/psr-ai/troubleshooting/#sa-stop-says-auto-parse-failed).

## Hidden Context Layer

- **Google Chrome needs a manual install.** Chrome ignores the switch that loads the extension. Microsoft Edge and Playwright's Chromium load it automatically. [Details](/psr-ai/hidden-context-layer/#browser-support).
- **Brave is not tested.** It is expected to load the extension automatically.
- **`sa context install` does not pair the extension.** Paste the token from `sa context token` into the extension's options once. [Details](/psr-ai/hidden-context-layer/#pair-the-extension).
- **`sa context test` uses your default browser.** If that is not the browser with the extension, open the test page URL in the right browser yourself.

## Agents and tests

- **Dry runs still send screenshots.** `sa agent`, `sa replay` and `sa watch` send a full-screen screenshot to the model provider at every step, also in a dry run. [Details](/psr-ai/replay-and-tests/#before-you-let-it-act).
- **`sa replay` needs a ZIP with `data.json`.** No recorder writes one. Zip the output folder of `sa stop` yourself. Step 1 is planned from the recorded screenshot, not from your live screen. [Details](/psr-ai/replay-and-tests/#sa-replay).
- **`sa do` cannot run the command it picks** with Click 8.2 or later. Use `--dry-run` and run the printed command yourself. [Details](/psr-ai/replay-and-tests/#sa-do).
- **`sa testgen` writes a first draft.** Screen coordinates from a desktop recording rarely match a browser page, and steps it cannot map become placeholders. Review the file before you rely on it. [Details](/psr-ai/replay-and-tests/#sa-testgen).
- **`psr_fetch_link` can read pages as you.** Its fallback on Windows and macOS first tries your own Chrome or Edge profile. [Details](/psr-ai/mcp/#psr_fetch_link-and-your-browser-profile).

## Publishing

- **`sa pipeline` always pushes** inside a Git repository, with no dry run. Use `sa stop` and `sa pub` to look first. [Details](/psr-ai/ai-analysis/#sa-pipeline).
- **Pull requests target `main` by default.** If your default branch has another name, pass `--pr-base`.

## Media

- **The Instagram session file is not locked down.** `~/.psr_instagram_session.json` is written with your default file permissions. [Details](/psr-ai/privacy-and-security/#local-files-with-credentials).

## Not available

These do not exist in this version:

- A hosted service, account or paid plan. Everything runs on your machine.
- A security policy or contribution guide in the repository. [Contributing](/psr-ai/contributing/) covers the basics.
