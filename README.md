# PSR.ai

> Windows Steps Recorder, reimagined as a first-class tool for AI agents and developers.

PSR.ai wraps the built-in `psr.exe` utility and turns its raw output into structured,
machine-readable documentation in one command. Three steps replace hours of manual
screenshot annotation: record, stop, publish.

---

## Why PSR.ai

`psr.exe` has existed in every Windows install since Vista. It captures every click
and keystroke as a screenshot sequence, then bundles the result into a zipped MHT
archive. Nobody uses it because the output is unusable: a binary blob with no
structure, no API, and no way to feed it to an AI.

PSR.ai fixes that. It parses the archive, extracts clean PNG screenshots, produces a
dense AI context block that fits in any LLM prompt, renders a GitHub-flavored Markdown
report, assembles an animated GIF with cursor highlighting, and can push everything to
your repository in one command.

---

## Quick Start

```powershell
# 1. Install (Python 3.10+ required, Windows only)
pip install -e .

# If `sa` is not found immediately, add the Python Scripts directory to PATH:
$env:PATH += ";$env:LOCALAPPDATA\Python\pythoncore-3.xx-64\Scripts"

# 2. Record a session (PSR runs in the background - terminal stays free)
sa start

# ... perform the workflow you want to document ...

# 3. Stop and auto-generate everything
sa stop
#   Output:
#     Parsed:      12 steps extracted
#     Data:        psr_output\psr_20240115_143000_output\data.json
#     Screenshots: psr_output\psr_20240115_143000_output\screenshots\
#     Report:      psr_output\psr_20240115_143000_output\report.md
#     GIF:         psr_output\psr_20240115_143000_output\action_replay.gif

# 4. Publish to docs/ and push to GitHub
sa pub psr_output\psr_20240115_143000_output --git
```

For GIF generation, Pillow is required:

```powershell
pip install psr-ai[gif]
```

---

## Command Reference

| Command | What it does |
|---|---|
| `sa start` | Launch PSR.exe in the background; terminal stays free |
| `sa start --maxsc 50` | Set the maximum screenshot count (default: 25) |
| `sa stop` | Stop recording; auto-parse ZIP, write report.md + action_replay.gif |
| `sa stop --no-gif` | Stop and parse; skip GIF generation |
| `sa status` | Check whether a PSR recording is currently active |
| `sa pub <dir>` | Copy session assets to docs/sessions/ |
| `sa pub <dir> --git` | Copy, then `git commit` + `git push` |
| `sa pub <dir> --git -m "msg"` | Use a custom commit message |
| `sa pub <dir> --github-api` | Push report.md via the GitHub Contents API |
| `sa --version` | Print the installed version |

---

## Output Structure

After `sa stop`, the output directory contains:

```
psr_20240115_143000_output/
|- report.md            # GitHub-flavored Markdown: AI context block + step timeline
|- data.json            # Structured JSON for LLM consumption
|- action_replay.gif    # Animated replay with cursor highlight halos
|- screenshots/
   |- step_000.png
   |- step_001.png
   |- ...
```

After `sa pub <dir> --git`, the same tree is committed to:

```
docs/sessions/psr_20240115_143000_output/
```

---

## AI Agent Workflow Integration

### What the AI Context Block contains

`report.md` opens with a fenced `text` block designed to be copy-pasted directly
into any LLM (Claude, ChatGPT, Gemini) without burning excessive tokens:

```text
Session: My Workflow | Source: recording.zip | Steps: 12
STEP 000 | 1/15/2024 14:30:00 PM | Chrome | Left-click on Submit | screenshot:yes
STEP 001 | 1/15/2024 14:30:05 PM | Chrome | Type: search query   | screenshot:yes
...
```

Each line encodes: step index, timestamp, active program, action description,
and whether a screenshot exists.

### Feeding data.json to an agent

`data.json` follows a stable schema that any agent or script can parse:

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
      "timestamp": "1/15/2024 14:30:00 PM",
      "ui_element": "button#submit",
      "program": "Chrome",
      "action_text": "Left-click on Submit",
      "screenshot": "screenshots/step_000.png",
      "click_x": 860,
      "click_y": 540
    }
  ]
}
```

Pass `data.json` to an agent as a tool result or file attachment. The agent can
reference screenshots by their relative path to load visual context on demand.

### GitHub publish workflow

```powershell
# Local Git push (most common)
sa pub psr_output\session_output --git

