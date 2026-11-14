"""Groups words into sentence-sized segments. Thresholds come from Recall's measured runs."""

from __future__ import annotations

import re

from engram.speech.models import Segment, Word

MAX_SECONDS = 15.0  # a segment never runs longer than this
PAUSE_SECONDS = 1.0  # a silence this long ends a segment...
MIN_WORDS = 4  # ...unless the segment so far is a fragment like "um, which is"
FRAGMENT_PAUSE_SECONDS = 5.0  # a fragment waits this long for the rest of its sentence
_SENTENCE_END = re.compile(r"[.?!]$")


def to_segments(words: list[Word]) -> list[Segment]:
    segments: list[Segment] = []
    current: list[Word] = []

    def flush() -> None:
        if not current:
            return
        text = re.sub(r"\s+", " ", " ".join(w.text for w in current)).strip()
        logprobs = [w.logprob for w in current if w.logprob is not None]
        segments.append(
            Segment(
                start=round(current[0].start, 2),
                end=round(current[-1].end, 2),
                text=text,
                logprob_mean=round(sum(logprobs) / len(logprobs), 3) if logprobs else None,
            )
        )
        current.clear()

    for word in words:
        if current:
            pause = word.start - current[-1].end
            fragment = len(current) < MIN_WORDS and pause <= FRAGMENT_PAUSE_SECONDS
            too_long = word.end - current[0].start > MAX_SECONDS
            if (pause > PAUSE_SECONDS and not fragment) or too_long:
                flush()
        current.append(word)
        if _SENTENCE_END.search(word.text) and current[-1].end - current[0].start >= 2:
            flush()
    flush()
    return segments
