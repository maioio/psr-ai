# PSR Fallback Asset

This directory ships a Windows shortcut (`Steps Recorder.lnk`) that points to
`%windir%\system32\psr.exe` - the built-in Windows Steps Recorder executable.

## Purpose

PSR.ai uses a 4-tier resolver to locate `psr.exe` at runtime:

| Tier | Source | Notes |
|---:|---|---|
| 1 | `PATH` lookup via `shutil.which("psr")` | Standard; works on most machines |
| 2 | `C:\Windows\System32\psr.exe` | Direct absolute path |
| 3 | `C:\Windows\SysWOW64\psr.exe` | 32-bit layer on 64-bit Windows |
| 4 | This `.lnk` shortcut (bundled) | Last resort for locked-down enterprise environments |

## How it works

Windows resolves `.lnk` files through its Shell subsystem. When `recorder.py`
detects that the resolved path has a `.lnk` extension, it sets `shell=True` on
the `subprocess.Popen` call so the OS Shell handles the shortcut lookup itself.

## Note

This shortcut does NOT embed or redistribute `psr.exe`. It is simply a pointer
to the copy that Windows already installs at `%windir%\system32\psr.exe`.
If that file is absent (e.g. stripped by Group Policy), Tier 4 will also fail
and `PSRNotFoundError` will be raised with a clear diagnostic message.
