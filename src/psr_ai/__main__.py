"""Module entry point: `python -m psr_ai` is equivalent to the `sa` script.

Used by the detached recording daemon and by the MCP server block written by
`sa mcp install`, both of which need an invocation that works without relying
on the console script being on PATH.
"""

from __future__ import annotations

from psr_ai.cli import main

if __name__ == "__main__":
    main()
