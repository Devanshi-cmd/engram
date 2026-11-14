"""A transcriber for tests and offline demos: returns scripted words, costs nothing."""

from __future__ import annotations

from pathlib import Path

from engram.speech.models import Transcript, Word


class FakeTranscriber:
    name = "fake"

    def __init__(self, words: list[Word] | None = None) -> None:
        self.words = words or [
            Word(text=t, start=i * 0.5, end=i * 0.5 + 0.4)
            for i, t in enumerate(["Today", "we", "derive", "the", "chain", "rule."])
        ]
        self.calls: list[tuple[Path, list[str]]] = []
        self.balance_minutes: float | None = None

    def transcribe(self, audio: Path, keyterms: list[str]) -> Transcript:
        self.calls.append((audio, keyterms))
        return Transcript(language="en", words=list(self.words), source=self.name)

    def minutes_left(self) -> float | None:
        return self.balance_minutes
