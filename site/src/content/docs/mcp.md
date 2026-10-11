---
title: MCP server
description: Run sa mcp so an MCP client such as Claude Desktop can start recordings, read browser context and take snapshots itself.
---

`sa mcp` runs a Model Context Protocol server over standard input and output. An MCP client launches it and calls its tools: start and stop a recording, take a screenshot, open the context browser, read captured browser events, and fetch a link. It exposes 7 MCP tools.

The server speaks JSON-RPC 2.0, one message per line, and reports MCP protocol version `2024-11-05`. It has no network port.

## Add it to Claude Desktop

```bash
sa mcp install
```

This finds the Claude Desktop config file for your system, adds a `psr-ai` entry under `mcpServers`, and leaves every other server in the file as it was. The entry points to the absolute path of `sa` in the environment you ran the command from, so a virtualenv keeps working even when it is not on Claude Desktop's `PATH`. Restart Claude Desktop afterwards.

| Option | What it does |
|---|---|
| `--dry-run` | Print the block that would be written, and change nothing. |
| `--force` | Replace an existing entry with the same name. Without it, an existing entry is an error. |
| `--name NAME` | Register under another key. Default `psr-ai`. |
| `--config PATH` | Edit this file instead of the default location. |

Before writing, the old file is copied to `claude_desktop_config.json.bak`. The command refuses to touch a file that is not valid JSON, or whose `mcpServers` is not an object.

`sa mcp path` prints the config path it would use:

| System | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json`, or `~/.config/Claude/claude_desktop_config.json` |

### Other MCP clients

Any client that launches stdio servers can run PSR.ai. Use `sa` as the command and `mcp` as its argument, with the absolute path to `sa` if the client does not share your shell's `PATH`:

```json
{
  "mcpServers": {
    "psr-ai": {
      "command": "/path/to/your/venv/bin/sa",
      "args": ["mcp"]
    }
  }
}
```

`python -m psr_ai.mcp` starts the same server.

### Test it by hand

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' | sa mcp
```

A JSON line with `serverInfo` means the server works. Close input or press Ctrl-C to stop it.

## Tools

| Tool | Inputs | What it does |
|---|---|---|
| `psr_start_recording` | `output_path`, `max_screenshots` (default 100) | Starts a recording, like `sa start`. |
| `psr_stop_and_parse` | none | Stops the recording, waits up to 10 seconds for the ZIP, writes `report.md` and `data.json`, and returns the report text. |
| `psr_quick_snapshot` | `max_px` (default 1280), `colors` (2 to 256, default 128) | Takes one screenshot, shrinks and quantizes it, and returns it as a Base64 PNG. Needs the `gif` extra. |
| `psr_open_context_browser` | `browser`, `url` | Opens a browser with the [Hidden Context Layer](/psr-ai/hidden-context-layer/), like `sa context browser`. |
| `psr_get_browser_context` | `kind`, `limit` (default 50), `session_dir` | Returns the browser events captured so far, as JSON. |
| `psr_context_status` | none | Reports whether the listener runs, which session it serves, how many events it holds, and which browsers are available. |
| `psr_fetch_link` | `url` (required), `max_chars` (default 8000) | Returns the text of a web page. See [the note below](#psr_fetch_link-and-your-browser-profile). |

A typical debugging run: `psr_open_context_browser`, then `psr_start_recording`, then the user reproduces the problem, then `psr_get_browser_context` and `psr_stop_and_parse`.

The pairing step from [Hidden Context Layer](/psr-ai/hidden-context-layer/#pair-the-extension) still applies: the extension sends nothing until its token is set.

## Things to know

:::caution[macOS and Linux]
`psr_stop_and_parse` does not find a recording started by `psr_start_recording` on macOS and Linux, for the same reason as `sa stop`. See [Platforms](/psr-ai/platforms/#macos-and-linux-recording-from-the-cli).
:::

### psr_fetch_link and your browser profile

`psr_fetch_link` works in up to three stages:

1. **YouTube, Instagram and TikTok links** go to `yt-dlp`, if it is installed. It returns the title, uploader, description and URL. It does not return a transcript.
2. **Other links** are fetched directly and stripped to plain text, up to 512 KB of HTML.
3. **Fallback.** If that returns little text (under 200 characters), looks like a login wall, gets `401`, `403` or `429`, or `yt-dlp` failed, the page is opened again in a headless Chromium through Playwright, when Playwright is installed.

:::caution[The fallback can use your logged-in browser profile]
On Windows and macOS, the headless fallback first tries to open your own Chrome profile, or Edge's if Chrome has none, with its cookies. Pages you are logged in to can then be read as you. If that browser is open, its profile is locked and an isolated profile is used instead. Do not let an agent call `psr_fetch_link` on links you would not open yourself.
:::

### Snapshots

`psr_quick_snapshot` uses Pillow on Windows, `screencapture` on macOS, and `gnome-screenshot`, `scrot` or `maim` on Linux. It returns the image to the client; nothing is written to the output folder.
