# PSR.ai

> Record any workflow once. Let AI agents understand it forever.

![PSR.ai Demo](docs/assets/PSR_ai_v1_0_0_Demo.gif)

PSR.ai turns raw screen recordings into structured, machine-readable documentation in one command - preventing AI token bleeding, cutting API costs by up to 80%, and enabling autonomous self-healing pipelines across Windows, macOS, and Linux.

---

## Why PSR.ai

Every time you describe a workflow to an AI agent in free text, you waste tokens. PSR.ai captures the workflow visually, extracts a dense structured summary, and feeds it directly to any LLM - without burning context on narration.

| Problem | PSR.ai Solution |
|---|---|
| Describing workflows wastes tokens | Structured AI context block fits in one prompt |
| Manual screenshot annotation takes hours | `sa stop` generates everything automatically |
| Agents repeat the same mistakes | `sa heal` correlates failures with recorded steps |
| Sharing workflows exposes sensitive context | `sa review` purges private frames before publish |
| Reproducing bugs is hard to explain | `sa replay` re-executes the recording with Claude |

---

## Quick Start - 30 seconds

```bash
# Install (Python 3.10+ required)
pip install psr-ai

# Record a session - runs silently in the background
sa start

# ... perform any workflow you want to document ...

# Stop and auto-generate everything
sa stop
# Output (quiet by default - one line, ready to pipe):
#   12 steps -> psr_output/psr_20240115_143000_output/report.md

# Want the full breakdown? Ask for it:
sa --verbose stop
#   Parsed:      12 steps extracted
#   Data:        psr_output/psr_20240115_143000_output/data.json
#   Screenshots: psr_output/psr_20240115_143000_output/screenshots/
#   Report:      psr_output/psr_20240115_143000_output/report.md
#   GIF:         psr_output/psr_20240115_143000_output/action_replay.gif

# Optional: publish to GitHub
sa pub psr_output/psr_20240115_143000_output --git
```

**Optional extras:**

```bash
pip install psr-ai[gif]     # GIF generation (Pillow)
pip install psr-ai[ai]      # AI vision analysis (Anthropic/OpenAI)
pip install psr-ai[record]  # Frame de-duplication + click tracking for the
                            # Windows polling fallback (Pillow, pynput)
```

---

## Open-Core vs Enterprise Cloud

| Feature | Open-Core (Free) | Enterprise Cloud |
|---|---|---|
| Full-page screen capture | Yes | Yes |
| Structured JSON + Markdown output | Yes | Yes |
| Animated GIF replay | Yes | Yes |
| AI vision step descriptions | Yes (bring your own key) | Yes (managed) |
| Self-healing test integration | Yes | Yes |
| Playwright test generation | Yes | Yes |
| Session publish to GitHub | Yes | Yes |
| Central telemetry dashboard | - | Yes |
| Multi-tenant team scopes | - | Yes |
| Managed cloud storage | - | Yes |
| SLA + priority support | - | Yes |

Enterprise inquiries: open an issue or contact via GitHub.

---

## Platform Support

PSR.ai runs natively on all major operating systems:

| Platform | Recording Engine |
|---|---|
| Windows | `psr.exe` (built-in Steps Recorder) |
| Windows (locked down) | Local polling daemon - automatic fallback |
| macOS | `screencapture` + `osascript` |
| Linux | `gdbus` (Wayland) / `xwd` (X11) |

**Windows fallback.** When `psr.exe` is missing, disabled by Group Policy, or
raises `PermissionError`, `sa start` switches to a local polling engine without
saying a word - same command, same ZIP, same downstream pipeline. The fallback
captures with the native OS screen-copy API (Pillow `ImageGrab`, or PowerShell
`System.Drawing` when Pillow is absent), compares every frame in memory against
the previous one, and discards idle frames before they are ever written, so an
unattended session costs no disk. It runs as a detached process, so it survives
the terminal that started it and stops on `sa stop` from any shell.

All platforms produce identical output: `data.json`, `report.md`, `action_replay.gif`, and a `screenshots/` directory.

---

## Command Reference

