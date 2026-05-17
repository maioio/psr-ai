# PSR.ai v1.0.0 - Architecture and Command Reference

**Version:** 1.0.0  
**Python:** 3.10+  
**Platforms:** Windows, macOS, Linux  
**Entry point:** `sa` (maps to `psr_ai.cli:main`)

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Core Factory Architecture](#2-core-factory-architecture)
3. [Native OS Recording Engines](#3-native-os-recording-engines)
   - 3.1 [Windows Engine - 5-Tier Fallback Matrix](#31-windows-engine---5-tier-fallback-matrix)
   - 3.2 [macOS Engine - screencapture and osascript](#32-macos-engine---screencapture-and-osascript)
   - 3.3 [Linux Engine - Wayland and X11 Dispatch](#33-linux-engine---wayland-and-x11-dispatch)
4. [Performance Architecture](#4-performance-architecture)
   - 4.1 [In-Memory I/O Pipeline](#41-in-memory-io-pipeline)
   - 4.2 [Pre-Compiled Regex Patterns](#42-pre-compiled-regex-patterns)
   - 4.3 [Parallel Extraction via ThreadPoolExecutor](#43-parallel-extraction-via-threadpoolexecutor)
5. [Token Economy and Deduplication](#5-token-economy-and-deduplication)
   - 5.1 [Idle-Frame Deduplication](#51-idle-frame-deduplication)
   - 5.2 [Semantic Step Merging](#52-semantic-step-merging)
   - 5.3 [Screenshot Resize and JPEG Compression](#53-screenshot-resize-and-jpeg-compression)
6. [Complete CLI Command Reference](#6-complete-cli-command-reference)
7. [Autonomous Agent and Safety Layers](#7-autonomous-agent-and-safety-layers)
   - 7.1 [Agent Loop - Capture, Think, Act, Verify](#71-agent-loop---capture-think-act-verify)
   - 7.2 [Safety Controls](#72-safety-controls)
   - 7.3 [FAILSAFE and Interrupt Handling](#73-failsafe-and-interrupt-handling)
   - 7.4 [Executor Platform Dispatch](#74-executor-platform-dispatch)
   - 7.5 [Screenshot Token Optimization](#75-screenshot-token-optimization)
8. [Module Data Flow](#8-module-data-flow)
9. [Distribution Extras](#9-distribution-extras)
10. [Security Hardening Summary](#10-security-hardening-summary)

---

## 1. Project Overview

PSR.ai is an AI-native CLI that wraps Windows Steps Recorder (`psr.exe`) and extends it with cross-platform capture engines, vision LLM analysis, automated test generation, and a fully autonomous desktop agent.

The design principle throughout is a strict separation between the **capture layer** (platform-specific, read/write), the **analysis layer** (LLM-driven, read-only), and the **action layer** (the agent executor, write-only when authorized). No module crosses these boundaries.

All data produced by the system is written to a standardized `data.json` + `screenshots/` layout inside a ZIP, so every downstream tool - analyzer, testgen, publisher, agent - operates on the same schema regardless of which capture engine produced it.

---

## 2. Core Factory Architecture

The recording subsystem follows a platform factory pattern identical across two components: `recorder.py` and `executor.py`. Both expose a single callable that inspects `sys.platform` at runtime and returns the correct engine, hiding all platform-specific implementation behind a shared abstract base class.

### recorder.py factory

```
Recorder() -> BaseRecorder
```

```
sys.platform == "win32"          ->  WindowsRecorder
sys.platform == "darwin"         ->  MacRecorder
sys.platform.startswith("linux") ->  LinuxRecorder
anything else                    ->  RuntimeError("Unsupported platform")
```

`Recorder` is a plain function (not a class) that returns an instance of the correct subclass. Callers always receive a `BaseRecorder` and call:

- `recorder.start_recording(output_path, max_screenshots)` - begin session, return output path
- `recorder.stop_recording()` - end session, return final ZIP path
- `recorder.is_recording_active()` - poll status

The backward-compatible aliases `recorder.start()` and `recorder.stop()` are preserved so existing scripts do not need changes.

### executor.py factory

```
get_executor() -> BaseExecutor
```

```
sys.platform == "win32"          ->  WindowsExecutor
sys.platform == "darwin"         ->  MacExecutor
sys.platform.startswith("linux") ->  LinuxExecutor
anything else                    ->  RuntimeError("Unsupported platform")
```

The `BaseExecutor` abstract interface exposes: `screenshot()`, `click()`, `double_click()`, `move()`, `type_text()`, `key()`, `scroll()`, and `dispatch()`. The `dispatch()` method acts as an action-dict router: it receives a dict produced by the agent loop (e.g. `{"type": "click", "x": 100, "y": 200}`) and delegates to the correct concrete method, raising `ValueError` for unknown types.

### Why two factories

`Recorder` handles the *input* side: reading the screen passively and storing a structured session. `get_executor()` handles the *output* side: writing mouse and keyboard events back to the OS. They are kept in separate modules so the recorder can be imported without pulling in `pyautogui`, which is only required for the agent feature.

---

## 3. Native OS Recording Engines

### 3.1 Windows Engine - 5-Tier Fallback Matrix

`WindowsRecorder` wraps Microsoft's built-in `psr.exe` (Problem Steps Recorder). On enterprise machines, Group Policy may relocate or disable `psr.exe`, so the engine tries five resolution paths in strict priority order before raising `PSRNotFoundError`:

| Tier | Method | Path |
|---|---|---|
| Fast path | `shutil.which("psr")` | Any directory on `%PATH%` |
| Tier 1 | `%SystemRoot%\System32\psr.exe` | Standard system location |
| Tier 2 | `%WinDir%\System32\psr.exe` | Legacy env var fallback |
| Tier 3a | Bundled portable exe | `assets/psr_fallback/psr.exe` |
| Tier 3b | Bundled shortcut | `assets/psr_fallback/psr_shortcut.lnk` |

If none of the five paths succeeds, `PSRNotFoundError` is raised with a detailed human-readable message that explains all three remediation options (Group Policy, manual copy, PATH symlink).

**UAC elevation:** When `psr.exe` carries a UAC elevation manifest, `subprocess.Popen` raises `OSError` with `winerror=740`. `WindowsRecorder._launch()` catches this specific error code and falls back to `ShellExecuteW` (via `ctypes.windll.shell32`), which hands the elevation prompt to Windows itself. The user never needs to run the terminal as Administrator.

**Shortcut launch:** When the resolved path ends in `.lnk`, the `_use_shell` property returns `True` and `subprocess.Popen` is called with `shell=True`, allowing the Windows Shell to resolve the shortcut before execution.

**Silent recording:** PSR is started with `/gui 0` (no GUI chrome) and `CREATE_NO_WINDOW` creation flag, so no terminal window appears during capture.

### 3.2 macOS Engine - screencapture and osascript

`MacRecorder` does not depend on any third-party system tool and runs on a clean macOS installation.

**Screenshot capture:** Each step calls `subprocess.run(["screencapture", "-x", path])`. The `-x` flag suppresses the shutter sound (no audio permission required). The PNG is written to a temporary file and immediately read back into the session.

**Click tracking (primary):** If `pynput` is installed (`pip install psr-ai[desktop]`), a `pynput.mouse.Listener` thread fires `_on_click` on every mouse-button-down event, capturing a screenshot at the exact moment of interaction. This produces a precise, event-driven recording.

**Interval fallback (secondary):** If `pynput` is absent, a daemon thread calls `_interval_capture()` every `_INTERVAL_SECONDS` seconds, comparing the current frontmost window name (via `osascript`) and pointer position against the last captured state. Idle frames are silently discarded before any disk I/O occurs.

**Active window detection:** `osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true'` returns the current app name. This is embedded in each step's `program` field for grouping by the test generator.

**Output format:** At `stop_recording()`, the thread is joined, a `data.json` manifest is written, and `_pack_to_zip()` bundles everything into a single ZIP file that is structurally identical to what Windows PSR produces.

### 3.3 Linux Engine - Wayland and X11 Dispatch

`LinuxRecorder` handles both modern Wayland compositors and classic X11 displays through automatic environment detection.

**Screenshot tool resolution:** At construction time, `shutil.which()` probes for screenshot tools in priority order:

| Priority | Tool | Command |
|---|---|---|
| 1 | gnome-screenshot | `gnome-screenshot -f {path}` |
| 2 | scrot | `scrot {path}` |
| 3 | maim | `maim {path}` |

The first available tool is stored as `self._screenshot_cmd` and reused for every capture call. If none are found, `screenshot()` raises `RuntimeError` with a clear install hint.

**Active window detection - Wayland vs X11:**

The `_active_window_name()` method inspects `os.environ.get("WAYLAND_DISPLAY")` to determine the display stack and selects the appropriate IPC mechanism:

```
WAYLAND_DISPLAY is set:
    1. Try gdbus  (org.gnome.Shell.Eval - GNOME Shell D-Bus method)
    2. Fall back to xprop (XWayland compatibility layer)

WAYLAND_DISPLAY is absent (pure X11):
    1. Try xprop  (_NET_ACTIVE_WINDOW root property + _NET_WM_NAME)
    2. Fall back to gdbus
```

**X11 security validation:** Before passing the xprop window ID as a subprocess argument, it is validated against the compiled pattern `_RE_X11_WINDOW_ID = re.compile(r'^0x[0-9a-fA-F]+$')`. Any value that does not match a valid hex window ID is rejected, preventing malformed or adversarially crafted xprop output from injecting unexpected arguments into the subprocess call.

**Idle deduplication:** Same logic as macOS - interval-mode recordings compare `(program, x, y)` triple against the last captured state before every disk write.

---

## 4. Performance Architecture

### 4.1 In-Memory I/O Pipeline

The original implementation extracted the ZIP to a temporary directory, wrote every PNG to disk, read it back, and deleted the directory. This pattern caused significant I/O latency for recordings with many steps.

The rewritten parser (`parser.py`) eliminates all intermediate disk I/O:

```python
with zipfile.ZipFile(zip_path) as zf:
    raw_bytes = zf.read("Output.mht")          # entire MHT into RAM
    msg = email.message_from_bytes(raw_bytes)  # parsed entirely in memory
    for part in msg.walk():
        if part.get_content_type() == "image/png":
            b64_data = part.get_payload()       # raw base64 string
            img_bytes = base64.decodebytes(     # decode in RAM
                _RE_WS.sub(b"", b64_data.encode())
            )
            # img_bytes passed directly to Step.image_bytes - no temp file
```

The `io.BytesIO` buffer is used wherever PIL needs a file-like object:

```python
buf = io.BytesIO()
img.save(buf, format="PNG")
return buf.getvalue()   # bytes returned directly, no disk path involved
```

This approach removes all `tempfile.mkdtemp()` calls from the hot path and avoids the filesystem entirely for intermediate representations.

### 4.2 Pre-Compiled Regex Patterns

All patterns used during MHT parsing are compiled once at module import time and stored as module-level constants. This eliminates per-call compilation overhead for recordings with hundreds of steps:

| Pattern constant | Purpose |
|---|---|
| `_RE_STEP` | Match `<p class="step">Step N: <b>text</b></p>` |
| `_RE_TIMESTAMP` | Extract `M/D/YYYY H:MM:SS AM/PM` from HTML |
| `_RE_PROGRAM` | Extract `window title:` and `program:` values |
| `_RE_DD` | Fallback `<dd>...</dd>` block extraction |
| `_RE_TAGS` | Strip HTML tags from fallback `<dd>` content |
| `_RE_TITLE` | Extract `<title>` from the MHT HTML document |
| `_RE_WS` | Strip CRLF whitespace injected by PSR's base64 encoder |
| `_RE_X11_WINDOW_ID` | Validate X11 window IDs before subprocess use |

### 4.3 Parallel Extraction via ThreadPoolExecutor

PNG steps are independent of each other after the MHT has been parsed. The parser uses `concurrent.futures.ThreadPoolExecutor` to write all step images to disk concurrently rather than sequentially:

```python
with ThreadPoolExecutor() as executor:
    futures = [
        executor.submit(_write_step_png, step, output_dir)
        for step in session.steps
    ]
    for f in futures:
        f.result()   # re-raise any write exception on the calling thread
```

For a 25-step recording on a typical SSD, this reduces the serialization phase from a sequential sum of write latencies to approximately the latency of the single slowest write. The speedup scales with step count and storage I/O depth.

---

## 5. Token Economy and Deduplication

### 5.1 Idle-Frame Deduplication

Both `MacRecorder` and `LinuxRecorder` maintain a three-field dedup state after every captured step:

```python
self._last_program: str   # frontmost window name
self._last_x: int         # pointer X coordinate
self._last_y: int         # pointer Y coordinate
```

Before each interval capture, `_is_idle()` compares the current triple against the stored state. If all three values match, the frame is silently discarded - no screenshot is taken, no disk write occurs, and the step counter is not incremented. This prevents recordings from accumulating identical frames when the user is reading or thinking between actions.

The check happens entirely in Python before any subprocess or I/O call, so the cost of a discarded frame is negligible.

### 5.2 Semantic Step Merging

`generator.py` implements `merge_consecutive_steps()`, which collapses repetitive runs of the same action type before the session is serialized to the AI context block in `report.md`. This directly reduces the token count sent to the LLM during analysis.

**Mergeable action families:**

| Family | Example sequence |
|---|---|
| scroll | scroll-down, scroll-down, scroll-down |
| type / input | individual keystrokes grouped into one type event |
| key | repeated key presses (e.g. arrow-down navigation) |

**Timestamp-based grouping threshold:** Steps within 5 seconds of each other and of the same action family are eligible for merging. Steps separated by more than 5 seconds are treated as distinct interactions even if they share the same action type.

The merged step preserves the first step's timestamp and label, appends a count annotation (e.g. "x3"), and produces a single history entry for the LLM. The original unmerged steps remain in `data.json` for the test generator and session log.

### 5.3 Screenshot Resize and JPEG Compression

`agent_prompt.py` contains `resize_screenshot_bytes(raw: bytes, max_width: int = 1280) -> bytes`, which runs every screenshot through a proportional downscale before base64 encoding for the LLM API call.

**Processing path:**

```
raw PNG bytes (e.g. 2560 x 1440, ~2MB)
    |
    v
PIL.Image.open(io.BytesIO(raw))
    |
    +-- width <= 1280? --> return original bytes unchanged (PNG, no re-encode)
    |
    +-- width > 1280?  --> resize proportionally (LANCZOS filter)
                          convert to RGB
                          save to io.BytesIO as JPEG, quality=85, optimize=True
                          return JPEG bytes (~200-400KB for typical desktop)
```

The `build_messages()` function accepts both `screenshot_b64: str` (legacy path) and `screenshot_raw: bytes | None` (optimized path). When `screenshot_raw` is provided:

- The resize runs in-memory with no intermediate files.
- The `media_type` field in the API message is set to `"image/jpeg"` when a resize occurred and `"image/png"` when the image was already within the limit (original bytes passed through unchanged).
- `PIL` import failure is caught silently; the original bytes are used as-is.

On a 2560x1440 display, this reduces per-step LLM payload from approximately 2-3MB to 200-400KB, cutting token cost per step by roughly 80%.

---

## 6. Complete CLI Command Reference

All commands are accessed through the `sa` entry point. The entry point maps directly to `psr_ai.cli:main` (a Click group). Every command uses lazy imports so that, for example, `sa start` does not load `anthropic` or `pyautogui`.

### Command table

| Command | Arguments / Options | Purpose | Core module |
|---|---|---|---|
| `sa start` | `[OUTPUT]` `--maxsc N` | Launch a recording session. Windows: starts `psr.exe`. macOS/Linux: starts background capture thread. Returns the path where the ZIP will be written. | `core.recorder` |
| `sa stop` | *(none)* | Stop the active session. Waits for `psr.exe` to commit the ZIP on Windows; joins the capture thread on macOS/Linux. Automatically invokes the parser and renders `report.md` + `action_replay.gif`. | `core.recorder`, `core.parser`, `core.generator` |
| `sa status` | *(none)* | Check whether a recording is currently active. On Windows, probes the process list for `psr.exe`. On macOS/Linux, polls the recorder thread state. | `core.recorder` |
| `sa review` | `SESSION_ZIP` | Interactive step-by-step review. Displays each step's label and screenshot path, prompts to keep or drop. Useful for removing sensitive screens before publishing. Rewrites the ZIP with only the retained steps. | `core.parser`, `core.generator` |
| `sa analyze` | `SESSION_ZIP` `--provider anthropic\|openai` `--model MODEL` | Send each step's screenshot to a vision LLM (Claude or GPT-4o) with a structured prompt. Writes an `ai_description` field back into `data.json` and regenerates `report.md` with the enriched content. Validates the API key before entering the loop. | `core.analyzer` |
| `sa pub` | `SESSION_ZIP` `--dest DIR` `--git` `--branch NAME` `--github-api` `--token TOKEN` `--repo OWNER/REPO` | Copy session assets (`report.md`, `data.json`, `action_replay.gif`, `screenshots/`) to a destination directory. With `--git`: stages and commits via subprocess git, pushes with `-u remote branch` for upstream tracking. With `--github-api`: pushes directly via the GitHub Contents REST API without a local git installation. | `core.publisher` |
| `sa heal` | `[PROJECT_ROOT]` `[SESSION_ZIP]` `--pytest-args ARGS` | Run the project's pytest suite and return structured diagnostics. Parses pass/fail/error counts, extracts `FailingTest` records with source hints, and correlates failures against the session's `data.json` if provided. Does not auto-fix anything; surfaces the structured report for human or agent consumption. | `core.healer` |
| `sa testgen` | `SESSION_ZIP` `--output PATH` | Read `data.json` from the session and emit a Playwright Python test file. Groups steps by program window, maps action descriptions to `page.goto()`, `page.keyboard.type()`, `page.mouse.click()`, `page.mouse.wheel()`, and `expect(page).to_have_screenshot()` calls. | `core.testgen` |
| `sa pipeline` | `SESSION_ZIP` `--analyze` `--publish` `--pr` `[options]` | Run the full parse-analyze-publish chain as a single atomic operation. Each stage is optional via flags. Never raises; all exceptions are captured in `PipelineResult.error`. Writes a structured report at the end. | `core.pipeline` |
| `sa agent` | `TASK` `--dry-run / --no-dry-run` `--ask / --no-ask` `--max-steps N` `--provider anthropic\|openai` `--model MODEL` `--screenshot PATH` `--output-dir DIR` | Run the autonomous desktop agent loop against a natural-language task description. See Section 7 for the full safety and execution model. | `core.agent`, `core.agent_prompt`, `core.executor` |

### Global option notes

- All commands that accept `--provider` default to `anthropic` and validate the corresponding environment variable (`ANTHROPIC_API_KEY` or `OPENAI_API_KEY`) before making any network call.
- All output paths default to a timestamped directory under the platform-appropriate user data location (`~/Library/Application Support/psr_ai` on macOS, `%APPDATA%/psr_ai` on Windows, `~/.local/share/psr_ai` on Linux).
- The `_echo()` wrapper in `cli.py` catches `UnicodeEncodeError` on every print call and falls back to ASCII-safe output with `errors="replace"`, preventing crashes in terminals with restricted code pages (common on Windows with default `cp1252` encoding).

---

## 7. Autonomous Agent and Safety Layers

### 7.1 Agent Loop - Capture, Think, Act, Verify

`core.agent.run_agent(config, prompt_fn, echo_fn)` is the public entry point. It is guaranteed never to raise: all exceptions are caught and written to `AgentResult.error`.

The internal loop in `_run()` follows a strict four-phase cycle per step:

```
for step_idx in range(1, config.max_steps + 1):

    Phase 1 - Capture:
        if step 1 and config.initial_screenshot:
            read initial_screenshot from disk (no executor call)
        else:
            sleep(config.screenshot_interval)
            img_bytes = executor.screenshot()
        save PNG to screenshots_dir/step_NNN.png

    Phase 2 - Think:
        response = _call_llm(
            config, model, img_b64, history, step_idx,
            build_messages, parse_response,
            screenshot_raw=img_bytes,   # enables resize optimization
        )
        extract: action, reasoning, confidence, is_destructive, requires_confirmation

    Phase 3 - Decide:
        if action.type == "done":
            record step, set success=True, break
        if action.type == "ask_user":
            call prompt_fn(question), feed answer into history, continue
        compute needs_approval:
            dry_run=True        -> always skip execution
            ask_before_action   -> prompt before every action
            is_destructive and ask_before_destructive -> always prompt
            requires_confirmation -> always prompt

    Phase 4 - Act:
        if approved and not dry_run:
            executor.dispatch(action)
        record AgentStep to result.steps and history

else:  # loop exhausted
    result.stopped_reason = "max_steps"

_write_session_log(result, session_dir)
```

The session log is written in the same `data.json` schema as a recorded session, making every agent run inspectable with `sa analyze`, `sa testgen`, and `sa pub`.

### 7.2 Safety Controls

Three independent safety layers, all enabled by default:

| Control | Default | Effect |
|---|---|---|
| `dry_run=True` | ON | Actions are described in output but never executed. `executor.dispatch()` is never called. |
| `ask_before_action=True` | ON | Before every non-terminal action, the user is prompted `Execute this action? [y/N]`. Execution only proceeds on explicit `y` or `yes`. |
| `ask_before_destructive=True` | ON | Actions where `is_destructive=True` or `requires_confirmation=True` are always prompted, even when `ask_before_action=False`. This gate cannot be bypassed by the `--no-ask` flag. |

Fully autonomous mode (`--no-dry-run --no-ask`) disables `dry_run` and `ask_before_action` but preserves the destructive action gate. The CLI prints a prominent warning when both flags are combined.

### 7.3 FAILSAFE and Interrupt Handling

**pyautogui FAILSAFE:** The `_pyautogui()` helper in `executor.py` sets `pyautogui.FAILSAFE = True` at every invocation before returning the module. This activates pyautogui's built-in emergency stop: moving the physical mouse to the top-left corner of the screen (coordinate 0, 0) immediately raises `pyautogui.FailSafeException`, which propagates up through `executor.dispatch()`, through `_run()`, and is caught by `run_agent()`'s outer `except Exception` handler, resulting in a clean `AgentResult` with `stopped_reason="error"` rather than a hung process.

**KeyboardInterrupt (Ctrl+C):** `run_agent()` wraps `_run()` with a dedicated `except KeyboardInterrupt` clause that fires before the general `except Exception` clause:

```python
try:
    _run(config, result, _echo, _prompt)
except KeyboardInterrupt:
    result.stopped_reason = "user_interrupt"
    result.error = "Interrupted by user (Ctrl+C)."
    _echo("\nAgent interrupted. Stopping cleanly.")
except Exception as exc:
    result.error = str(exc)
    result.stopped_reason = "error"
    _echo(f"Agent error: {exc}")
return result
```

The interrupt is fully absorbed: no exception propagates to the CLI layer, no traceback is printed, and the partial session log is still written to `session_dir/data.json` with all steps captured before the interrupt.

**Wayland warning:** `LinuxExecutor.__init__()` checks `os.environ.get("WAYLAND_DISPLAY")` at construction time. If set, a non-blocking warning is printed:

```
Warning: Active Wayland session detected.
PyAutoGUI input injection may be restricted by Linux security profiles.
Switch to an X11 session if mouse or keyboard events fail to trigger.
```

The executor is still constructed and functional (XWayland may be available); the warning is informational only and does not prevent the agent from running.

### 7.4 Executor Platform Dispatch

`core.executor.get_executor()` returns the correct engine:

| Platform | Class | Screenshot method | Input method |
|---|---|---|---|
| `win32` | `WindowsExecutor` | `PIL.ImageGrab.grab()` - no extra dependencies | `pyautogui` |
| `darwin` | `MacExecutor` | `subprocess screencapture -x` | `pyautogui` |
| `linux*` | `LinuxExecutor` | `gnome-screenshot` / `scrot` / `maim` (first found) | `pyautogui` (X11 only) |

All three engines share the `_pyautogui()` lazy-import helper, which sets `FAILSAFE = True` and `PAUSE = 0.05` (50ms inter-action pause for input stability) before returning the module. If `pyautogui` is not installed, the helper raises `ImportError` with a clear message: `pip install psr-ai[agent]`.

`BaseExecutor.dispatch()` handles the following action types:

| Action type | Calls |
|---|---|
| `click` | `self.click(x, y, button)` |
| `double_click` | `self.double_click(x, y)` |
| `move` | `self.move(x, y)` |
| `type` | `self.type_text(text)` |
| `key` | `self.key(key_combo)` |
| `scroll` | `self.scroll(x, y, direction, amount)` |
| `done` | no-op (handled by agent loop before dispatch) |
| `ask_user` | no-op (handled by agent loop before dispatch) |
| `screenshot` | no-op |
| anything else | raises `ValueError("Unknown action type: ...")` |

### 7.5 Screenshot Token Optimization

Before each LLM call, `agent.py` passes the raw screenshot bytes as `screenshot_raw` to `_call_llm()`, which forwards them to `build_messages()` in `agent_prompt.py`. `build_messages()` calls `resize_screenshot_bytes()`, which:

1. Opens the bytes with `PIL.Image.open(io.BytesIO(raw))`.
2. If `image.width <= 1280`, returns the original bytes unchanged and uses `media_type = "image/png"`.
3. If `image.width > 1280`, computes `ratio = 1280 / image.width`, resizes with `Image.LANCZOS`, converts to RGB, saves to `io.BytesIO` as JPEG at quality 85 with `optimize=True`, and returns the JPEG bytes with `media_type = "image/jpeg"`.

All of this occurs in RAM with no temporary files. The `media_type` field is set correctly in the Anthropic Messages API image source dict so the API decodes the image correctly regardless of format.

---

## 8. Module Data Flow

```
User invokes `sa start`
       |
       v
  Recorder() factory
  --> WindowsRecorder / MacRecorder / LinuxRecorder
       |
       | produces: session.zip (MHT + screenshots/ on Windows;
       |           data.json + screenshots/ on macOS/Linux)
       v
  `sa stop` triggers parse + render
       |
  parse_session(zip_path) [parser.py]
  --> ZipFile.read() -> MHT bytes in RAM
  --> email.message_from_bytes() -> MIME walk
  --> ThreadPoolExecutor parallel PNG write
  --> RecordingSession(steps=[Step(index, label, image_bytes, ...)])
       |
  generate_markdown(session, output_dir) [generator.py]
  --> merge_consecutive_steps()
  --> report.md with AI context block + TOC + timeline
       |
  GifGenerator(session).generate() [generator.py]
  --> PIL frames with cursor halo compositing
  --> action_replay.gif
       |
       v
  `sa analyze` [optional]
  --> _analyze_with_anthropic() / _analyze_with_openai()
  --> per-step ai_description written back to data.json
  --> report.md regenerated with enriched content
       |
       v
  `sa pub` [optional]
  --> Publisher.copy(session_dir, dest_root)
  --> Publisher.commit_and_push() [git subprocess or GitHub API]
       |
       v
  `sa testgen` [optional]
  --> generate_playwright_test(session)
  --> Playwright Python test file
       |
       v
  `sa heal` [optional]
  --> heal(project_root, session_dir)
  --> pytest subprocess + structured diagnostics
```

```
User invokes `sa agent "task"`
       |
       v
  AgentConfig validated (API key pre-check)
       |
  run_agent(config) [agent.py]
       |
  for each step:
       |
       +---> executor.screenshot() [executor.py]
       |           |
       |           v
       |     resize_screenshot_bytes() [agent_prompt.py]
       |           |
       |           v
       +---> build_messages() -> _call_anthropic() / _call_openai()
       |           |
       |           v
       +---> parse_response() -> action dict
       |           |
       |           v
       +---> safety gate (dry_run / ask_before_action / is_destructive)
       |           |
       |           v
       +---> executor.dispatch(action) [if approved]
       |
  _write_session_log() -> data.json (PSR.ai schema)
```

---

## 9. Distribution Extras

`pyproject.toml` defines five optional dependency groups. All are installable as `pip install psr-ai[<extra>]`. The runtime dependency is only `click>=8.1`; everything else is opt-in.

| Extra | Packages | When needed |
|---|---|---|
| `[gif]` | `Pillow>=10.0` | `sa stop` and `sa pub --gif` - animated GIF generation requires PIL for frame compositing and cursor halo rendering. |
| `[ai]` | `anthropic>=0.25`, `openai>=1.0` | `sa analyze` - vision LLM enrichment. Install this extra and export `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`. |
| `[desktop]` | `pynput>=1.7` | Event-driven click tracking on macOS and Linux. Without this extra, recording falls back to 3-second interval capture. Not required on Windows (psr.exe handles capture natively). |
| `[agent]` | `pyautogui>=0.9.54`, `Pillow>=10.0`, `anthropic>=0.25` | `sa agent` - includes all three dependencies required for the autonomous agent: pyautogui for mouse/keyboard execution, Pillow for screenshot resize optimization, and the Anthropic SDK for vision LLM calls. |
| `[dev]` | `pytest>=8.0`, `pytest-cov>=5.0`, `Pillow>=10.0` | Development and CI only. Not included in any production install. |

**Recommended install paths:**

```bash
# Read-only analysis workflow (no desktop control):
pip install psr-ai[gif,ai]

# Full recording with event-driven capture (macOS/Linux):
pip install psr-ai[gif,ai,desktop]

# Full autonomous agent (all capabilities):
pip install psr-ai[agent]

# Development:
pip install -e ".[agent,desktop,dev]"
```

---

## 10. Security Hardening Summary

The following security controls were implemented during the hardening phase:

| Control | Location | What it prevents |
|---|---|---|
| X11 window ID regex validation | `recorder.py:_RE_X11_WINDOW_ID` | Malformed or adversarial `xprop` output injecting unexpected arguments into subprocess calls |
| ZIP slip protection | `parser.py:extract_zip()` | Path traversal via crafted ZIP entries with `../` components |
| API key pre-check | `analyzer.py`, `agent.py` | Clear `ValueError` before any network call; no silent per-step failures |
| `pyautogui.FAILSAFE = True` | `executor.py:_pyautogui()` | Emergency stop via top-left mouse corner; fires on every `_pyautogui()` call |
| `KeyboardInterrupt` isolation | `agent.py:run_agent()` | Ctrl+C absorbed cleanly; partial session always written; no traceback leak |
| `CREATE_NO_WINDOW` flag | `recorder.py:WindowsRecorder._launch()` | Prevents console window flash when launching `psr.exe` |
| UAC elevation via `ShellExecuteW` | `recorder.py:WindowsRecorder._shell_execute()` | Transparent UAC elevation without requiring elevated terminal |
| Lazy imports throughout | `cli.py`, `executor.py`, `agent.py` | Heavy dependencies (`pyautogui`, `anthropic`, `openai`, `PIL`) are never imported unless the specific feature is actually used |
| Unicode error fallback | `cli.py:_echo()` | Prevents `UnicodeEncodeError` crash on Windows consoles with restricted code pages |
| Wayland input warning | `executor.py:LinuxExecutor.__init__()` | Non-blocking advisory; prevents silent failure of input injection on Wayland without XWayland |

---

*PSR.ai v1.0.0 - 522 tests passing - Python 3.10+ - MIT License*
