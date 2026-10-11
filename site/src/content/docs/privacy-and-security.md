---
title: Privacy and security
description: What a recording contains, which commands send data off your machine and where, and which local files hold credentials.
---

PSR.ai runs on your machine. A recording stays on your disk until you run a command that sends it somewhere, and no command sends usage statistics. This page lists every place data can go, so you can decide before you run a command.

## What a recording holds

The output folder that `sa stop` writes, under `~/psr_output/` by default, can contain:

- **Screenshots of the whole screen** at every step. Other windows, notifications and anything else visible are captured too.
- **Window titles and step descriptions.** These often name documents, email subjects, chat partners and URLs.
- **Browser events**, when the [Hidden Context Layer](/psr-ai/hidden-context-layer/) extension is paired: requests, responses, console messages and errors, redacted as described [on that page](/psr-ai/hidden-context-layer/#security-and-limits).
- **Element HTML** of the clicked elements, fetched again after the recording (see below).
- **Clipboard text**, only when you run `sa stop --clipboard`.

The files are not encrypted. Before you share a folder or send it to a model, run [`sa review`](/psr-ai/ai-analysis/#review-and-drop-steps) to drop steps with private content. It deletes those screenshots from disk.

### Clipboard

The clipboard is read only with `--clipboard`, and only its text. Before it is written to `data.json`:

- JWTs, API keys starting with `sk-`, `pk-` or `rk-`, GitHub `ghp_` tokens, Slack tokens, AWS access key IDs, private key blocks, `user:password@host` strings and card-like numbers are replaced with markers such as `[REDACTED_API_KEY]`.
- A clipboard that holds nothing but one long opaque string is replaced entirely.
- The text is cut to 2,000 characters.

Redaction matches known shapes. A plain password in the clipboard is not recognised, so leave `--clipboard` off when the clipboard may hold one.

## What leaves your machine

| Command | What is sent | Where |
|---|---|---|
| `sa stop` | A page request to each browser URL found in window titles and step text, to read the clicked element's HTML. Only when Playwright is installed. | Those websites, from a new headless Chromium without your cookies. |
| `sa analyze`, `sa pipeline --analyze` | Every screenshot in the folder. | Anthropic or OpenAI. |
| `sa agent`, `sa replay`, `sa watch` | A screenshot of the whole screen at every step and every check, **also in a dry run**. | Anthropic or OpenAI. |
| `sa video-analyze` | Up to 50 frames and the transcript. The audio too, when a Groq or OpenAI key is set. | Anthropic; Groq or OpenAI for transcription. |
| `sa pub --git`, `sa pipeline` | The output folder, as a Git commit. | Your Git remote. |
| `sa pub --github-api`, `sa pipeline --pr` | `report.md`, or a pull request. | The GitHub API. |
| MCP tools | Whatever the tool returns: reports, events, screenshots, page text. | The MCP client, and usually its model provider. |
| `psr_fetch_link` (MCP) | A page request, possibly with your browser's cookies. | The website. See below. |
| Browser toolkit, `sa download`, `sa download-profile`, `sa scrape-species` | Requests to the URLs or services you name. | Those websites. |
| `sa instagram` | Your login on first use, then the saved session. | Instagram. |

The model provider receives what is on screen. Close anything private before you run `sa agent`, `sa replay` or `sa watch`, and review a folder before `sa analyze`.

## Things that act without asking

- **`sa pipeline` pushes.** Inside a Git repository it always commits and pushes, with no dry run. Use `sa stop` and `sa pub` when you want to look first. [Details](/psr-ai/ai-analysis/#sa-pipeline).
- **`sa stop` revisits URLs.** The element HTML step opens every browser URL it finds in the recording, without a prompt. Uninstall Playwright from that environment if you do not want that.
- **`psr_fetch_link` can use your logged-in profile.** On Windows and macOS its headless fallback first tries your own Chrome or Edge profile, so pages you are logged in to are read as you. [Details](/psr-ai/mcp/#psr_fetch_link-and-your-browser-profile).
- **Desktop control.** With `--no-dry-run`, `sa agent`, `sa replay` and `sa watch` move your real mouse and type on your real keyboard. With `--no-ask` they stop asking before each action. Move the mouse to the top-left corner of the screen to abort. [Details](/psr-ai/replay-and-tests/#before-you-let-it-act).

## Local files with credentials

| File | Holds | Notes |
|---|---|---|
| `~/.psr_ai/context_token` | The pairing token of the Hidden Context Layer. | Readable only by you on macOS and Linux. `sa context token --rotate` replaces it. |
| `~/.psr_ai/browser_profile/` | The separate browser profile that `sa context browser` opens. | Holds the cookies of any site you log in to in that window. |
| `~/.psr_instagram_session.json` | The Instagram session of `sa instagram`. | Written with your default file permissions. Treat it like a password, and delete it to log out. |
| `~/psr_output/` | Recordings, screenshots and logs. | Not encrypted. |
| `claude_desktop_config.json.bak` | The previous Claude Desktop config, written by `sa mcp install`. | Contains whatever the original held. |

API keys are read from environment variables: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GROQ_API_KEY` and `GITHUB_TOKEN`. At startup `sa` also loads `~/.env` for any variable that is not already set. It never reads a `.env` in the current directory. `sa video-analyze` also reads `GROQ_API_KEY` and `OPENAI_API_KEY` from `~/.config/watch/.env` when they are not set.

For `sa instagram`, prefer the `IG_USERNAME` and `IG_PASSWORD` environment variables to `--password`, which stays in your shell history.

## Hidden Context Layer

The listener binds to `127.0.0.1` only, requires the pairing token, refuses requests from web pages and accepts events only during a recording. Secrets in headers and URLs are redacted twice, in the extension and again in the listener. [Security and limits](/psr-ai/hidden-context-layer/#security-and-limits) has the full rules and what they do not catch.

## Reporting a vulnerability

The repository has no security policy yet. Do not put the details of a vulnerability in a public issue. Open an issue at [github.com/maioio/psr-ai](https://github.com/maioio/psr-ai/issues) that asks for a private contact, without the details.
