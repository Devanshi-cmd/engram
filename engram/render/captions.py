"""Captions from the narrator's word timings: short chunks, one at a time, never guessed."""

from __future__ import annotations

from dataclasses import dataclass

from engram.voice.models import SpokenWord

PAUSE = 0.35  # a silence this long starts a new chunk
HOLD = 0.6  # the last chunk stays this long after its last word


@dataclass(frozen=True)
class Chunk:
    words: tuple[SpokenWord, ...]
    start: float
    end: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def chunk(words: list[SpokenWord], max_chars: int, offset: float = 0.0) -> list[Chunk]:
    """Caption chunks of at most `max_chars`. Sentences (and pauses) are split first, so a
    caption never ends one sentence and starts the next; a long sentence is then cut into
    evenly sized pieces rather than full lines and an orphaned last word. Times are shifted
    by `offset` (the video's silent lead-in)."""
    sentences: list[list[SpokenWord]] = []
    for word in words:
        gap = word.start - sentences[-1][-1].end if sentences and sentences[-1] else 0.0
        if not sentences or gap > PAUSE or sentences[-1][-1].text[-1:] in ".?!":
            sentences.append([])
        sentences[-1].append(word)
    groups = [part for sentence in sentences for part in _balanced(sentence, max_chars)]
    chunks = []
    for i, group in enumerate(groups):
        end = groups[i + 1][0].start if i + 1 < len(groups) else group[-1].end + HOLD
        if end - group[-1].end > 1.0:
            end = group[-1].end + HOLD  # don't hold a caption over a long silence
        chunks.append(Chunk(tuple(group), group[0].start + offset, end + offset))
    return chunks


def _balanced(words: list[SpokenWord], max_chars: int) -> list[list[SpokenWord]]:
    length = sum(len(w.text) + 1 for w in words) - 1
    pieces = max(1, -(-length // max_chars))
    while True:
        target = length / pieces
        out: list[list[SpokenWord]] = [[]]
        size = 0
        for word in words:
            if out[-1] and size + len(word.text) > target + 3 and len(out) < pieces:
                out.append([])
                size = 0
            out[-1].append(word)
            size += len(word.text) + 1
        if all(sum(len(w.text) + 1 for w in part) - 1 <= max_chars for part in out):
            return out
        pieces += 1


def webvtt(chunks: list[Chunk]) -> str:
    def stamp(t: float) -> str:
        ms = round(t * 1000)
        return (
            f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"
        )

    cues = [f"{stamp(c.start)} --> {stamp(c.end)}\n{c.text}" for c in chunks]
    return "WEBVTT\n\n" + "\n\n".join(cues) + "\n"