| Command | What it does |
|---|---|
| `sa start` | Start recording; terminal stays free |
| `sa start --maxsc 100` | Frame cap (default: 100). The session stops itself on reaching it |
| `sa --verbose <cmd>` | Full narration; quiet (results only) is the default everywhere |
| `sa mcp install` | Register the MCP server with Claude Desktop |
| `sa context install` | Set up the Hidden Context Layer browser extension |
| `sa context status` | Is the context listener up? how many events captured? |
| `sa stop --clipboard` | Also capture clipboard text into data.json (redacted, opt-in) |
| `sa stop` | Stop recording; auto-generate report + GIF |
| `sa stop --no-gif` | Stop and parse; skip GIF generation |
| `sa status` | Check whether recording is active |
| `sa capture <url>` | Full-page screenshot of any URL |
| `sa capture <url> --pdf` | Full-page PDF of any URL |
| `sa analyze <dir>` | Enrich steps with AI vision descriptions |
| `sa review <dir>` | Interactively drop sensitive frames |
| `sa replay <zip>` | Re-execute a session using Claude |
| `sa pub <dir>` | Copy session to docs/sessions/ |
| `sa pub <dir> --git` | Copy + git commit + push |
| `sa pub <dir> --github-api` | Push via GitHub Contents API |
| `sa heal` | Run tests + correlate failures with session |
| `sa testgen <dir>` | Generate Playwright integration test |
| `sa pipeline <zip>` | Full parse + analyze + publish pipeline |
| `sa watch "<trigger>" --action "<action>"` | Watch screen and react automatically |
| `sa agent "<task>"` | Let Claude autonomously fix a problem |

---

## Output Structure

```
psr_20240115_143000_output/
|- report.md            # AI context block + step timeline (GitHub Markdown)
|- data.json            # Structured JSON for LLM and agent consumption
|- action_replay.gif    # Animated replay with cursor highlight halos
|- screenshots/
   |- step_000.png
   |- step_001.png
   |- ...
```

---

## AI Agent Integration

### AI Context Block

`report.md` opens with a dense context block designed to fit in any LLM prompt:

```text
Session: My Workflow | Source: recording.zip | Steps: 12
STEP 000 | 2024-01-15 14:30:00 | Chrome | Left-click on Submit | screenshot:yes
STEP 001 | 2024-01-15 14:30:05 | Chrome | Type: search query   | screenshot:yes
```

> **Note:** `timestamp`, `program`, and `action_text` fields are populated automatically by `sa analyze <dir>` (requires an Anthropic or OpenAI API key). Without `sa analyze`, sessions are still fully usable — LLMs can work directly from the screenshots.

### Feeding data.json to an agent

```json
{
  "title": "Session title",
  "source": "recording.zip",
  "generated_at": "2024-01-15T14:30:00+00:00",
  "step_count": 12,
  "steps": [
    {
      "index": 0,
      "label": "Click Submit",
      "timestamp": "2024-01-15 14:30:00",
      "program": "Chrome",
      "action_text": "Left-click on Submit",
      "screenshot": "screenshots/step_000.png"
    }
  ]
}
```

### Self-healing CI integration

```yaml
# .github/workflows/psr-self-healing.yml
- name: Run self-healing diagnostics
  run: sa heal --session psr_output/latest_output
```

---

## Architecture

```
src/psr_ai/
|- cli.py                # click command group: all sa commands
|- core/
|  |- recorder.py        # Screen recorder with multi-OS fallback resolution
|  |- parser.py          # Archive extraction and step parsing
|  |- generator.py       # MarkdownGenerator + GifGenerator
|  |- analyzer.py        # AI vision enrichment (Anthropic / OpenAI)
|  |- publisher.py       # Copy + git commit/push + GitHub API
|  |- capture.py         # Full-page URL capture (Playwright)
|  |- agent.py           # Autonomous Claude agent loop
|  |- replay.py          # Session replay engine
|  |- watcher.py         # Screen condition watcher
|  |- healer.py          # Test suite diagnostics
|  |- testgen.py         # Playwright test generation
|  |- pipeline.py        # End-to-end automation pipeline
|- ee/
|  |- client.py          # Enterprise: async cloud telemetry client
|  |- auth.py            # Enterprise: multi-tenant token authorization
|- utils/
   |- encoding.py        # Charset auto-detection with fallback chain
   |- paths.py           # Path utilities and filename sanitization
```

---

## Hidden Context Layer (browser context, no F12)

A screenshot shows *what the screen looked like*. It does not show the 500 the
page just swallowed, the `TypeError` in the console, or which button was really
clicked. The Hidden Context Layer captures all three - with no DevTools window
open and no "extension is debugging this browser" infobar.

```bash
sa context install    # opens a browser with the layer already loaded
sa start              # capture begins automatically
# ... reproduce the bug normally in that window ...
sa stop               # data.json now carries browser_context per step
```

Nothing is installed from a web store and nothing is paid for - the extension
ships inside the `psr-ai` package. Claude Code gets the same capability through
MCP (`psr_open_context_browser`, `psr_get_browser_context`, `psr_context_status`),
and any other tool can read it with `sa context events`.

What lands on each step of `data.json`:

