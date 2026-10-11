---
title: Troubleshooting
description: Fixes for the problems you are most likely to meet when recording, stopping, pairing the browser extension and running the optional commands.
---

Each section starts with what you see, then explains the cause and the fix. When something is a known defect rather than a setup problem, [Limitations](/psr-ai/limitations/) lists it too.

Run a command with `--verbose` before its name, for example `sa --verbose stop`, to see what it is doing.

## Recording

### The polling recorder runs instead of Steps Recorder

On Windows, PSR.ai switches from `psr.exe` to its own polling recorder without asking when `psr.exe` is missing, blocked or refuses to start. You can tell the polling recorder is running when `~/psr_output/.polling_session.json` exists during a recording. Both engines produce the same output, so this only matters if you want Steps Recorder's own step descriptions.

If `psr.exe` is blocked by Group Policy, an administrator can turn it back on:

1. Open the Local Group Policy Editor (`gpedit.msc`).
2. Go to **Computer Configuration > Administrative Templates > Windows Components > Application Compatibility**.
3. Set **Turn off Steps Recorder** to **Not Configured** or **Disabled**.

The same policy is stored as the `DisableUAR` value under `HKLM\Software\Policies\Microsoft\Windows\AppCompat`; `1` turns Steps Recorder off. On a managed machine, ask your IT team rather than changing it yourself.

[Platforms](/psr-ai/platforms/#windows) explains where PSR.ai looks for `psr.exe`.

### `sa status` says psr.exe is running

`sa status` prints `psr.exe is running` for any active recording, including one made by the polling recorder. Use the session file described above to tell the engines apart.

### `sa stop` says no recording output was found

```
No recording output found - the session may have exited before saving.
```

- **On macOS or Linux** this is a known issue: the capture stops when `sa start` exits, so `sa stop` has nothing to stop. Record on Windows for now. [Details](/psr-ai/platforms/#macos-and-linux-recording-from-the-cli).
- **On Windows** the recording ended before it wrote its ZIP, for example because the recorder process was closed. Start a new recording.

### `sa stop` says auto-parse failed

```
Warning: auto-parse failed (...). The ZIP was saved successfully.
```

The recording is safe in the ZIP whose path `sa stop` prints. The hint that follows, to run `sa pub` on the ZIP, does not work: `sa pub` only accepts an output folder.

To parse the ZIP again, run `sa pipeline` from a folder that is **not** inside a Git repository:

```bash
mkdir psr-reparse
cd psr-reparse
sa pipeline ~/psr_output/psr_20260101_120000.zip
```

Outside a Git repository, `sa pipeline` parses the ZIP into `<zip name>_output` next to it, copies the result into `docs/sessions/` under the current folder, and skips the commit and push. Inside a repository it commits and pushes, so check with `git status` first. This route does not add the browser events, element HTML or window title that `sa stop` adds.

If parsing fails again, keep the ZIP and [report the problem](#report-a-problem) with the text in brackets after `auto-parse failed`.

### `sa start` prints a traceback ending in `Unsupported platform`

PSR.ai records on Windows, macOS and Linux only. On any other system, `sa start` stops with a Python traceback instead of a short message. The commands that do not record the screen, such as the [browser toolkit](/psr-ai/browser-toolkit/), still work.

## Hidden Context Layer

### No browser events in the report

When the extension never checked in, `sa stop` says so. Run the doctor during a recording:

```bash
sa start
sa context doctor
```

Fix the first line that is not `OK`:

| Line | Value | Fix |
|---|---|---|
| Listener | `DOWN` | No recording is running. Run `sa start`; it starts the listener. |
| Listener | `AUTH` | The extension holds a different pairing token. Run `sa context token` and paste the token into the extension's options page, **PSR.ai context layer**. |
| Extension | `STALE` | The extension checked in, then stopped. The browser may be closed, or the extension was turned off. |
| Extension | `DOWN` | The extension never checked in. Open `chrome://extensions` or `edge://extensions` and make sure **PSR.ai Hidden Context Layer** is turned on. If it is not there, follow the [manual install](/psr-ai/hidden-context-layer/#manual-install). |
| Browsers | `NONE` | No Chromium-family browser was found. Install Microsoft Edge. |

[Check that it works](/psr-ai/hidden-context-layer/#check-that-it-works) describes every line, and `sa context test` checks the whole loop.

### Google Chrome does not load the extension

Chrome ignores the switch PSR.ai uses to load the extension, and `sa context browser --browser chrome` refuses to start. Use Microsoft Edge, or load the extension into Chrome by hand with the [manual install](/psr-ai/hidden-context-layer/#manual-install).

### `sa context test` fails although the extension is paired

The test page opens in your system's default browser. If that is not the browser with the extension, the checks fail. Run `sa --verbose context test`, copy the test page URL it prints, and open it in the PSR.ai browser.

## Optional commands

### Playwright is not installed

The browser toolkit and the element HTML step of `sa stop` need Playwright and a browser. Some messages tell you to run `pip install psr-ai[capture]`. That fails, because PSR.ai is not on PyPI yet. Install the extra from your clone instead:

```bash
pip install -e ".[capture]"
playwright install chromium
```

### An API key is not set

```
ANTHROPIC_API_KEY environment variable is not set.
```

`sa analyze`, `sa agent`, `sa replay`, `sa watch` and `sa video-analyze` read the key from the environment. Set it in the same terminal before you run the command:

```bash
export ANTHROPIC_API_KEY=...        # macOS and Linux
$env:ANTHROPIC_API_KEY = "..."      # Windows PowerShell
```

Use `OPENAI_API_KEY` with `--provider openai`. Publishing a pull request needs `GITHUB_TOKEN` and `GITHUB_REPO` (as `owner/repo`).

### `sa do` fails with an error about `mix_stderr`

With Click 8.2 or later, `sa do` chooses a command but cannot run it. Add `--dry-run` to print the command, then run it yourself. Include the full URL, with `https://`, in the request:

```bash
sa do "take a screenshot of https://example.com" --dry-run
```

### `sa צלם` is not recognised

The Hebrew alias of `sa capture` is only registered when your terminal's encoding can print Hebrew. Use `sa capture`, or switch the terminal to UTF-8.

## Report a problem

Open an issue at [github.com/maioio/psr-ai](https://github.com/maioio/psr-ai/issues) with:

- your operating system and Python version,
- the output of `sa --version`,
- the command you ran with `--verbose`, and its full output.

Recordings contain full-screen screenshots. Do not attach one unless you have checked it with [`sa review`](/psr-ai/ai-analysis/#review-and-drop-steps). For a security problem, follow [Reporting a vulnerability](/psr-ai/privacy-and-security/#reporting-a-vulnerability) instead.
