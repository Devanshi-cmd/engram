from __future__ import annotations

from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class Word(BaseModel):
    text: str
    start: float
    end: float
    speaker: str | None = None
    logprob: float | None = None


class Segment(BaseModel):
    """A sentence-sized run of words: what the transcript view and the timeline show."""

    start: float
    end: float
    text: str
    logprob_mean: float | None = None


class Transcript(BaseModel):
    language: str | None
    words: list[Word]
    segments: list[Segment] = []
    source: str


class Transcriber(Protocol):
    name: str

    def transcribe(self, audio: Path, keyterms: list[str]) -> Transcript:
        """Timed words for the audio file. `keyterms` are spellings to prefer (names, jargon)."""
        ...

    def minutes_left(self) -> float | None:
        """Minutes of audio the account can still transcribe, or None if unknown."""
        ...
