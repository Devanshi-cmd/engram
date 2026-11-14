from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class SpokenWord(BaseModel):
    text: str
    start: float
    end: float
    offset: int  # character offset of the word in the narrated text


class Narration(BaseModel):
    text: str
    duration: float
    words: list[SpokenWord]


class Narrator(Protocol):
    name: str

    def speak(self, text: str) -> tuple[bytes, Narration]:
        """MP3 bytes and the timing of every word."""
        ...


def words_from_characters(text: str, starts: list[float], ends: list[float]) -> list[SpokenWord]:
    """Character timings (what ElevenLabs returns) → word timings, split on whitespace."""
    words: list[SpokenWord] = []
    i = 0
    n = min(len(text), len(starts), len(ends))
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        words.append(SpokenWord(text=text[i:j], start=starts[i], end=ends[j - 1], offset=i))
        i = j
    return words
