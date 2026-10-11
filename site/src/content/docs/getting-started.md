---
title: Getting started
description: Install PSR.ai from source, record a workflow, and open the report an AI agent can read.
---

PSR.ai records what you do on screen and turns it into a step-by-step report, a JSON file and numbered screenshots that an AI agent can read. The command is `sa`.

## Requirements

- Python 3.10 or newer.
- Windows for recording from the command line. On macOS and Linux, `sa start` and `sa stop` do not work together yet; commands that do not record the screen are not affected. [Platforms](/psr-ai/platforms/) has the details for each system.

## Install

PSR.ai is not on PyPI yet, so install it from source.

```bash
git clone https://github.com/maioio/psr-ai
cd psr-ai
pip install -e ".[gif]"
sa --version
```

The `gif` extra adds Pillow, which renders the animated replay. The other extras are listed under [Commands](/psr-ai/commands/#optional-extras).

## Record a workflow

```bash
sa start
```

`sa start` returns immediately and prints the path of the ZIP it will write. The default is `~/psr_output/psr_<YYYYMMDD_HHMMSS>.zip`. On Windows, recording continues in the background, so you can close the terminal. On macOS and Linux, read the [platform note](/psr-ai/platforms/#macos-and-linux-recording-from-the-cli) first: the command-line flow has a known limitation there.

Do the task you want to capture, then stop:

```bash
sa stop
```

`sa stop` waits for the recording to be written, parses it and prints one line: the number of steps and the path of the report.

## Open the result

The output folder sits next to the ZIP and is named after it:

```text
psr_20240115_143000_output/
  report.md            readable report with an AI context block at the top
  data.json            every step as structured data
  action_replay.gif    animated replay (needs the gif extra)
  screenshots/
    step_001.png
    step_002.png
```

Give `report.md` or `data.json` to your agent. [Output format](/psr-ai/output-format/) describes both files.

Run `sa status` at any time to see whether a recording is active.

## Next steps

- [Recording](/psr-ai/recording/): every option of `sa start` and `sa stop`.
- [MCP server](/psr-ai/mcp/): let an MCP client such as Claude Desktop start and stop recordings itself.
- [Hidden Context Layer](/psr-ai/hidden-context-layer/): attach failed network requests and console errors from the browser to each step.
- [Privacy and security](/psr-ai/privacy-and-security/): what leaves your machine, and when.