# GitHub Contents API (no local git clone needed)
$env:GITHUB_TOKEN  = "ghp_..."
$env:GITHUB_REPO   = "owner/repo"
$env:GITHUB_BRANCH = "main"
sa pub psr_output\session_output --github-api
```

The Contents API path commits `report.md` to
`docs/sessions/<session-name>/report.md` on the target branch.

---

## Architecture

```
src/psr_ai/
|- cli.py                # click command group: sa start / stop / status / pub
|- core/
|  |- recorder.py        # PSR.exe control with 4-tier fallback path resolver
|  |- parser.py          # ZIP auto-detect, MHT MIME parse, Base64-PNG extract
|  |- generator.py       # MarkdownGenerator + GifGenerator (cursor halos, resize)
|  |- publisher.py       # Publisher class: copy + git commit/push + GitHub API
|- utils/
   |- encoding.py        # Binary-mode MHT read with charset auto-detection
   |- paths.py           # safe_path, sanitize_filename, timestamp_filename
assets/
|- psr_fallback/
   |- psr_shortcut.lnk   # Bundled PSR shortcut for enterprise fallback (Tier 4)
```

### Pitfall mitigations built in

| Pitfall | Mitigation |
|---|---|
| MHT charset corruption (Hebrew, Chinese) | Binary-mode read + 4-codec fallback chain |
| Pillow GIF memory explosion | Resize to max 1280 px + quantize to 128 colors |
| psr.exe missing from PATH | 4-tier fallback: PATH, %SystemRoot%, %WinDir%, bundled .lnk |
| PSR output is ZIP, not raw MHT | Auto-detect ZIP, extract MHT transparently |
| Colon in timestamp crashes Windows filenames | timestamp_filename() uses YYYYMMDD_HHMMSS |

---

## Development

```powershell
# Install with all dev dependencies (includes Pillow for GIF tests)
pip install -e .[dev]

# Run the full test suite
pytest

# Verify the CLI entrypoint
sa --help
```

Tests are organized by module:

| File | Coverage |
|---|---|
| `tests/test_encoding.py` | Charset detection, BOM stripping, fallback chain |
| `tests/test_paths.py` | safe_path, sanitize_filename, timestamp_filename |
| `tests/test_recorder.py` | PSR resolver tiers, is_recording_active, start/stop |
| `tests/test_parser.py` | extract_zip, parse_mht, generate_json_meta |
| `tests/test_generator.py` | MarkdownGenerator, AI context block, TOC, path safety |
| `tests/test_gif.py` | GifGenerator, resize, cursor halo, bbox clamping |
| `tests/test_publisher.py` | Publisher copy, git automation, GitHub API paths |

---

## Installation Notes (Windows)

After `pip install -e .`, if the `sa` command is not found:

```powershell
# Session fix (current terminal only):
$env:PATH += ";$env:LOCALAPPDATA\Python\pythoncore-3.xx-64\Scripts"

# Permanent fix (all future terminals):
[Environment]::SetEnvironmentVariable(
    "PATH",
    "$env:PATH;$env:LOCALAPPDATA\Python\pythoncore-3.xx-64\Scripts",
    "User"
)
```

Replace `pythoncore-3.xx-64` with the actual folder name from your Python install.

---

## Known Constraints

- **Windows only**: `psr.exe` is a Windows-exclusive binary.
- **Group Policy**: `psr.exe` may be blocked on enterprise-managed machines.
- **GIF memory**: very long recordings should use `--maxsc` to cap the screenshot
  count at record time. Each frame is resized to 1280 px max before encoding.
- **Click coordinates**: PSR MHT files do not natively embed cursor X/Y.
  The `click_x`/`click_y` fields in `data.json` are populated only when the
  HTML metadata includes coordinate information.

---

## License

MIT License - see [LICENSE](LICENSE) for the full text.

Copyright (c) 2024 Maio Eshet

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

---

*PSR.ai is not affiliated with Microsoft. Windows Steps Recorder is a Microsoft product.*
