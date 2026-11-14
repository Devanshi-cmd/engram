"""Mermaid diagrams are checked with Mermaid's own parser (Node, `tools/mermaid`) before
anyone sees them. A diagram that fails gets one repair attempt with the parser's error, and
is dropped if it still fails: a broken diagram is never shown.

The checker is the same Mermaid version the course page loads, so "parses here" means
"renders there". Install it with `npm ci --prefix tools/mermaid`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from engram.errors import EngramError

TOOL = Path(__file__).resolve().parents[2] / "tools" / "mermaid"


def checker_ready() -> bool:
    return shutil.which("node") is not None and (TOOL / "node_modules" / "mermaid").is_dir()


def check(sources: list[str]) -> list[str | None]:
    """The parser's error for each source, or None where it parses."""
    if not sources:
        return []
    if not checker_ready():
        raise EngramError(
            "The Mermaid checker isn't installed. Run `npm ci --prefix tools/mermaid`."
        )
    try:
        done = subprocess.run(
            ["node", str(TOOL / "check.mjs")], input=json.dumps(sources), capture_output=True,
            text=True, timeout=60, check=True,
        )  # fmt: skip
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise EngramError(f"The Mermaid checker failed: {str(e)[:200]}") from e
    results: list[str | None] = json.loads(done.stdout)
    return results


def tidy(source: str) -> str:
    """Strips a ```mermaid fence if the model added one."""
    s = source.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        s = s.rsplit("```", 1)[0]
    return s.strip()
