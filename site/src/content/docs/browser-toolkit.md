---
title: Browser toolkit
description: Commands that drive a real browser through Playwright - screenshots, PDFs, scraping, network logs, assertions, traces and test generation.
---

The browser toolkit is a set of `sa` commands that open a page in a real browser and save what they find. They are independent of recording: you can use them without ever running `sa start`.

## Install

They need Playwright, from the `capture` extra, and a browser:

```bash
pip install -e ".[capture]"
playwright install chromium
```

Commands that take `--browser` or `-b` can also use `firefox` or `webkit`; install those with `playwright install firefox webkit`.

Unless you pass `-o`, results go to a folder under `~/psr_output/`, named after the command (for example `~/psr_output/scrape/`). Run `sa <command> --help` for every option.

## Capture a page

| Command | What it does |
|---|---|
| `sa capture URL` | Full-page screenshot, or a PDF with `--pdf`. Writes `~/psr_output/capture_<url>.png` by default. |
| `sa browse URL` | Full-page screenshot plus the raw HTML. |
| `sa form URL` | Same as `browse`, for capturing a form before you fill it in. |
| `sa screenshot URL` | Screenshot with the full set of Playwright options. |
| `sa pdf URL` | PDF with layout control. |
| `sa video URL` | Record a `.webm` video of the page loading. |

```bash
sa capture https://example.com --pdf
sa capture https://example.com --crawl --max-pages 20
sa screenshot https://example.com --selector h1 --mask ".ads"
sa pdf https://example.com --landscape --format Letter
```

**capture.** `--crawl` follows the site's internal links and merges up to `--max-pages` pages (default 15) into one PDF. GitHub repositories get a fixed list of pages instead (README, commits, issues and so on). `-i`, `--intent` describes what you care about, so the most relevant pages are captured first.

**browse** and **form.** `--wait-for SELECTOR` waits for an element before capturing. `browse` also takes `--delay` (default 1.5 seconds) to let scripts settle.

**screenshot.** `--format png|jpeg`, `--quality`, `--no-full-page`, `--selector` for one element, `--mask` to cover elements with a box, `--omit-background` for a transparent PNG, `--animations allow|disabled` and `--scale device|css`.

**pdf.** `--format` (default `A4`), `--landscape`, `--no-background`, `--margin-top`, `--margin-bottom`, `--margin-left`, `--margin-right`, `--header` and `--footer` as HTML, `--page-ranges` and `--scale`.

**video.** `-b` picks the engine, and `--headed` shows the browser window while recording.

## Extract data

| Command | What it does |
|---|---|
| `sa scrape URL SELECTOR` | Save every element that matches a CSS selector as JSON. |
| `sa locate URL` | Find elements with Playwright's locators and report them. |

```bash
sa scrape https://news.ycombinator.com "tr.athing"
sa locate https://example.com --role link --name "More information"
```

`scrape` takes `--wait-for` and `--no-screenshot`. `locate` finds elements by `--css`, `--xpath`, `--role` with `--name`, `--text`, `--label`, `--placeholder`, `--alt`, `--title` or `--test-id`, and `--nth` picks one match.

## Network and runtime

| Command | What it does |
|---|---|
| `sa intercept URL` | Log every network request to JSON. `--block DOMAIN` blocks a domain and can be repeated. |
| `sa har URL` | Record the page's traffic to a HAR file, or replay one with `--play FILE`. |
| `sa events URL` | Log page errors, failed requests, service workers and console messages. |
| `sa websockets URL` | Log WebSocket frames. `--wait` sets how long to listen after load, in milliseconds (default 5000). |
| `sa cookies URL` | Show the page's cookies. `--add name=value` adds one before loading; `--clear` removes them all first. |
| `sa clock URL` | Load a page with the browser clock frozen (`--fixed-time`), moved forward (`--tick`), or with timers run for a while (`--run-for`). `--eval` runs JavaScript afterwards. |
| `sa api URL` | Send an HTTP request through Playwright's request API, without a browser page. |

`sa har` takes `--filter` to keep only matching URLs and, when replaying, `--update-missing` to go to the network for requests the file does not have.

```bash
sa api https://api.example.com/items -X post -d '{"name":"test"}'
sa api https://api.example.com/me --storage-state auth.json --assert-status 200
```

`sa api` takes `-X`, `--method` (`get`, `post`, `put`, `delete` or `patch`), `-d`, `--data` as a JSON string, `-H`, `--header "Name: Value"` (repeatable), and `--storage-state` to reuse the cookies of a saved browser login. `--assert-status` makes it exit with 1 when the status differs. `-o` saves the response as JSON.

## Check a page

| Command | What it does |
|---|---|
| `sa assert URL` | Run Playwright `expect` checks against a live page. |
| `sa accessibility URL` | A quick accessibility audit, saved as a Markdown report and a JSON list of problems. |

```bash
sa assert https://example.com --visible h1 --title-pattern "Example"
sa assert https://example.com --text h1 "Example Domain" --count p 2
```

`sa assert` checks `--visible`, `--hidden`, `--enabled`, `--disabled`, `--text SELECTOR VALUE`, `--count SELECTOR N`, `--url-pattern` and `--title-pattern`.

`sa accessibility` looks for images without alt text, inputs without labels, empty buttons and links, a missing page title, and skipped heading levels. It is a short list of checks, not a full audit.

## Tests and traces

| Command | What it does |
|---|---|
| `sa codegen URL` | Open a browser window, record what you do, and save it as a pytest file when you close the window. |
| `sa test-all TEST_FILE` | Run one Playwright test file on several engines, with one HTML report per engine. |
| `sa trace URL` | Load a page while recording a Playwright trace ZIP: network and DOM snapshots. |
| `sa trace-show TRACE_PATH` | Open a trace ZIP in the Playwright trace viewer. |
| `sa devices` | List the devices Playwright can emulate. `--filter iphone` narrows the list. |

```bash
sa codegen https://myapp.example/login -o tests/test_login.py
sa test-all tests/test_ui.py --browsers chromium,firefox
sa trace https://example.com
```

`codegen` writes `~/psr_output/codegen_test.py` by default; `-b` picks the engine and `--data-dir` saves a step log. `test-all` needs two more packages: `pip install pytest-playwright pytest-html`. Its default engines are `chromium,firefox,webkit`. A trace can also be opened at [trace.playwright.dev](https://trace.playwright.dev).

To turn a desktop recording into a test instead, see [`sa testgen`](/psr-ai/replay-and-tests/#sa-testgen).

## Plain-language shortcut

`sa do` maps a sentence to one of these commands. See [Replay and tests](/psr-ai/replay-and-tests/#sa-do).

On terminals that can print Hebrew, `sa צלם URL` is another name for `sa capture`.
