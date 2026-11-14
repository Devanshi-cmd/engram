"""Anchors: how every note, card and question gets its timestamp without the model making
one up.

Opus cites where an item comes from, either a frame id it saw (`{"frame": "f0044"}`) or a
few words the lecturer said (`{"quote": "keep changing theta to reduce j of theta"}`). Code
turns that into a time and a thumbnail, as Recall's `anchor.py` does, with two changes:

- quotes are matched against the words themselves, not 15-second segments, so the time is
  where the quote starts, to the word;
- a match inside the item's own chapter wins over a slightly better one elsewhere (lecturers
  repeat themselves).

A quote that doesn't match well enough falls back to the frame, if one was given. An item
that can't be placed at all is reported, never given an invented time.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from pydantic import BaseModel, Field

from engram.speech.models import Word
from engram.timeline.align import Span
from engram.timeline.text import grams, overlap

QUOTE_MATCH = 0.6  # share of the quote's trigrams that must appear where it's placed
WINDOW, STRIDE = 60, 30  # words per candidate window; windows overlap so a quote fits one


class Anchor(BaseModel):
    frame: str = Field(
        description="Id of the frame (like f0044) the item is about, or '' if it's about speech"
    )
    quote: str = Field(
        description="5-15 consecutive words copied exactly from a speech line, or ''"
    )


@dataclass(frozen=True)
class Moment:
    t: float  # seconds
    frame: str | None  # the frame on screen at that moment: the item's thumbnail
    how: str  # "quote 0.92" | "frame" | "frame (quote 0.31 too weak)"


class Anchors:
    def __init__(self, words: list[Word], spans: list[Span]) -> None:
        self.words = words
        self.spans = sorted(spans, key=lambda s: s.start)
        self._span_starts = [s.start for s in self.spans]
        self._windows = [
            (i, grams(" ".join(w.text for w in words[i : i + WINDOW])))
            for i in range(0, max(1, len(words) - WINDOW // 2), STRIDE)
        ]
        self._frame_spans: dict[str, list[Span]] = {}
        for span in self.spans:
            self._frame_spans.setdefault(span.frame, []).append(span)

    def frame_at(self, t: float) -> str | None:
        i = bisect_right(self._span_starts, t) - 1
        return self.spans[max(i, 0)].frame if self.spans else None

    def quote_time(
        self, quote: str, within: tuple[float, float] | None = None
    ) -> tuple[float, float] | None:
        """(start of the best match, match score), or None if nothing was said."""
        g = grams(quote)
        if not g or not self._windows:
            return None
        # Ties go to the earliest window: where an idea is first said is where it's taught.
        scored = [(overlap(g, wg), -i) for i, wg in self._windows]
        best = max(scored)
        if within is not None:
            lo, hi = within
            inside = [x for x in scored if lo - 5 <= self.words[-x[1]].start <= hi]
            if inside and (top := max(inside))[0] >= QUOTE_MATCH:
                best = top
        score, first = best[0], -best[1]
        # Refine to the word: the start whose next n words match the quote best. A little
        # slack (n + 2 words) breaks ties, for fillers the timeline dropped from the quote.
        n = max(1, len(quote.split()))
        last = min(len(self.words), first + WINDOW)

        def text(i: int, k: int) -> set[str]:
            return grams(" ".join(w.text for w in self.words[i : i + k]))

        options = [
            (overlap(g, text(i, n)), overlap(g, text(i, n + 2)), -i)
            for i in range(first, max(first + 1, last - n // 2))
        ]
        *_, neg = max(options)
        return self.words[-neg].start, score

    def frame_time(self, frame: str, within: tuple[float, float] | None = None) -> float | None:
        spans = self._frame_spans.get(frame)
        if not spans:
            return None
        if within is not None:
            lo, hi = within
            for span in spans:
                if span.end > lo and span.start < hi:
                    return max(span.start, lo)
        return spans[0].start

    def locate(self, anchor: Anchor, within: tuple[float, float] | None = None) -> Moment | None:
        frame = anchor.frame.strip() or None
        if frame is not None and frame not in self._frame_spans:
            frame = None  # an id the model made up, or a typo
        quoted = self.quote_time(anchor.quote, within) if anchor.quote.strip() else None
        if quoted is not None and quoted[1] >= QUOTE_MATCH:
            t, score = quoted
            return Moment(round(t, 2), frame or self.frame_at(t), f"quote {score:.2f}")
        if frame is not None:
            t_frame = self.frame_time(frame, within)
            assert t_frame is not None
            why = f" (quote {quoted[1]:.2f} too weak)" if quoted else ""
            return Moment(round(t_frame, 2), frame, f"frame{why}")
        return None
