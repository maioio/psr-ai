---
title: Recording
description: Start, check and stop a recording, and what sa stop does with it.
---

A recording is a ZIP of screenshots plus step metadata. `sa start` begins one, `sa stop` ends it and turns it into a report.

## sa start

```bash
sa start
sa start -o ~/sessions/checkout-bug.zip --maxsc 50
sa start --low-token
```

| Option | Default | What it does |
|---|---|---|
| `-o`, `--output PATH` | `~/psr_output/psr_<YYYYMMDD_HHMMSS>.zip` | Where the ZIP is written. |
| `--maxsc N` | `100` | Maximum screenshots. When the cap is reached the session ends cleanly and the ZIP is written. |
| `--low-token` | off | Downscale screenshots to 512px on the longest side, to cut vision token costs later. |
| `--context` / `--no-context` | `--context` | Run the [Hidden Context Layer](/psr-ai/hidden-context-layer/) listener so the browser extension can attach events to steps. |

`sa start` returns at once and prints only the output path. On Windows the recording runs in a separate process, so it keeps going after the terminal closes. Add `--verbose` before the command name (`sa --verbose start`) to see more detail. [Platforms](/psr-ai/platforms/#polling-recorder) explains how to tell which Windows engine is running.

:::caution[macOS and Linux]
On macOS and Linux the capture thread runs inside the `sa start` process, which exits as soon as it prints the path. A later `sa stop` therefore finds no recording. Until this is fixed, recording from the command line works reliably on Windows only. [Platforms](/psr-ai/platforms/#macos-and-linux-recording-from-the-cli) has the details.
:::

### How steps are captured

The engine is chosen for you:

- **Windows:** the built-in Steps Recorder, `psr.exe`, when Windows allows it. If `psr.exe` is missing, blocked by Group Policy or refuses to launch, PSR.ai switches to a detached polling recorder without asking.
- **Polling recorder (Windows fallback):** takes a frame every 3 seconds and an extra frame on each mouse click when `pynput` is installed (the `record` or `desktop` extra). Frames identical to the previous one are discarded before they are written, so an idle screen costs nothing.
- **macOS and Linux:** with `pynput` installed (the `record` or `desktop` extra), a frame is taken on each mouse click. Without it, the frontmost application is checked every 3 seconds and a frame is taken only when it changed. Frames that look the same as the previous one are discarded.

[Platforms](/psr-ai/platforms/) has the details for each operating system.

## sa status

```bash
sa status
```

Prints `Status: RECORDING` or `Status: IDLE`.

## sa stop

```bash
sa stop
sa stop --no-gif
sa stop --clipboard
```

| Option | What it does |
|---|---|
| `--no-gif` | Skip the animated replay. Only `report.md`, `data.json` and the screenshots are written. |
| `--clipboard` | Also store the current clipboard text in `data.json`. Off by default because clipboards often hold credentials. The text is redacted and cut to 2,000 characters. |

`sa stop` waits for the ZIP to be complete, then:

1. Parses it into `report.md`, `data.json` and `screenshots/step_NNN.png` in a folder named `<zip name>_output` next to the ZIP.
2. Renders `action_replay.gif`, one frame per step, 1.5 seconds each. This needs Pillow (`pip install -e ".[gif]"`); without it the GIF is skipped with a note.
3. Merges browser events from the Hidden Context Layer into the steps they belong to, when the extension sent any. If it never checked in, `sa stop` says so and points to `sa context doctor`.
4. Records the active window title in `data.json`, and the clipboard when you passed `--clipboard`.
5. **Fetches element HTML (best effort).** If Playwright is installed, PSR.ai looks for browser URLs in the recorded window titles and step text, opens each one again in a headless Chromium, and stores the HTML of the clicked element as `element_html` on the step. This makes a network request to those URLs from your machine. Without Playwright the step is skipped.
6. Suggests `sa video-analyze` if a video was playing in one of the recorded windows.

The only line printed is the step count and the report path. If parsing fails, the ZIP is still saved and `sa stop` prints its path. It also suggests running `sa pub` on the ZIP, which does not work, because `sa pub` takes an output folder. [Troubleshooting](/psr-ai/troubleshooting/#sa-stop-says-auto-parse-failed) explains how to parse the ZIP again.

## After recording

- [Output format](/psr-ai/output-format/) explains `report.md` and `data.json`.
- [`sa review`](/psr-ai/ai-analysis/#review-and-drop-steps) removes steps you do not want to share.
- [`sa analyze`](/psr-ai/ai-analysis/) adds an AI description to each step.
