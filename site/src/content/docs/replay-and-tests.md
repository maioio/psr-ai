---
title: Replay and tests
description: Let Claude act on the desktop, replay a recorded session, react to the screen, and turn a session into a Playwright test.
---

These commands turn a recording, or a plain goal, into actions. `sa agent`, `sa replay` and `sa watch` drive your real mouse and keyboard. `sa testgen` and `sa heal` work with your test suite.

## Before you let it act

- **Dry run is the default.** `sa agent`, `sa replay` and `sa watch` print each planned action and skip it until you pass `--no-dry-run`.
- **Emergency stop.** Move the mouse to the top-left corner of the screen to abort a running action, or press Ctrl+C in the terminal.
- **Screenshots leave your machine, even in a dry run.** Every step, and every check in `sa watch`, sends a screenshot of your whole screen to the model provider. Close anything private first.

They need the `agent` extra and `ANTHROPIC_API_KEY`. To use OpenAI instead, add the `ai` extra, set `OPENAI_API_KEY` and pass `-p openai`.

```bash
pip install -e ".[agent]"
```

## sa agent

```bash
sa agent "Open Notepad and type hello"
sa agent "Fill in the signup form" --no-dry-run
```

`sa agent` takes a goal in plain language. At each step it captures the screen, sends the screenshot and a short summary of the last three steps to the model, and gets back one action:

| Action | What it does |
|---|---|
| `click`, `double_click` | Click at a screen position. |
| `move` | Move the mouse. |
| `type` | Type text. |
| `key` | Press a key or key combination. |
| `scroll` | Scroll. |
| `ask_user` | Ask you a question in the terminal and pass your answer back to the model. |
| `done` | The goal is reached. |

| Option | Default | Meaning |
|---|---|---|
| `-s`, `--screenshot FILE` | none | Use this image as the first screenshot instead of capturing the screen. |
| `--max-steps` | 20 | Stop after this many steps. |
| `-p`, `--provider` | `anthropic` | `anthropic` or `openai`. |
| `-m`, `--model` | `claude-sonnet-4-6` for Anthropic, `gpt-4o` for OpenAI | Use another model. |
| `--dry-run` / `--no-dry-run` | dry run | Show actions, or carry them out. |
| `--ask` / `--no-ask` | ask | Ask before each action. |
| `-o`, `--output-dir` | `~/psr_output/agent_session_<timestamp>.d/` | Where to write the session log. |

When it acts for real, it asks `Execute this action? [y/N]` before each action. Only `y` or `yes` runs it. With `--no-ask` it stops asking, except for actions the model marks as destructive or as needing confirmation. `--no-dry-run --no-ask` prints a warning that the agent runs fully on its own.

The run ends when the model reports `done`, at the step limit, on a model error, or on Ctrl+C. The exit code is 0 only when the goal was reached.

The session log folder holds a `data.json` in the [usual format](/psr-ai/output-format/), with the action, whether it ran and its result on each step, and `screenshots/step_NNN.png`.

## sa replay

```bash
sa replay checkout_session.zip
sa replay checkout_session.zip --no-dry-run --ask
```

`sa replay` reads a recorded session and asks the model to repeat it on the current screen. It builds the goal from the session title and up to 30 of its steps, and tells the model to adapt when the screen looks different. Then it runs the same loop as `sa agent`, and writes the same session log.

:::caution[The ZIP must contain data.json]
`ZIP_PATH` is a ZIP with a `data.json` inside. A Windows Steps Recorder ZIP has none, and `sa stop` writes a folder, not a ZIP. Zip the output folder of `sa stop` yourself, with `data.json` and `screenshots/` at the top level of the archive.
:::

