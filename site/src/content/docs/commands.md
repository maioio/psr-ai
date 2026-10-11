---
title: Commands
description: Every sa command, grouped by job, with the extra each one needs.
---

The command-line tool is `sa`. It has 42 commands. Run `sa --help` for the list and `sa <command> --help` for every option of one command.

## Global options

These go before the command name, for example `sa --verbose stop`.

| Option | What it does |
|---|---|
| `--verbose` / `--quiet` | Quiet is the default everywhere: only results are printed. `--verbose` adds progress narration. |
| `--version` | Print the version and exit. |

## Recording

| Command | What it does |
|---|---|
| `sa start` | Start a recording. See [Recording](/psr-ai/recording/). |
| `sa status` | Print whether a recording is active. |
| `sa stop` | Stop the recording and write the report, data and screenshots. |

## Enrich a recording

| Command | What it does | Needs |
|---|---|---|
| `sa analyze SOURCE_DIR` | Send each screenshot to a vision model and store its description on the step. | `ai` extra and an API key |
| `sa review SOURCE_DIR` | Go through the steps one by one and drop the ones you do not want to keep. | |

Both are described in [AI analysis](/psr-ai/ai-analysis/).

## Publish

| Command | What it does |
|---|---|
| `sa pub SOURCE_DIR` | Copy an output folder into your docs, and optionally commit and push it. |
| `sa pipeline ZIP_PATH` | Parse, optionally analyze, publish, commit and push in one run, with an optional pull request. |

See [AI analysis](/psr-ai/ai-analysis/#publish-a-session) for their options.

## Agents and tests

| Command | What it does | Needs |
|---|---|---|
| `sa mcp` | Run the MCP server on stdin and stdout. `sa mcp install` adds it to Claude Desktop; `sa mcp path` prints the config path. | |
| `sa agent TASK` | Let Claude plan desktop actions toward a goal. Dry run by default. | `agent` extra and an API key |
| `sa replay ZIP_PATH` | Replay a recorded session with Claude driving the mouse and keyboard. Dry run by default. | `agent` extra and an API key |
| `sa watch TRIGGER` | Watch the screen and act when a condition appears. Dry run by default. | `agent` extra and an API key |
| `sa testgen SOURCE_DIR` | Turn a recorded session into a Playwright test file. | |
| `sa heal` | Run your pytest suite and print structured failure diagnostics, optionally next to a recorded session. | |
| `sa do INSTRUCTION` | Match a plain-language request, in many languages, to a browser toolkit or download command and run it. | |

[MCP server](/psr-ai/mcp/) and [Replay and tests](/psr-ai/replay-and-tests/) have the details.

:::caution
`sa do` is meant to run the command it chooses straight away, and with Click 8.2 or later that step currently fails. Pass `--dry-run` to print the command and run it yourself. See [Limitations](/psr-ai/limitations/).
:::

## Hidden Context Layer

`sa context` has these subcommands. See [Hidden Context Layer](/psr-ai/hidden-context-layer/).

| Subcommand | What it does |
|---|---|
| `install` | Prepare the browser extension and the pairing token. |
| `browser` | Open a browser with the extension in a dedicated PSR.ai profile. |
| `token` | Print the pairing token, or replace it with `--rotate`. |
| `status` | Show whether the listener is running and how many events it holds. |
| `events` | Print the events captured for a session. |
| `doctor` | Check the listener, the token, the extension and the session, and say what is wrong. |
| `test` | Open a test page during a recording and confirm that events arrive. |
| `serve` | Run the listener by hand. `sa start` normally does this for you. |

## Browser toolkit

These drive a real browser through Playwright. They are independent of recording. See [Browser toolkit](/psr-ai/browser-toolkit/).

`accessibility`, `api`, `assert`, `browse`, `capture`, `clock`, `codegen`, `cookies`, `devices`, `events`, `form`, `har`, `intercept`, `locate`, `pdf`, `screenshot`, `scrape`, `test-all`, `trace`, `trace-show`, `video`, `websockets`.

## Media

| Command | What it does | Needs |
|---|---|---|
| `sa video-analyze SOURCE` | Describe a video file or URL from sampled frames and an audio transcript. | `analyze` and `ai` extras, ffmpeg and an Anthropic API key |
| `sa download URL` | Download a video or its audio with yt-dlp, or an image gallery with gallery-dl. | `download` extra |
| `sa download-profile URL` | Download the media of a profile or channel. | `download` extra |
| `sa instagram URL` | Download an Instagram reel, post or album through its mobile API. | `instagram` extra and an Instagram login |
| `sa scrape-species QUERY` | Look up a species on iNaturalist, GBIF and BugGuide. | |

`sa video-analyze` is covered in [AI analysis](/psr-ai/ai-analysis/#analyze-a-video). Only download media you have the right to download.

## Optional extras

The base install depends only on `click`. Each extra adds what a group of commands needs. Install one or more from the cloned repo:

```bash
pip install -e ".[gif,ai]"
```

| Extra | Adds | Used by |
|---|---|---|
| `gif` | Pillow | The replay GIF in `sa stop` |
| `record` | Pillow, pynput | Click-triggered capture in the polling, macOS and Linux recorders |
| `desktop` | pynput | Click-triggered capture |
| `ai` | anthropic, openai | `sa analyze`, `sa pipeline --analyze`, `sa video-analyze` |
| `agent` | pyautogui, Pillow, anthropic | `sa agent`, `sa replay`, `sa watch` |
| `capture` | playwright, pypdf | The browser toolkit, and the element HTML fetch in `sa stop` |
| `download` | yt-dlp, gallery-dl | `sa download`, `sa download-profile` |
| `instagram` | instagrapi | `sa instagram` |
| `analyze` | faster-whisper, yt-dlp | `sa video-analyze` |
| `analyze-legacy` | openai-whisper, yt-dlp | `sa video-analyze` with the older Whisper package |
| `full` | anthropic, faster-whisper, yt-dlp, Pillow, playwright, pynput | Most features at once |
| `dev` | pytest, pytest-asyncio, pytest-cov, Pillow, aiohttp | Running the test suite |
| `cloud` | aiohttp | Library code only; no command uses it |

After installing `capture`, download a browser once:

```bash
playwright install chromium
```