```jsonc
"browser_context": {
  "network_failures": [
    { "method": "POST",
      "url": "https://api.shop.example.com/v1/orders?api_key=[REDACTED]",
      "status": 500,
      "response_body": "{\"error\":\"payment gateway timeout\"}" }
  ],
  "console_errors": [
    { "subtype": "uncaught_exception",
      "message": "TypeError: Cannot read properties of null (reading 'orderId')",
      "stack": "at onOrderResponse (checkout.js:118)" }
  ],
  "element": { "tag": "button", "role": "button", "name": "Place order",
               "selector": "#checkout > button.primary" }
}
```

Plus session-level OS metadata in `os_context`: the active window title always,
and the clipboard only when you ask for it with `sa stop --clipboard`.

**How it stays hidden.** It uses only observational `chrome.webRequest` events
and a `world: "MAIN"` content script - never the DevTools panel APIs (which stop
working the moment F12 closes) and never `chrome.debugger` (which raises the
yellow infobar). Full design, threat model, and the open-source prior art that
was evaluated: [docs/HIDDEN_CONTEXT_LAYER.md](docs/HIDDEN_CONTEXT_LAYER.md).

**Browser support.** Measured 2026-08: Edge 151 loads the layer automatically;
Chrome 151 silently ignores `--load-extension`, so PSR.ai picks Edge by default
and gives Chrome users a one-time manual load (`sa context install --no-launch`).
Extensions never load in headless browsers.

**Privacy.** The extension transmits nothing unless a recording session is
active. Traffic is loopback-only, bearer-token authenticated, rejected from
non-extension origins, and secrets (auth headers, cookies, API keys, JWTs) are
redacted on both sides. Turn the whole thing off per run with
`sa start --no-context`.

---

## Claude Desktop Integration (MCP)

PSR.ai ships a built-in MCP server that lets Claude Desktop call your local recording tools directly.

### 1. Install it (one command)

```bash
sa mcp install
# Installed: psr-ai -> C:\Users\you\AppData\Roaming\Claude\claude_desktop_config.json
```

The installer finds the config for your OS, resolves the interpreter of the
virtualenv you installed into, and merges the block into `mcpServers`. Servers
already in the file are left untouched, and the previous file is copied to
`claude_desktop_config.json.bak` before anything is written. Re-running it is a
no-op.

```bash
sa mcp install --dry-run        # print the block instead of writing it
sa mcp install --force          # replace an existing psr-ai entry
sa mcp install --config <path>  # write somewhere else entirely
sa mcp path                     # print the config path for this machine
```

Restart Claude Desktop afterwards.

### 2. Or add the server block by hand

| Platform | Path |
|---|---|
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

Open (or create) the file and add `psr-ai` under `mcpServers`:

```json
{
  "mcpServers": {
    "psr-ai": {
      "command": "python",
      "args": ["-m", "psr_ai.mcp"]
    }
  }
}
```

If you installed into a virtual environment, point `command` at the venv interpreter:

```json
{
  "mcpServers": {
    "psr-ai": {
      "command": "C:\\path\\to\\venv\\Scripts\\python.exe",
      "args": ["-m", "psr_ai.mcp"]
    }
  }
}
```

Alternatively, use the `sa mcp` console script if it is on your PATH:

```json
{
  "mcpServers": {
    "psr-ai": {
      "command": "sa",
      "args": ["mcp"]
    }
  }
}
```

### 3. Verify the connection

1. Restart Claude Desktop completely.
2. Open a new conversation.
3. Look for the hammer icon (tool indicator) in the chat input bar.
4. Click it and confirm you see `psr_start_recording`, `psr_stop_and_parse`, `psr_quick_snapshot`, and `psr_fetch_link` listed.

### 4. Optional: link fetching for social/video URLs

`psr_fetch_link` uses `yt-dlp` for YouTube/Instagram/TikTok content:

```bash
pip install yt-dlp
```

Generic web pages work with no extra dependencies.

---

## Development

```bash
# Install with all dev dependencies
pip install -e ".[dev,gif,ai]"

# Run the full test suite
pytest

# Verify the CLI
sa --help
```

---

## Known Constraints

- **Group Policy (Windows)**: `psr.exe` may be blocked on enterprise-managed machines. PSR.ai falls back to a bundled shortcut automatically.
- **GIF memory**: long recordings should use `--maxsc` to cap the screenshot count. Each frame is resized to 1280 px max before encoding.

---

## License

MIT - see [LICENSE](LICENSE) for the full text.

Copyright (c) 2024 Maio Eshet

---

*PSR.ai is not affiliated with Microsoft. Windows Steps Recorder is a Microsoft product.*