The first screenshot in the archive's `screenshots/` folder is used as the model's first view. So step 1 is planned from the recorded screen, not your live one; later steps see the live screen.

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` / `--no-dry-run` | dry run | Show actions, or carry them out. |
| `--ask` / `--no-ask` | no ask | Ask before each action. Destructive actions are confirmed either way. |
| `--max-steps` | 60 | Stop after this many steps. |
| `--speed` | 1.0 | Scales the pause between steps. 2.0 is twice as slow. |
| `-p`, `-m` | | As in `sa agent`. |
| `-o`, `--output-dir` | | Where to write the session log. |

## sa watch

```bash
sa watch "an error dialog is visible" -a "click OK on the error dialog"
```

`sa watch` takes a screenshot at a fixed interval and asks the model whether the condition is on screen. When it is, it runs `sa agent` with the `--action` text as the goal, for up to 20 steps.

| Option | Default | Meaning |
|---|---|---|
| `-a`, `--action` | required | What to do when the condition appears. |
| `--interval` | 5.0 | Seconds between checks. |
| `--timeout` | 3600 | Stop watching after this many seconds. |
| `--max-triggers` | 1 | Stop after the action has run this many times. |
| `--dry-run` / `--no-dry-run` | dry run | In a dry run, detection is live but the action is not carried out. |
| `--ask` / `--no-ask` | no ask | Ask before each action. |
| `-p`, `-m`, `-o` | | As in `sa agent`. |

:::caution[Each check is a model call]
With the defaults, `sa watch` can send up to 720 screenshots in an hour. Raise `--interval` or lower `--timeout` to send fewer.
:::

It exits with 0 when the trigger fired `--max-triggers` times, and with 1 when the time ran out first.

## sa testgen

```bash
sa testgen psr_output/psr_20240115_143000_output
```

`sa testgen` reads `data.json` from an output folder and writes a pytest file that uses Playwright. The default file is `generated_test.py` inside that folder; `-o` writes it elsewhere. `-f playwright` is the only format.

The file has one test function per run of steps in the same program, a `base_url` fixture and a small soft-assertion helper. Each step becomes one line:

| Recorded step | Generated code |
|---|---|
| Opening or navigating, or an action that contains a URL | `page.goto(url)`, or `page.goto(base_url)` when there is no URL |
| Typing, with the text in quotes | `page.keyboard.type("...")` |
| Click with screen coordinates | `page.mouse.click(x, y)` |
| Click without coordinates | `page.get_by_text("<label>").click()` |
| Right click | `page.mouse.click(x, y, button="right")` |
| Pressing a key | `page.keyboard.press("Enter")`, or the key named in the step |
| Scroll | `page.mouse.wheel(0, 300)` |
| Anything else with a screenshot | `expect(page).to_have_screenshot()` |

Steps it cannot map become a `pass` line with a comment, or a call marked `# TODO`. Treat the file as a first draft: screen coordinates from a desktop recording rarely match a browser page, so replace them with locators.

The `base_url` fixture returns `http://localhost:3000`. Change it to your app's address. Then run the test:

```bash
pip install pytest-playwright
playwright install
pytest generated_test.py --headed
```

## sa heal

```bash
sa heal
sa heal -k checkout -s psr_output/psr_20240115_143000_output
```

`sa heal` runs pytest and prints a structured diagnosis of each failure. It does not change your code.

| Option | Meaning |
|---|---|
| `-p`, `--project DIR` | The project to test. Default the current directory. |
| `-k`, `--filter EXPR` | Passed to pytest as `-k`. |
| `-s`, `--session DIR` | An output folder. Its last recorded step is shown next to the failures. |

It exits with 0 when every test passes, and with 1 otherwise.

## sa do

```bash
sa do "take a screenshot of https://example.com" --dry-run
```

`sa do` matches a request in plain language to one of the browser toolkit or download commands, using keyword rules rather than a model. It understands many languages, including English, Hebrew, Arabic, Russian, Spanish, French, German and Chinese. `--lang` overrides the detected language.

| Option | Meaning |
|---|---|
| `--dry-run` | Print the `sa` command it chose, and run nothing. |
| `--lang` | Language code, such as `en` or `he`. |

:::caution[Known issue]
Without `--dry-run`, `sa do` tries to run the chosen command at once. With Click 8.2 or later this step fails with an error about `mix_stderr`. Use `--dry-run` and run the printed command yourself. See [Limitations](/psr-ai/limitations/).
:::
