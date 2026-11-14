"""Offline narrator for tests: a quiet tone, three words a second."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from engram.voice.models import Narration, words_from_characters

SECONDS_PER_CHAR = 0.06


class FakeNarrator:
    name = "fake-voice"

    def __init__(self) -> None:
        self.texts: list[str] = []

    def speak(self, text: str) -> tuple[bytes, Narration]:
        self.texts.append(text)
        starts = [i * SECONDS_PER_CHAR for i in range(len(text))]
        ends = [s + SECONDS_PER_CHAR for s in starts]
        duration = len(text) * SECONDS_PER_CHAR
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "voice.mp3"
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
                 "-i", f"sine=frequency=220:duration={duration:.2f}", "-af", "volume=0.05",
                 "-ar", "44100", "-b:a", "64k", str(out)],
                check=True,
            )  # fmt: skip
            audio = out.read_bytes()
        return audio, Narration(text=text, duration=duration,
                                words=words_from_characters(text, starts, ends))  # fmt: skip
