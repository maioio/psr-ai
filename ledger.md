# PSR.ai — Ledger

Persistent append-only memory. Never overwrite. Never delete entries.

## Current State
- Last session: 2026-06-18
- Completed: PSRCloudClient implementation (ee/client.py) + 18 async tests verified green + WORKFLOW_GUIDE.md written
- Next task: none pending — system is clean and documented
- Roo entry point: none

---

## 2026-06-18

- INVARIANT: ee/client.py upload() is a stub — real aiohttp impl deferred until cloud endpoint is finalized. Do not treat the stub as production code.
- INVARIANT: ee/auth.py sk-ant regex is a secret scanner guard, not a leaked key. Do not flag it in security audits.
- INVARIANT: core/downloader.py password param comes from CLI --password flag at runtime, not hardcoded. Not a vulnerability.
- QUIRK: gitleaks returns empty JSON (not null) when no findings — check for empty array before ConvertFrom-Json.
- RULED OUT: uploading .env to git — .gitignore covers .env and .env.* as of 2026-06-18.
- INVARIANT: aiohttp is a cloud-only optional extra ([cloud] in pyproject.toml) — never move to core dependencies.
- INVARIANT: PSRCloudClient must be used as async context manager (__aenter__/__aexit__) — calling upload() without entering raises RuntimeError.
- INVARIANT: All logging calls in ee/client.py pass headers through _sanitize_headers() — never log raw auth headers.
- RULED OUT: self.token as public attribute — renamed to self._token to prevent accidental logging of the value.
- QUIRK: PSR Cloud rate limit env vars are prefixed PSR_CLOUD_ (not API_) to avoid collision with other clients in the same process.
- INVARIANT: pytest-asyncio mode=STRICT (set in pyproject.toml) — all async tests need @pytest.mark.asyncio explicitly.
- RULED OUT: async def side_effect for aiohttp.ClientSession.request mock — returns coroutine, not context manager. Use side_effect=ExceptionClass(...) directly for error simulation.
- QUIRK: aiohttp.ClientSession.request must be patched at "aiohttp.ClientSession.request" not at the module level — patching the import reference does not intercept the session's bound method.
