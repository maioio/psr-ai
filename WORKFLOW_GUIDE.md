# WORKFLOW_GUIDE.md
# Developer Manual — AI-Orchestrated Development System
# PSR.ai | Updated: 2026-06-18 | Owner: Maio Eshet

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Orchestration — Captain and Executor](#2-orchestration--captain-and-executor)
3. [The Handoff Protocol](#3-the-handoff-protocol)
4. [The Ledger System](#4-the-ledger-system)
5. [Dependency Management](#5-dependency-management)
6. [Security Protocol](#6-security-protocol)
7. [The Skill Toolbelt](#7-the-skill-toolbelt)
8. [Session Workflow — Step by Step](#8-session-workflow--step-by-step)
9. [Setup Checklist for New Projects](#9-setup-checklist-for-new-projects)
10. [Quick Reference Card](#10-quick-reference-card)

---

## 1. System Overview

This system turns two AI coding tools — Claude Code CLI and Roo Code (VS Code extension) — into a coordinated development team with shared memory, clear role boundaries, and a structured handoff protocol.

Without this system: both tools work in isolation, context is lost between sessions, and you repeat yourself constantly.

With this system: Claude Code plans and audits, Roo Code implements and verifies, and a set of plain text files (`ledger.md`, `handoff-*.md`) serve as shared memory that both agents read and write.

### The Core Principle

Every session produces two artifacts:
- **Code changes** — committed to git
- **Memory updates** — appended to `ledger.md`

A session that ends without updating `ledger.md` is incomplete.

### The Three Files That Run Everything

| File | Purpose | Who writes | Who reads |
|------|---------|-----------|----------|
| `ledger.md` | Persistent memory — decisions, invariants, quirks, current state | Both agents | Both agents at session start |
| `handoff-YYYY-MM-DD.md` | One-time context packet for agent switching | Claude Code | Roo Code once, then archived |
| `CLAUDE.md` / `.clinerules` | Role definitions and project constitution | You (once) | Both agents always |

---

## 2. Orchestration — Captain and Executor

### Role Definitions

**Claude Code (Captain)**
- Runs in the terminal (`claude` CLI)
- Owns: architecture, planning, git operations, structural analysis, security audits, cross-file search, dependency management, handoff writing
- Uses: `file-search`, `security-audit`, `arch-handoff`, `session-compact`, `api-reverse` skills
- Does NOT: perform interactive VS Code operations, see the Problems panel, review diffs visually

**Roo Code (Executor)**
- Runs inside VS Code as an extension
- Owns: line-by-line implementation, test execution in the VS Code pytest panel, visual diff review, inline error correction from the Problems panel, Boomerang subtask orchestration
- Uses: `ast-refactor`, `async-convert` skills
- Does NOT: manage git history, run cross-file structural audits, write handoff files

### Decision Matrix — Who Does What

| Task | Claude Code | Roo Code |
|------|-------------|----------|
| Read `ledger.md` at session start | ✅ | ✅ |
| Design module structure | ✅ | ❌ |
| Write `TODO [Roo]` comments | ✅ | ❌ |
| Implement a function body | ❌ | ✅ |
| Run `git commit` | ✅ | ✅ (with checkpoint) |
| Run `security-audit` | ✅ | ❌ |
| Review test output in VS Code panel | ❌ | ✅ |
| Fix a failing test interactively | ❌ | ✅ |
| Update `ledger.md` | ✅ | ✅ (append only) |
| Write `handoff-*.md` | ✅ | ❌ |
| Archive consumed `handoff-*.md` | ✅ | ✅ |
| Add optional dependency to `pyproject.toml` | ✅ | ❌ |

### The Captain Protocol (Claude Code behavior)

When you give Claude Code a task, it follows this sequence every time:

```
1. Read ledger.md → Current State block
2. Run file-search to locate relevant files
3. Form a plan — ask 1-2 sharp questions if intent is ambiguous
4. Execute: edit files, run terminal commands
5. Run security-audit before closing
6. Write TODO [Roo] if Roo handoff needed
7. Update ledger.md → Current State + any new INVARIANT/RULED OUT/QUIRK
8. Commit to git
```

Claude Code never skips steps 5 or 7. If it does, remind it:
> "You skipped the security audit and ledger update."

### When Claude Code Hands Off to Roo

Claude Code hands off to Roo when the task requires:
- Interactive diff review in VS Code
- Running tests and fixing failures iteratively in the Problems panel
- Line-by-line implementation inside a specific function (not structural)
- Boomerang orchestration across multiple VS Code modes

When handing off, Claude Code always provides:
1. **Why Roo**: one sentence explaining why VS Code context is needed
2. **Roo Prompt**: the exact text to paste into Roo's chat
3. **Updated handoff file**: `handoff-YYYY-MM-DD.md` written before you switch

---

## 3. The Handoff Protocol

### Part A — The TODO [Roo] Comment

Every task delegated to Roo is anchored by a structured comment block in the source file. This is the contract between Claude Code and Roo.

**Format (all 6 fields are mandatory):**

```python
# TODO [Roo]: <imperative verb phrase — what to implement, one sentence>
# CONTEXT: <why this exists — one sentence, no history>
# INPUTS: <parameter names and types, comma-separated, or "none">
# OUTPUTS: <return type and shape, or side-effect description>
# CONSTRAINTS: <explicit list of what NOT to touch, comma-separated>
# TESTS: <exact command Roo must run and pass before marking done>
```

**Real example from this project:**

```python
# TODO [Roo]: Review test suite in tests/test_ee_client.py and verify all 18 cases pass in VS Code
# CONTEXT: PSRCloudClient was implemented with aiohttp + backoff + _sanitize_headers; tests passed in terminal but need VS Code pytest runner verification
# INPUTS: tests/test_ee_client.py (18 tests), src/psr_ai/ee/client.py (implementation)
# OUTPUTS: all 18 tests green in VS Code pytest panel; if any fail, fix in test file only
# CONSTRAINTS: do not modify client.py, do not add dependencies beyond pytest-asyncio>=0.23, do not remove test cases
# TESTS: pytest tests/test_ee_client.py -v → 18 passed, 0 failed
```

**Rules for writing TODO [Roo] comments:**

- INPUTS must be typed. "session_dir" is wrong. "session_dir: Path" is correct.
- CONSTRAINTS must name specific files or functions. "be careful" is not a constraint.
- TESTS must be a runnable command, not a description. "make sure it works" is not a test.
- One TODO block per logical task. If two tasks are needed, write two blocks at two locations.
- Place the block immediately above the code Roo should start from. Not at the top of the file unless the task is file-wide.

**After Roo completes the task:** remove the TODO [Roo] block and replace with a normal implementation or docstring. A completed TODO left in the codebase is noise.

### Part B — The Handoff File

The handoff file is a one-time context packet written by Claude Code, read once by Roo, then archived.

**Location:** project root (`/handoff-YYYY-MM-DD.md`)
**Token limit:** 500 tokens maximum (~30 lines). Cut prose ruthlessly.
**Naming:** one per day. Second handoff same day: `handoff-YYYY-MM-DD-2.md`

**Template (copy exactly):**

```markdown
# Handoff YYYY-MM-DD

## Decisions
- <decision>: <why — one sentence>

## Files modified
- <path>: <what changed — one sentence>

## Do not re-derive
- <fact that cost significant reasoning to establish>

## Roo entry point
File: <path>:<line>
Task: <imperative verb phrase — one sentence>
Constraints: <comma-separated list>
Tests: <exact runnable command>
```

**What belongs in "Do not re-derive":**

This is the most important section. It captures facts that were hard to discover and would cost tokens to re-discover. Examples from this project:

- `async def side_effect` does NOT work for `aiohttp.ClientSession.request` mock — returns coroutine not context manager
- `aiohttp.ClientSession.request` must be patched at `"aiohttp.ClientSession.request"` not at module import level
- `auth.py` sk-ant regex is a secret scanner guard, not a leaked key — do not flag in security audits

**Archive protocol:**

After Roo reads and acts on a handoff file:

```powershell
New-Item -ItemType Directory -Force -Path ".handoffs" | Out-Null
Move-Item handoff-YYYY-MM-DD.md .handoffs\
```

Add `.handoffs/` to `.gitignore` if handoffs are personal. Commit them if the project is collaborative.

### Part C — The Roo Prompt

When you switch from Claude Code to Roo, paste this prompt structure:

```
Read handoff-YYYY-MM-DD.md in the project root first.

Task: <one sentence from the handoff entry point>

Steps:
1. <step 1>
2. <step 2>
...

Constraints:
- <constraint 1>
- <constraint 2>

See TODO [Roo] comment at <file>:<line> for full contract.
```

The prompt must reference both the handoff file AND the TODO [Roo] location. Roo reads the handoff for context and the TODO for the exact contract.

---

## 4. The Ledger System

### What the Ledger Is

`ledger.md` is a persistent, append-only memory file in the project root. It survives across sessions, across agents, and across context window resets. It is the single source of truth for decisions that must never be re-derived.

**Location:** `<project_root>/ledger.md`
**Access pattern:** read at start of every session, append at end of every session
**Mutability:** append-only. Never overwrite. Never delete entries.

### The Two-Part Structure

The ledger has two sections that serve different purposes:

**Section 1 — Current State (top of file, always updated)**

This is the "check-in / check-out" block. It answers "where did we stop?" in under 10 seconds.

```markdown
## Current State
- Last session: YYYY-MM-DD
- Completed: <what was finished this session — one sentence>
- Next task: <exact next action — specific enough to start without asking>
- Roo entry point: <file>:<line> (if Roo handoff pending)
```

**Section 2 — Historical Log (append-only, never touched after writing)**

```markdown
## YYYY-MM-DD
- RULED OUT: <approach> — <reason in one sentence>
- INVARIANT: <architectural fact that must never change>
- QUIRK: <discovered API or runtime behavior — one sentence>
```

### Entry Type Definitions

| Type | Use when | Example |
|------|---------|---------|
| `RULED OUT` | An approach was tried or considered and rejected. Prevents re-deriving the same dead end. | `RULED OUT: asyncio.Queue for pipeline backpressure — deadlocks when producer and consumer share the same event loop` |
| `INVARIANT` | A constraint that must hold forever: data shape, API contract, architectural boundary. | `INVARIANT: aiohttp is a cloud-only optional extra — never move to core dependencies` |
| `QUIRK` | Non-obvious runtime or API behavior that would cost tokens to re-discover. | `QUIRK: Anthropic API returns 529 not 429 for overload; Retry-After header is absent — use fixed 2s backoff` |

### Check-Out Protocol (end of session)

Say to Claude Code or Roo:
> "We're done for today. Update ledger.md with what we completed and what the next task is."

The agent will:
1. Update the `## Current State` block at the top
2. Append a dated entry to the historical log with any new RULED OUT / INVARIANT / QUIRK
3. Run a duplicate check before appending:
   ```powershell
   rg --line-number "RULED OUT.*<approach>" ledger.md
   ```

### Check-In Protocol (start of session)

Say to Claude Code or Roo:
> "Read ledger.md and tell me where we stopped and what the next task is."

The agent reads `## Current State` and responds immediately. No re-derivation needed.

### Full Ledger Example

```markdown
# PSR.ai — Ledger

Persistent append-only memory. Never overwrite. Never delete entries.

## Current State
- Last session: 2026-06-18
- Completed: PSRCloudClient implementation (ee/client.py) + 18 async tests verified in VS Code
- Next task: Add integration test against a real mock server (pytest-aiohttp)
- Roo entry point: none pending

---

## 2026-06-18
- INVARIANT: ee/client.py upload() was a stub — replaced with real aiohttp impl. Do not revert.
- INVARIANT: ee/auth.py sk-ant regex is a secret scanner guard, not a leaked key.
- INVARIANT: core/downloader.py password param comes from CLI --password flag, not hardcoded.
- INVARIANT: aiohttp is a cloud-only optional extra ([cloud] in pyproject.toml) — never move to core dependencies.
- INVARIANT: PSRCloudClient must be used as async context manager — calling upload() without entering raises RuntimeError.
- INVARIANT: pytest-asyncio mode=STRICT — all async tests need @pytest.mark.asyncio explicitly.
- RULED OUT: uploading .env to git — .gitignore covers .env and .env.* as of 2026-06-18.
- RULED OUT: async def side_effect for aiohttp.ClientSession.request mock — returns coroutine not context manager.
- QUIRK: gitleaks returns empty JSON (not null) when no findings — check for empty array before ConvertFrom-Json.
- QUIRK: PSR Cloud rate limit env vars are prefixed PSR_CLOUD_ to avoid collision with other clients.
- QUIRK: aiohttp.ClientSession.request must be patched at "aiohttp.ClientSession.request" not at module import level.
```

---

## 5. Dependency Management

### The Pattern

This project uses `pyproject.toml` with `setuptools`. Dependencies follow a strict tiering system:

| Tier | Location | Rule |
|------|----------|------|
| **Core** | `[project] dependencies` | Only what every user of the CLI needs. Currently only `click>=8.1`. |
| **Feature extras** | `[project.optional-dependencies]` | One extra per feature domain. Install only when that feature is used. |
| **Dev** | `[project.optional-dependencies] dev` | Test runners, coverage, linting. Never in production installs. |

### How to Add a New Dependency

**Step 1 — Determine the tier.**

Ask: "Does every user of `sa` (the CLI) need this?" If no — it's an optional extra.
Ask: "Is this only needed for testing or development?" If yes — it goes in `dev`.

**Step 2 — Identify or create the correct extra.**

Check if an existing extra already covers the domain:

```powershell
rg --line-number "optional-dependencies" pyproject.toml -A 20
```

If no existing extra fits, create a new one. Name it after the feature, not the library.
- Good: `cloud = ["aiohttp>=3.9"]`
- Bad: `aiohttp = ["aiohttp>=3.9"]`

**Step 3 — Edit `pyproject.toml`.**

Add the new extra in alphabetical order within `[project.optional-dependencies]`:

```toml
[project.optional-dependencies]
cloud   = ["aiohttp>=3.9"]       # ← new
gif     = ["Pillow>=10.0"]
ai      = ["anthropic>=0.25", "openai>=1.0"]
```

**Step 4 — Install locally and verify.**

```powershell
pip install -e ".[cloud]"
python -c "import aiohttp; print(aiohttp.__version__)"
```

**Step 5 — Update `ledger.md`.**

```markdown
- INVARIANT: aiohttp is a cloud-only optional extra ([cloud] in pyproject.toml) — never move to core dependencies.
```

**Step 6 — Commit.**

```powershell
git add pyproject.toml
git commit -m "deps: add aiohttp>=3.9 as [cloud] optional extra"
```

### Current Extra Map

| Extra | Libraries | When to install |
|-------|----------|----------------|
| `cloud` | aiohttp>=3.9 | PSR Cloud upload feature |
| `gif` | Pillow>=10.0 | GIF generation |
| `ai` | anthropic>=0.25, openai>=1.0 | AI analysis features |
| `desktop` | pynput>=1.7 | Desktop input capture |
| `agent` | pyautogui, Pillow, anthropic | Agent automation |
| `capture` | playwright>=1.40, pypdf>=4.0 | Web capture |
| `download` | yt-dlp, gallery-dl | Media download |
| `instagram` | instagrapi>=2.0 | Instagram download |
| `analyze` | faster-whisper, yt-dlp | Audio analysis |
| `full` | anthropic, faster-whisper, yt-dlp, Pillow, playwright, pynput | Everything |
| `dev` | pytest, pytest-asyncio, pytest-cov, Pillow | Development only |

### Installing for Development

```powershell
pip install -e ".[dev,cloud,ai]"
```

Never use `pip install -e ".[full]"` in development unless you specifically need all features — it pulls heavy dependencies like playwright and faster-whisper.

---

## 6. Security Protocol

### The Non-Negotiables

These rules are enforced by `security-audit` skill and verified before every commit that touches auth, API calls, or user input.

1. **No secrets in code.** All auth values come from environment variables. Never hardcoded.
2. **`_sanitize_headers()` before every log call.** Any file that logs request/response data must contain this function and wrap all header logging through it.
3. **No `.env` in git.** `.gitignore` must contain `.env` and `.env.*` (with `!.env.example` exception).
4. **No `requests` library.** All HTTP calls use `aiohttp`. `requests` is blocking IO.
5. **No `eval()`, no `shell=True`, no `pickle.loads()` on untrusted input.**

### The `_sanitize_headers()` Function

This function must be copied verbatim into every file that performs HTTP logging:

```python
_SENSITIVE_HEADERS = frozenset({
    "authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
    "x-auth-token",
    "x-session-token",
    "x-psr-token",        # project-specific — add your own here
})

def _sanitize_headers(headers: dict[str, str]) -> dict[str, str]:
    return {
        k: "***REDACTED***" if k.lower() in _SENSITIVE_HEADERS else v
        for k, v in headers.items()
    }
```

Every `logger.warning()` or `logger.error()` that touches headers must wrap them:

```python
# WRONG
logger.warning("Request headers: %s", resp.headers)

# CORRECT
logger.warning("Request headers: %s", _sanitize_headers(dict(resp.headers)))
```

### Running `security-audit`

Run before every commit that touches auth, API calls, network code, or user input:

```powershell
# Phase 1: gitleaks
gitleaks detect --source . --no-git --report-format json --report-path gl-report.json 2>$null

# Phase 2: hardcoded credentials
rg --line-number --ignore-case "api[_-]?key\s*=\s*['\`"][^'\`"]{8,}" src/ --type py -m 20

# Phase 3: structural
rg --line-number "eval\(|shell\s*=\s*True|pickle\.loads\(" src/ --type py -m 20

# Phase 4: no requests library
rg --count "^import requests|^from requests" src/ --type py

# Phase 5: async without except
rg --files-with-matches "async def" src/ --type py | ForEach-Object {
    $has = rg --count "except" $_
    if (-not $has) { Write-Output "MISSING EXCEPT: $_" }
}

# Phase 6: .env coverage
rg --line-number "^\.env" .gitignore -m 5
```

If `CRITICAL > 0`: do not commit. Fix first.
If `HIGH > 0`: flag in ledger.md and resolve before next session.

### The `.env.example` Pattern

Every module that reads from environment must ship a `.env.example`:

```
# PSRCloudClient configuration
# Copy to .env and fill in real values. Never commit .env.
PSR_CLOUD_TOKEN=your_token_here
PSR_CLOUD_RPM_LIMIT=60
PSR_CLOUD_BASE_BACKOFF_S=1.0
PSR_CLOUD_MAX_BACKOFF_S=32.0
PSR_CLOUD_MAX_RETRIES=3
```

Commit `.env.example`. Never commit `.env`.

---

## 7. The Skill Toolbelt

Seven skills live at `C:\Users\User\.roo\skills\` and `C:\Users\User\.roo\skills-code\`. They are loaded by Roo automatically when the task matches the skill description.

### Skill Map

| Skill | Location | Trigger |
|-------|----------|---------|
| `arch-handoff` | `skills/` (global) | Planning phase done, need to write TODO [Roo] |
| `file-search` | `skills/` (global) | Before any file read or edit |
| `security-audit` | `skills/` (global) | Before any commit touching auth/API/input |
| `session-compact` | `skills/` (global) | End of session, context >55%, agent switch |
| `api-reverse` | `skills/` (global) | HAR analysis, generating async clients |
| `ast-refactor` | `skills-code/` (Code mode) | Structural rename, extraction, Canvas patterns |
| `async-convert` | `skills-code/` (Code mode) | Converting blocking IO to async |

### Composability Chain

The skills are designed to chain:

```
file-search → locate targets
    ↓
ast-refactor → identify structural changes
    ↓
arch-handoff → write TODO [Roo] contracts
    ↓
async-convert → convert blocking → async (Roo)
    ↓
security-audit → verify no vulnerabilities introduced
    ↓
session-compact → write handoff.md + update ledger.md
```

### Invoking Skills in Claude Code

Claude Code invokes skills automatically when the task matches. You can also invoke explicitly:

> "Run security-audit on the current state."
> "Use arch-handoff to write a TODO for Roo on ee/client.py line 46."
> "Use session-compact to close this session."

### Invoking Skills in Roo

Roo loads skills automatically via the Progressive Disclosure system. Skills in `skills/` are available in all modes. Skills in `skills-code/` are available only in Code mode.

To invoke explicitly in Roo, type the skill name as a slash command:
```
/ast-refactor
/async-convert
```

---

## 8. Session Workflow — Step by Step

### Starting a Session (Check-In)

**Step 1 — Open the terminal and run Claude Code:**
```powershell
claude
```

**Step 2 — Check in:**
```
Read ledger.md and tell me where we stopped and what the next task is.
```

Claude Code reads `## Current State` and reports. If no `Current State` block exists, it reads the last dated entry.

**Step 3 — Confirm or redirect:**
Either confirm the suggested next task, or give a new instruction. Claude Code runs `file-search` on the relevant files before touching anything.

### During a Session

**Claude Code's autonomous loop:**
1. `file-search` — locate files
2. Read specific sections (never whole files)
3. Plan → ask 1-2 questions if ambiguous
4. Execute changes
5. Run verification (tests or `security-audit`)
6. Update `ledger.md`
7. `git commit`

**Switching to Roo mid-session:**
1. Claude Code writes `TODO [Roo]` comment in the source file
2. Claude Code writes `handoff-YYYY-MM-DD.md`
3. Claude Code provides the exact Roo prompt
4. You copy the prompt, open Roo in VS Code, paste
5. Roo reads the handoff file first, then follows the TODO contract

### Closing a Session (Check-Out)

Say to whichever agent is active:
```
We're done for today. Update ledger.md Current State and commit everything.
```

The agent will:
1. Update `## Current State` with today's date, what was completed, and the exact next task
2. Append any new INVARIANT / RULED OUT / QUIRK to the dated log
3. Archive any consumed handoff files to `.handoffs/`
4. Run `security-audit` if any code was changed
5. `git commit -am "session close YYYY-MM-DD"`

### The Git Checkpoint Rule

**Before any code-modifying operation:**
```powershell
git status
git commit -am "pre-refactor checkpoint"
```

If no git repo:
```powershell
git init
git add .
git commit -m "initial commit"
```

**On failure after a refactor:**
```powershell
git reset --hard HEAD
```

Never leave the codebase in a broken state between sessions.

### Commit Message Convention

| Prefix | Use for |
|--------|---------|
| `feat:` | New feature or capability |
| `test:` | New or modified tests |
| `deps:` | Dependency changes |
| `refactor:` | Code restructuring without behavior change |
| `fix:` | Bug fix |
| `audit:` | Security audit results |
| `session close` | End-of-session commit |
| `pre-refactor checkpoint` | Safety checkpoint before structural change |

---

## 9. Setup Checklist for New Projects

Use this checklist every time you start a new project that should use this system.

### Phase 1 — Project Structure

```
[ ] git init (if not already a repo)
[ ] Create src/<package>/ structure
[ ] Create tests/ directory with __init__.py
[ ] Create pyproject.toml with setuptools backend
```

**Minimum `pyproject.toml`:**
```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "<project-name>"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = []   # core only — keep empty until something is truly required

[project.optional-dependencies]
dev = ["pytest>=8.0", "pytest-asyncio>=0.23", "pytest-cov>=5.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-v --tb=short"
asyncio_mode = "strict"

[tool.setuptools.packages.find]
where = ["src"]
```

### Phase 2 — Security Baseline

```
[ ] Create .gitignore with:
    .env
    .env.*
    !.env.example
    __pycache__/
    *.pyc
    .pytest_cache/
    .handoffs/
    *.egg-info/

[ ] Create .env.example (empty template, commit this)
[ ] Verify gitleaks is installed: gitleaks version
[ ] Run initial security-audit to establish baseline
```

### Phase 3 — Memory System

```
[ ] Create ledger.md in project root with this header:
```

```markdown
# <Project Name> — Ledger

Persistent append-only memory. Never overwrite. Never delete entries.

## Current State
- Last session: YYYY-MM-DD
- Completed: Project initialized
- Next task: <first real task>
- Roo entry point: none

---
```

```
[ ] Create .handoffs/ directory (git-ignored)
```

### Phase 4 — Agent Configuration

```
[ ] Create CLAUDE.md in project root (Claude Code reads this)
```

**Minimum `CLAUDE.md`:**
```markdown
# CLAUDE.md — Project Constitution

## Role
Claude Code is the Captain (architect, planner, auditor).
Roo Code is the Executor (implementer, tester).

## Workflow
1. Read ledger.md at session start
2. Run file-search before touching any file
3. Run security-audit before finishing any session
4. Write TODO [Roo] + handoff file before switching to Roo
5. Update ledger.md Current State at session end

## Stack
- Language: <Python / JS / other>
- Test runner: pytest (asyncio_mode=strict)
- HTTP: aiohttp (never requests)
- Secrets: environment variables only

## Hard Rules
- Never commit .env
- Never hardcode auth values
- Never use requests library
- Always _sanitize_headers() before logging
```

```
[ ] Create .clinerules in project root (Roo reads this)
```

**Minimum `.clinerules`:**
```markdown
# .clinerules

You are the Executor. Claude Code is the Captain.

## Your job
Implement what the TODO [Roo] comments specify.
Read handoff-*.md before starting any task.
Append to ledger.md when done — never overwrite.
Run the TESTS command from the TODO block before marking done.

## Hard rules
- Follow CONSTRAINTS exactly — named files/functions are off-limits
- Do not modify files not mentioned in the TODO block
- Commit after every completed TODO with: git commit -am "roo: <task>"
```

### Phase 5 — Skills Activation

```
[ ] Verify skills are in place:
```

```powershell
Get-ChildItem "C:\Users\User\.roo\skills\" -Directory | Select-Object Name
Get-ChildItem "C:\Users\User\.roo\skills-code\" -Directory | Select-Object Name
```

Expected output:
```
arch-handoff
file-search
security-audit
session-compact
api-reverse

ast-refactor
async-convert
```

```
[ ] If any are missing, recreate from the skill content in the session that built them
[ ] Skills are global — they work across all projects automatically
```

### Phase 6 — First Commit

```powershell
git add .
git commit -m "chore: project initialization with AI orchestration system"
```

### Phase 7 — Smoke Test

```
[ ] Claude Code can read ledger.md:
    "Read ledger.md and tell me the current state."

[ ] Claude Code can run file-search:
    "Run file-search to show me the project structure."

[ ] Security baseline is clean:
    "Run security-audit on the project."

[ ] Roo can read .clinerules:
    Open Roo in VS Code, type: "What are your operating rules for this project?"
```

---

## 10. Quick Reference Card

### Daily Rituals

**Check-In (start of session):**
```
Read ledger.md and tell me where we stopped.
```

**Check-Out (end of session):**
```
We're done. Update ledger.md Current State and commit.
```

**Switch to Roo:**
```
Claude Code writes TODO [Roo] + handoff file → you paste Roo prompt → Roo reads handoff
```

**Switch back to Claude Code:**
```
Roo archives handoff → you return to terminal → Claude Code reads ledger.md
```

### Key File Locations

| File | Path |
|------|------|
| Ledger | `<project_root>/ledger.md` |
| Active handoff | `<project_root>/handoff-YYYY-MM-DD.md` |
| Archived handoffs | `<project_root>/.handoffs/` |
| Project constitution | `<project_root>/CLAUDE.md` |
| Roo rules | `<project_root>/.clinerules` |
| Global skills (all modes) | `C:\Users\User\.roo\skills\` |
| Code-mode skills | `C:\Users\User\.roo\skills-code\` |

### Emergency Commands

**Roll back a failed refactor:**
```powershell
git reset --hard HEAD
```

**Check what changed this session:**
```powershell
git diff HEAD~1
```

**Verify no secrets in codebase:**
```powershell
gitleaks detect --source . --no-git 2>$null
```

**Check which tests are failing:**
```powershell
python -m pytest tests/ -v --tb=short 2>&1 | Select-String "FAILED|ERROR|passed|failed"
```

**Find all pending Roo tasks:**
```powershell
rg --line-number "TODO \[Roo\]" src/ --type py -m 20
```

**See ledger Current State:**
```powershell
Get-Content ledger.md | Select-Object -First 10
```

### The TODO [Roo] Template

```python
# TODO [Roo]: <imperative verb — what to build>
# CONTEXT: <why this exists — one sentence>
# INPUTS: <param: Type, param: Type>
# OUTPUTS: <return type and shape>
# CONSTRAINTS: <file1.py, function_name(), do not add imports>
# TESTS: <exact command> → <expected result>
```

### The Ledger Entry Template

```markdown
## YYYY-MM-DD
- RULED OUT: <approach> — <reason>
- INVARIANT: <fact that must never change>
- QUIRK: <non-obvious behavior>
```

### The Handoff File Template

```markdown
# Handoff YYYY-MM-DD

## Decisions
- <decision>: <why>

## Files modified
- <path>: <what changed>

## Do not re-derive
- <hard-won fact>

## Roo entry point
File: <path>:<line>
Task: <imperative phrase>
Constraints: <comma-separated>
Tests: <command> → <expected>
```

---

*This document is the operating procedure. When in doubt, come back here.*
*Last updated: 2026-06-18 | Owner: Maio Eshet*
