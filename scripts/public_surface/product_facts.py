"""Generate or check data/public-product-facts.json, the one source of public numbers.

Every value is derived from the code, never typed by hand:
  version, license, python_min  <- pyproject.toml
  cli_commands                   <- visible @main.command/@main.group in src/psr_ai/cli.py
                                    (hidden commands and add_command aliases excluded)
  mcp_tools                      <- the _TOOLS registry in src/psr_ai/mcp.py

The code is parsed with ast, not imported, so this runs without the project's
dependencies installed.

Usage:
  python scripts/public_surface/product_facts.py           # print what would be written
  python scripts/public_surface/product_facts.py --apply   # write the JSON file
  python scripts/public_surface/product_facts.py --check   # exit 1 if the file or site prose is stale
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FACTS = ROOT / "data" / "public-product-facts.json"
CLI = ROOT / "src" / "psr_ai" / "cli.py"
MCP = ROOT / "src" / "psr_ai" / "mcp.py"
CONTENT = [ROOT / "site" / "src" / "pages", ROOT / "site" / "src" / "content" / "docs"]

REPOSITORY = "https://github.com/maioio/psr-ai"
WEBSITE = "https://maioio.github.io/psr-ai/"

# Prose patterns that state a counted fact. Each number found must equal the facts value.
CLAIMS = {
    "cli_commands": re.compile(r"\b(\d+)\s+(?:visible\s+)?(?:CLI\s+)?commands\b", re.I),
    "mcp_tools": re.compile(r"\b(\d+)\s+MCP\s+tools\b", re.I),
}


def _keyword_is_true(call: ast.Call, name: str) -> bool:
    return any(
        k.arg == name and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in call.keywords
    )


def count_cli_commands(source: str) -> int:
    """Count functions decorated with @main.command(...) or @main.group(...), not hidden."""
    count = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            call = dec if isinstance(dec, ast.Call) else None
            func = call.func if call else dec
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "main"
                and func.attr in ("command", "group")
                and not (call and _keyword_is_true(call, "hidden"))
            ):
                count += 1
    return count


def mcp_tool_names(source: str) -> list[str]:
    """Return the "name" of every dict in the module-level _TOOLS list."""
    for node in ast.parse(source).body:
        target, value = None, None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target != "_TOOLS" or not isinstance(value, ast.List):
            continue
        names = []
        for item in value.elts:
            if not isinstance(item, ast.Dict):
                continue
            for k, v in zip(item.keys, item.values):
                if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                    names.append(v.value)
        return names
    raise SystemExit("mcp.py: _TOOLS list not found")


def build() -> dict:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    python_min = re.search(r">=\s*([\d.]+)", project["requires-python"])
    license_ = project["license"]
    tools = mcp_tool_names(MCP.read_text(encoding="utf-8"))
    return {
        "name": "PSR.ai",
        "package": project["name"],
        "version": project["version"],
        "license": license_["text"] if isinstance(license_, dict) else license_,
        "python_min": python_min.group(1) if python_min else project["requires-python"],
        "cli_entry_point": next(iter(project.get("scripts", {})), "sa"),
        "cli_commands": count_cli_commands(CLI.read_text(encoding="utf-8")),
        "mcp_tools": len(tools),
        "mcp_tool_names": tools,
        "repository": REPOSITORY,
        "website": WEBSITE,
    }


def check_prose(facts: dict) -> list[str]:
    errors = []
    for folder in CONTENT:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if path.suffix not in (".astro", ".md", ".mdx"):
                continue
            text = path.read_text(encoding="utf-8")
            for key, rx in CLAIMS.items():
                for m in rx.finditer(text):
                    if int(m.group(1)) != facts[key]:
                        rel = path.relative_to(ROOT).as_posix()
                        errors.append(f"{rel}: '{m.group(0)}' but facts say {key}={facts[key]}")
    return errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="write data/public-product-facts.json")
    mode.add_argument("--check", action="store_true", help="fail if the file or site prose is stale")
    args = ap.parse_args()

    facts = build()
    rendered = json.dumps(facts, indent=2, ensure_ascii=False) + "\n"

    if args.check:
        errors = []
        if not FACTS.exists():
            errors.append(f"{FACTS.relative_to(ROOT).as_posix()} missing: run with --apply")
        elif FACTS.read_text(encoding="utf-8") != rendered:
            errors.append(f"{FACTS.relative_to(ROOT).as_posix()} is stale: run with --apply")
        errors += check_prose(facts)
        for e in errors:
            print("FAIL", e)
        if not errors:
            print(f"facts current: {', '.join(f'{k}={facts[k]}' for k in ('version', 'cli_commands', 'mcp_tools'))}")
        return 1 if errors else 0

    if args.apply:
        FACTS.parent.mkdir(parents=True, exist_ok=True)
        FACTS.write_text(rendered, encoding="utf-8")
        print(f"wrote {FACTS.relative_to(ROOT).as_posix()}")
    else:
        print(rendered, end="")
        print("(dry run: pass --apply to write)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
