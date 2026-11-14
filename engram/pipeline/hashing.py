"""Content hashes: what makes a stage's cached result valid or stale."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def outputs_digest(root: Path, names: tuple[str, ...]) -> str | None:
    """One hash over a stage's output files and folders, or None if any is missing."""
    h = hashlib.sha256()
    for name in names:
        path = root / name
        if path.is_dir():
            files = sorted(p for p in path.rglob("*") if p.is_file())
        elif path.is_file():
            files = [path]
        else:
            return None
        for file in files:
            h.update(str(file.relative_to(root)).encode())
            h.update(file_sha256(file).encode())
    return h.hexdigest()


def key_of(value: Any) -> str:
    """A stable hash of JSON-serialisable data."""
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
