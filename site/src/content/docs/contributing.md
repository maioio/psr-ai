---
title: Contributing
description: Set up a development install, run the test suite, and work on the documentation site.
---

PSR.ai is open source under the MIT license. Bug reports, fixes and documentation changes are welcome. The repository does not have a contribution guide or a security policy yet; this page covers the basics until it does.

## Report a bug

Open an issue at [github.com/maioio/psr-ai](https://github.com/maioio/psr-ai/issues). [Troubleshooting](/psr-ai/troubleshooting/#report-a-problem) lists what to include. Check [Limitations](/psr-ai/limitations/) first: the problem may already be known.

Do not report a security problem in a public issue. See [Reporting a vulnerability](/psr-ai/privacy-and-security/#reporting-a-vulnerability).

## Set up a development install

You need Python 3.10 or newer and Git.

```bash
git clone https://github.com/maioio/psr-ai
cd psr-ai
pip install -e ".[dev]"
pytest
```

The `dev` extra adds pytest, pytest-asyncio, pytest-cov and Pillow. It is what the CI workflow installs, on Python 3.11. Add other extras when you work on the commands that need them, for example `pip install -e ".[dev,capture]"` for the browser toolkit. [Commands](/psr-ai/commands/#optional-extras) lists every extra.

## Where things are

| Path | What it holds |
|---|---|
| `src/psr_ai/cli.py` | Every `sa` command. |
| `src/psr_ai/core/` | Recorders, parser, report and GIF generators, analysis, publishing and the browser toolkit. |
| `src/psr_ai/mcp.py` | The MCP server and its tools. |
| `src/psr_ai/extension/` | The Hidden Context Layer browser extension. |
| `tests/` | The pytest suite. |
| `site/` | This documentation site. |
| `data/public-product-facts.json` | Counts and links the site shows, generated from the code. |

## Work on the documentation

The site is built with [Astro](https://astro.build/) and [Starlight](https://starlight.astro.build/). You need Node.js; the Pages workflow uses Node.js 24.

```bash
cd site
npm ci
npm run dev
```

The dev server prints a local address; the site lives under `/psr-ai/` on it.

Each docs page is a Markdown file in `site/src/content/docs/`:

- Start the file with `title` and `description` frontmatter.
- Add the page to the sidebar in `site/astro.config.mjs`.
- Link to other pages with the base path and a trailing slash, for example `/psr-ai/recording/`.
- Describe what the code does today. Check a claim against the code before you write it, and mark anything that does not exist yet as planned.

### Counts come from the code

The number of commands and MCP tools is not typed by hand. `scripts/public_surface/product_facts.py` reads it from `cli.py` and `mcp.py` and writes `data/public-product-facts.json`. It needs Python 3.11 or newer.

```bash
python scripts/public_surface/product_facts.py --apply   # after adding or removing a command or tool
python scripts/public_surface/product_facts.py --check   # fails if the file or a page is out of date
```

`--check` also reads the docs. A sentence that states a different number of commands or MCP tools than the code makes it fail.

The Pages workflow runs `--check` before it builds the site and `validate_site_build.py` after. If either fails, nothing is deployed. Commit a change to `cli.py`, `mcp.py` or `pyproject.toml` together with the regenerated facts file and any docs page it affects.

### Check the site before you open a pull request

From the repository root:

```bash
cd site
npm run build
cd ..
python scripts/public_surface/validate_site_build.py
cd site
npx playwright install chromium
npm test
```

- `validate_site_build.py` checks the built site in `site/dist`: the base path, that every internal link and asset exists, the canonical URLs, and that no stale repository URL or paid-plan wording slipped in.
- `npm test` runs Playwright on desktop and mobile viewports. Each page must load without console errors or failed requests, must not scroll sideways at 320, 360, 390 and 1280 pixels wide, and must pass an axe accessibility scan with no serious or critical issues, also at 320 pixels.

Stop any old preview server on port 4321 first. Outside CI the tests reuse a server that is already running there, and it may still serve an older build.

To look at the pages yourself, capture screenshots in light and dark mode. They are saved under `site/test-results/`:

```bash
SCREENSHOTS=1 npx playwright test tests/visual.spec.ts
```
