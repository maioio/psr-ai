# PSR Fallback Asset

This directory ships `psr_shortcut.lnk` - a Windows shortcut that points to
`%windir%\system32\psr.exe`, the built-in Windows Steps Recorder executable.

## Purpose

`recorder.py` uses a 3-tier resolver to locate `psr.exe` at runtime:

| Tier | Source | Notes |
|---:|---|---|
| 1 | `%SystemRoot%\System32\psr.exe` | Primary absolute path - works on all standard Windows installs |
| 2 | `%WinDir%\System32\psr.exe` | Secondary env-var path - handles non-standard SystemRoot configs |
| 3 | `psr_shortcut.lnk` (this file) | Last resort for locked-down enterprise environments |

A PATH lookup via `shutil.which("psr")` is attempted before all three tiers
as a fast-path for machines where the Scripts directory is correctly configured.

## How the .lnk fallback works

Windows resolves `.lnk` files through its Shell subsystem. When `recorder.py`
detects that the resolved path ends in `.lnk`, it sets `shell=True` on the
`subprocess.Popen` call so the OS Shell handles the shortcut resolution.

## Important note

`psr_shortcut.lnk` does NOT embed or redistribute `psr.exe`. It is a pointer
to the copy that Windows already installs at `%windir%\system32\psr.exe`.
If that file is absent (for example, stripped by Group Policy), Tier 3 will
also fail and `PSRNotFoundError` will be raised with a clear diagnostic message.
