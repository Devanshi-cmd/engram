"""Settings read from the environment, with `.env` in the project root as a fallback.

Secrets stay here and are passed only to the client that needs them. The Claude Code
subprocess never receives them (see `lectern.llm`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
_PLACEHOLDER_PREFIXES = ("your-", "change-me")


def _env(name: str) -> str | None:
    """A variable's value, or None when it's unset or still the .env.example placeholder."""
    value = os.environ.get(name, "").strip()
    if not value or value.startswith(_PLACEHOLDER_PREFIXES):
        return None
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str | None = None
    anthropic_api_key: str | None = None
    access_codes: frozenset[str] = field(default_factory=frozenset)

    @property
    def uploads_enabled(self) -> bool:
        return bool(self.access_codes)


def load_settings(env_file: Path | None = ROOT / ".env") -> Settings:
    if env_file is not None:
        load_dotenv(env_file, override=False)
    codes = _env("LECTERN_ACCESS_CODES") or ""
    return Settings(
        data_dir=Path(os.environ.get("LECTERN_DATA_DIR", ROOT / ".data")),
        elevenlabs_api_key=_env("ELEVENLABS_API_KEY"),
        elevenlabs_voice_id=_env("ELEVENLABS_VOICE_ID"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        access_codes=frozenset(c.strip() for c in codes.split(",") if c.strip()),
    )
