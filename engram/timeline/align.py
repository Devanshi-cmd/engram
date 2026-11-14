"""Speech ↔ frame alignment, the backbone every later output cites.

Each keyframe owns the stretches of the lecture whose view it represents. Every word goes
to the frame that was on screen at the word's midpoint. The result is a list of spans,
each with one frame, a time range and the words spoken over it. The frame reader gets those
words as context (a lecturer says what they write), and notes, cards and quiz questions
cite a span's time.
"""

from __future__ import annotations

from bisect import bisect_right

from pydantic import BaseModel

from engram.speech.models import Word


class Span(BaseModel):
    frame: str
    start: float
    end: float
    first_word: int  # index into the transcript's words; first_word == end_word means silence
    end_word: int
    text: str


def align_words(
    views: list[tuple[str, float, float]], words: list[Word], duration: float
) -> list[Span]:
    """`views` are (frame id, start, end). They are sorted and stretched to tile 0..duration."""
    ordered = sorted(views, key=lambda v: v[1])
    if not ordered:
        return []
    starts = [v[1] for v in ordered]
    starts[0] = 0.0
    ends = [*starts[1:], max(duration, ordered[-1][2])]

    buckets: list[list[int]] = [[] for _ in ordered]
    for i, word in enumerate(words):
        mid = (word.start + word.end) / 2
        slot = max(0, bisect_right(starts, mid) - 1)
        buckets[slot].append(i)

    spans: list[Span] = []
    for (frame, _, _), start, end, members in zip(ordered, starts, ends, buckets, strict=True):
        if spans and spans[-1].frame == frame:  # the same frame across a view boundary
            last = spans[-1]
            last.end = end
            if members:
                if last.first_word == last.end_word:
                    last.first_word = members[0]
                last.end_word = members[-1] + 1
            continue
        first = members[0] if members else (spans[-1].end_word if spans else 0)
        spans.append(
            Span(
                frame=frame,
                start=round(start, 2),
                end=round(end, 2),
                first_word=first,
                end_word=members[-1] + 1 if members else first,
                text="",
            )
        )
    for span in spans:
        span.text = " ".join(w.text for w in words[span.first_word : span.end_word])
    return spans
