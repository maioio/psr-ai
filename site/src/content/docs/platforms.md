---
title: Platforms
description: Which recording engine PSR.ai uses on Windows, macOS and Linux, what each one needs, and the current limits of each.
---

PSR.ai needs Python 3.10 or newer. It picks a recording engine for your operating system when you run `sa start`. On any system other than Windows, macOS or Linux, `sa start` stops with an `Unsupported platform` error.

| System | Engine | Status |
|---|---|---|
| Windows | Steps Recorder (`psr.exe`), or a polling recorder when that cannot run | Supported |
| macOS | `screencapture` and `osascript` | Recording from the CLI does not work yet. [See below](#macos-and-linux-recording-from-the-cli). |
| Linux | `gnome-screenshot`, `scrot` or `maim` | Recording from the CLI does not work yet. [See below](#macos-and-linux-recording-from-the-cli). |

## Windows

### Steps Recorder

PSR.ai first tries the Steps Recorder that ships with Windows. It looks for `psr.exe` in this order:

1. Your `PATH`.
2. `%SystemRoot%\System32\psr.exe`.
3. `%WinDir%\System32\psr.exe`.
4. `assets/psr_fallback/` in the cloned repo: a `psr.exe` you copied there yourself, then the shortcut that comes with the repo. The shortcut points to the same `System32` copy, so it does not help when Windows has removed `psr.exe`.

If `psr.exe` asks for elevation, PSR.ai launches it through the Windows shell so the UAC prompt can appear. The running session is tracked in `~/psr_output/.current_session`.

### Polling recorder

When `psr.exe` is missing, blocked by Group Policy or refuses to start, PSR.ai switches to its own recorder without asking. To see which one is running, look for `~/psr_output/.polling_session.json` during a recording: only the polling recorder writes it. The `Engine:` line of `sa --verbose start` is not enough, because it still says `WindowsRecorder` when `psr.exe` was found but refused to start.

- It runs as a separate background process, `sa record-daemon`, so the recording keeps going after the terminal that started it closes. You never run that command yourself.
- It takes a frame every 3 seconds, and another on each mouse click when `pynput` is installed (the `record` or `desktop` extra).
- Frames identical to the previous one are dropped before they are written.
- It captures with Pillow when installed, otherwise through PowerShell.
- It stops on its own once it reaches `--maxsc` frames.
- The running session is tracked in `~/psr_output/.polling_session.json`.

`sa stop` ends either engine and writes the same output.

## macOS

PSR.ai takes screenshots with `screencapture -x`, which makes no sound, and reads the name of the frontmost application with `osascript`.

With `pynput` installed (the `record` or `desktop` extra), a frame is taken on each mouse click. Without it, PSR.ai checks the frontmost application every 3 seconds and takes a frame only when it has changed.

## Linux

PSR.ai uses the first screenshot tool it finds on your `PATH`: `gnome-screenshot`, then `scrot`, then `maim`. Install one of them.

The active window title comes from `xprop` on X11 or from GNOME Shell through `gdbus`. When `WAYLAND_DISPLAY` is set, `gdbus` is tried first.

Capture works as on macOS: a frame on each click with `pynput`, or a check every 3 seconds without it.

## macOS and Linux: recording from the CLI

:::caution[Known issue]
On macOS and Linux, `sa start` and `sa stop` do not work together yet.
:::

On these systems the capture runs as a thread inside the `sa start` process, and `sa start` exits as soon as it prints the output path. The capture stops with it. A later `sa stop` is a new process and finds nothing to stop. It prints:

```
No recording output found - the session may have exited before saving.
```

The path `sa start` prints is also different on these systems. It ends in `.d`: a staging folder, not the ZIP that Windows reports.

The [MCP tools](/psr-ai/mcp/) `psr_start_recording` and `psr_stop_and_parse` have the same problem.

Until this is fixed, record on Windows. Commands that do not record the screen do not use these engines: the [browser toolkit](/psr-ai/browser-toolkit/), [`sa video-analyze`](/psr-ai/ai-analysis/#analyze-a-video), and the commands that work on an existing output folder, such as `sa analyze` and `sa testgen`.

## Desktop control

[`sa agent`, `sa replay` and `sa watch`](/psr-ai/replay-and-tests/) drive the mouse and keyboard with `pyautogui` on Windows, macOS and Linux.

On Linux, `pyautogui` needs an X11 display. On a Wayland session without XWayland, mouse and keyboard actions fail.

## Browser extension

The [Hidden Context Layer](/psr-ai/hidden-context-layer/) extension loads automatically in Microsoft Edge and in Playwright's Chromium. Google Chrome needs a manual install, and Firefox and Safari are not supported. The [browser support table](/psr-ai/hidden-context-layer/#browser-support) has the tested versions.
