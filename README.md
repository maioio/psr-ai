# PSR.ai - StepAnalyzer

> Windows Steps Recorder, reimagined as a first-class tool for AI agents and developers.

PSR.ai wraps the built-in `psr.exe` utility and turns its raw output into structured,
AI-readable documentation. Three commands replace hours of manual screenshot annotation.

---

## Why PSR.ai

`psr.exe` has existed in every Windows install since Vista. It captures every click and
keystroke as a screenshot sequence, then bundles the result into an MHT archive.
Nobody uses it because the output is almost unusable: a zipped MHT blob with no
structure, no API, no way to feed it to an AI.

PSR.ai fixes that. It gives the output a clean format, a Markdown timeline, and a
machine-readable JSON block that AI agents can consume directly.

---

## Quick Start

```powershell
# Install (Python 3.10+ required)
pip install -e .

# If `sa` is not found after install, add the Scripts directory to your PATH:
$env:PATH += ";C:\Users\<you>\AppData\Local\Python\pythoncore-3.xx-64\Scripts"

# Record a session
sa start

# ... do your work on screen ...

# Stop and save
sa stop

# Parse the ZIP and generate Markdown documentation
sa pub psr_output\psr_20240101_120000.zip

# Also generate an animated GIF (requires: pip install psr-ai[gif])
sa pub psr_output\psr_20240101_120000.zip --gif
```

---

## Commands

| Command | Description |
|---|---|
| `sa start` | Launch PSR.exe and begin recording |
| `sa stop` | Stop the active recording and save the ZIP |
| `sa pub <zip>` | Parse the ZIP and generate Markdown + optional GIF |
| `sa pub <zip> --gif` | Include an animated GIF in the output |
| `sa pub <zip> --publish` | Push session.md to the configured GitHub repository |
| `sa --version` | Print the installed version |

---

## Output Structure

```
psr_20240101_120000_output/
|- session.md        # Markdown timeline with embedded image references
|- session.gif       # Animated walkthrough (only with --gif flag)
|- images/
   |- step_000.png
   |- step_001.png
   |- ...
```

`session.md` includes an `AI Agent Context` section: a JSON array mapping each step
index to its label and image path. Feed this block directly to any LLM.

---

## GitHub Publish

Set three environment variables, then pass `--publish`:

```powershell
$env:GITHUB_TOKEN  = "ghp_..."
$env:GITHUB_REPO   = "owner/repo"
$env:GITHUB_BRANCH = "main"       # optional, defaults to main

sa pub recording.zip --publish
```

The session Markdown is committed to `docs/sessions/<session-name>/session.md`
on the target branch.

---

## GIF Generation

GIF output is opt-in and requires Pillow:

```powershell
pip install psr-ai[gif]
sa pub recording.zip --gif
```

Frames are automatically resized to a maximum width of 1280 px and quantized
to 128 colors to keep output files under a reasonable size.

---

## Architecture

```
src/psr_ai/
|- cli.py               # click command group: sa start / stop / pub
|- core/
|  |- recorder.py       # PSR.exe control with 3-tier fallback path resolver
|  |- parser.py         # ZIP auto-unzip, MHT boundary parsing, Base64-PNG extraction
|  |- generator.py      # Markdown builder and opt-in GIF assembler
|  |- publisher.py      # Local copy and GitHub Contents API push
|- utils/
   |- encoding.py       # Binary-mode MHT read with charset auto-detection
   |- paths.py          # safe_path(), sanitize_filename(), pathlib-only helpers
```

---

## Known Constraints

- Windows only: `psr.exe` is a Windows-exclusive binary.
- `psr.exe` may be blocked by enterprise Group Policy on some managed machines.
- GIF generation loads all frames into memory; very long recordings should use
  `--maxsc` to cap the screenshot count at record time.

---

## Development

```powershell
# Install with all dev dependencies
pip install -e .[dev]

# Run tests
pytest

# Check the CLI entrypoint
sa --help
```

---

## License

MIT

---

*PSR.ai is not affiliated with Microsoft. Windows Steps Recorder is a Microsoft product.*

<!-- GitHub: https://github.com/maioio/psr-ai -->
