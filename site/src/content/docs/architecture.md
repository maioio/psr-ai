---
title: How it works
description: The modules behind the sa command, how a recording becomes report.md and data.json, how the desktop agent loop runs, and the safety checks in the code.
---

PSR.ai is one Python package, `psr_ai`. It has three entry points: the `sa` command (`cli.py`), the [MCP server](/psr-ai/mcp/) (`mcp.py`) and the [Hidden Context Layer](/psr-ai/hidden-context-layer/) browser extension (`extension/`). The logic lives in `core/`. Path and text-encoding helpers live in `utils/`.

## Module map

| Area | Modules in `core/` |
|---|---|
| Recording | `recorder.py` picks the engine, `os_context.py` reads the active window title and, on request, the clipboard, `window_detect.py` finds windows that play video. |
| From recording to files | `parser.py`, `generator.py`, `pipeline.py`, `publisher.py`, `reviewer.py`, `html_extractor.py` |
| AI descriptions | `analyzer.py`, `video_analyzer.py` |
| Hidden Context Layer | `context_server.py` (the local listener), `context_browser.py` (starts a browser with the extension), `context_selftest.py` |
| Desktop agent | `agent.py`, `agent_prompt.py`, `executor.py`, `replay.py`, `watcher.py` |
| Tests | `testgen.py`, `healer.py`, `codegen.py` |
| Browser toolkit | `capture.py`, `scraper.py` |
| Other commands | `downloader.py` (`sa download`), `species.py` (`sa scrape-species`), `intent.py` (`sa do`) |

Commands import their optional packages, such as Playwright, pyautogui or the model SDKs, inside the command itself. `sa --help` and the recording commands therefore run without any extra installed.

## From recording to files

```text
sa start ──> recorder engine ──> ZIP
                                  │
sa stop  ──> parse ──> merge browser events ──> <zip name>_output/
                                                   report.md
                                                   data.json
                                                   screenshots/
                                  │
then     ──> sa review · sa analyze · sa pub · sa testgen · MCP tools
```

1. **Pick the engine.** `Recorder()` returns the engine for your platform. On Windows it uses `psr.exe`, and falls back to the polling recorder when `psr.exe` is missing, blocked or refuses to start. If a polling session from an earlier process is still running, it returns that engine, so `sa stop` always stops what `sa start` started. macOS and Linux have their own engines. [Recording](/psr-ai/recording/#how-steps-are-captured) describes how each one takes frames.
2. **Write a ZIP.** `psr.exe` saves a ZIP holding an MHT file. The other engines pack their screenshots and a `data.json` into a ZIP.
3. **Parse.** For a `psr.exe` recording, the parser reads the whole MHT file into memory and decodes the step screenshots in parallel threads.
4. **Write the output folder.** The generator writes `report.md`, `data.json` and the screenshots. If the Hidden Context Layer was running, `sa stop` attaches each browser event to the step it happened in. [Output format](/psr-ai/output-format/) lists every field.

## The desktop agent loop

`sa agent` runs this loop. `sa replay` turns the steps of a recorded ZIP into one task for the same loop, and `sa watch` starts the loop when its trigger appears.

1. **Capture** the screen.
2. **Shrink** a screenshot wider than 1280 px to 1280 px and re-encode it as JPEG, which keeps each request small.
3. **Ask** the model for one action, sending the screenshot and a summary of the last steps.
4. **Act** through the executor for your platform, unless it is a dry run.

The executor accepts `click`, `double_click`, `move`, `type`, `key` and `scroll`. `done`, `ask_user` and `screenshot` send no input. Any other action type raises an error instead of sending input.

Input goes through pyautogui with its fail-safe on: moving the mouse to the top-left corner aborts the current action. There is a 0.05 second pause between input calls.

The loop catches its own errors, so a run never ends in a traceback. It stops for one of these reasons:

| Reason | When |
|---|---|
| `task_complete` | The model reported `done`. |
| `max_steps` | The step limit was reached. |
| `llm_error` | The model call failed. |
| `user_interrupt` | You pressed Ctrl+C. |
| `error` | Any other error. The message is printed. |

[Replay and tests](/psr-ai/replay-and-tests/#before-you-let-it-act) lists the dry-run and confirmation rules.

## Safety checks in the code

- **ZIP extraction.** Before a ZIP is extracted, every entry is checked. If any entry's path would land outside the target folder, extraction stops with an error.
- **Paths.** User-supplied paths go through one helper, `safe_path()`, which refuses paths that climb out of their base folder. File names lose the characters Windows forbids, such as the colons in ISO timestamps.
- **Linux window IDs.** A window ID read from `xprop` must match `0x` followed by hex digits before it is passed back to `xprop`.
- **Browser command line.** A URL that PSR.ai places on a browser's command line must be a plain `http://` or `https://` address, and it comes after `--`. A value such as `--gpu-launcher=…` therefore cannot be read as a browser switch.
- **Local listener.** The Hidden Context Layer listener binds to `127.0.0.1` only.
- **Clipboard.** It is read only with `sa stop --clipboard`. Before it is saved, tokens that look like API keys, GitHub or Slack tokens, JWTs, AWS keys, private keys or `user:password@host` credentials are replaced with a `[REDACTED_…]` marker, and the text is cut to 2,000 characters. Window titles are cut to 300 characters and are not scrubbed.

[Privacy and security](/psr-ai/privacy-and-security/) covers what a recording holds and what leaves your machine.

## Code that no command uses

`ee/` holds a cloud upload client and token checks. No module outside `ee/` imports it, and no `sa` command or MCP tool calls it. The `cloud` extra installs its dependencies, but installing it changes nothing you can run.

## Design records

[ADR-001](https://github.com/maioio/psr-ai/blob/master/docs/adr/001-initial-architecture.md) records the original choice of a `src/` layout with a flat `core/` package. It dates from the first version, when the CLI had only `sa start`, `sa stop` and `sa pub`.
