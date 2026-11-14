"""Chapters: Opus splits the timeline into topics; code checks every boundary.

A lecture up to two hours makes a timeline of at most about 150 KB (40,000 tokens), which
Opus reads comfortably in one call, so that's the default. Longer timelines are split into
15-minute windows: each proposes topics (map), and one more call merges them (reduce), so
the context stays small however long the lecture is.

The model's `starts_at` must be a stamp from the timeline. Code snaps it to the nearest real
line, sorts, drops chapters shorter than a minute and makes the first start at zero.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from engram.course.prompts import CHAPTERS_SYSTEM, MERGE_PROMPT, WINDOW_PROMPT
from engram.course.schemas import ChapterDraft, ChapterPlan
from engram.errors import ProviderError
from engram.timeline.compact import clock

SINGLE_PASS_CHARS = 150_000
WINDOW_SECONDS = 15 * 60
MIN_CHAPTER_SECONDS = 60
MAX_CHAPTERS = 15

STAMP = re.compile(r"^\[(?:(\d+):)?(\d{1,2}):(\d{2})\]")


class Asker(Protocol):
    def ask[T: BaseModel](self, label: str, prompt: str, *, system: str, schema: type[T]) -> T: ...


@dataclass(frozen=True)
class Entry:
    """One stamped timeline line (speech, or a frame block with its following lines)."""

    t: float
    text: str


@dataclass(frozen=True)
class Timeline:
    header: str
    entries: list[Entry]

    def render(self, entries: list[Entry] | None = None) -> str:
        body = "\n".join(e.text for e in (self.entries if entries is None else entries))
        return f"{self.header.rstrip()}\n\n{body.strip()}\n"


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    summary: str
    start: float
    end: float

    @property
    def span(self) -> str:
        return f"{clock(self.start)}–{clock(self.end)}"


def parse_stamp(text: str) -> float | None:
    m = STAMP.match(text.strip())
    if not m:
        return None
    h, mm, ss = m.groups()
    return int(h or 0) * 3600 + int(mm) * 60 + int(ss)


def split_timeline(text: str) -> Timeline:
    header: list[str] = []
    entries: list[tuple[float, list[str]]] = []
    for line in text.splitlines():
        t = parse_stamp(line)
        if t is not None:
            entries.append((t, [line]))
        elif entries:
            entries[-1][1].append(line)
        else:
            header.append(line)
    return Timeline("\n".join(header), [Entry(t, "\n".join(lines)) for t, lines in entries])


def plan_chapters(
    asker: Asker, timeline: Timeline, duration: float
) -> tuple[str, str, list[Chapter]]:
    """(cleaned title, overview, chapters)."""
    text = timeline.render()
    if len(text) <= SINGLE_PASS_CHARS:
        plan = asker.ask("chapters", text, system=CHAPTERS_SYSTEM, schema=ChapterPlan)
    else:
        plan = _map_reduce(asker, timeline, duration)
    chapters = _checked(plan.chapters, timeline, duration)
    if not chapters:
        raise ProviderError("Opus proposed no usable chapters.")
    return plan.title.strip(), plan.overview.strip(), chapters


def windows(timeline: Timeline, duration: float) -> list[list[Entry]]:
    count = max(1, round(duration / WINDOW_SECONDS))
    size = duration / count
    out: list[list[Entry]] = [[] for _ in range(count)]
    for entry in timeline.entries:
        out[min(count - 1, int(entry.t // size))].append(entry)
    return [w for w in out if w]


def _map_reduce(asker: Asker, timeline: Timeline, duration: float) -> ChapterPlan:
    parts = windows(timeline, duration)
    proposed: list[ChapterDraft] = []
    for i, part in enumerate(parts, 1):
        span = f"{clock(part[0].t)}–{clock(part[-1].t)}"
        prompt = WINDOW_PROMPT.format(
            index=i, count=len(parts), span=span, timeline=timeline.render(part)
        )
        plan = asker.ask(f"chapters part {i}", prompt, system=CHAPTERS_SYSTEM, schema=ChapterPlan)
        proposed += plan.chapters
    topics = "\n".join(f"[{d.starts_at.strip('[] ')}] {d.title}: {d.summary}" for d in proposed)
    title = timeline.header.splitlines()[0].lstrip("# ") if timeline.header else "Lecture"
    return asker.ask(
        "chapters merge",
        MERGE_PROMPT.format(title=title, topics=topics),
        system=CHAPTERS_SYSTEM,
        schema=ChapterPlan,
    )


def _checked(drafts: list[ChapterDraft], timeline: Timeline, duration: float) -> list[Chapter]:
    stamps = sorted({e.t for e in timeline.entries})
    if not stamps:
        return []
    placed: list[tuple[float, ChapterDraft]] = []
    for draft in drafts:
        t = parse_stamp(f"[{draft.starts_at.strip('[] ')}]")
        if t is None or t > duration or not draft.title.strip():
            continue
        placed.append((_nearest(stamps, t), draft))
    placed.sort(key=lambda p: p[0])
    kept: list[tuple[float, ChapterDraft]] = []
    for t, draft in placed:
        if kept and t - kept[-1][0] < MIN_CHAPTER_SECONDS:
            continue  # too short to be a chapter: it belongs to the previous one
        kept.append((t, draft))
    kept = kept[:MAX_CHAPTERS]
    if kept and duration - kept[-1][0] < MIN_CHAPTER_SECONDS and len(kept) > 1:
        kept.pop()
    return [
        Chapter(
            number=i + 1,
            title=draft.title.strip(),
            summary=draft.summary.strip(),
            start=0.0 if i == 0 else t,
            end=kept[i + 1][0] if i + 1 < len(kept) else duration,
        )
        for i, (t, draft) in enumerate(kept)
    ]


def _nearest(stamps: list[float], t: float) -> float:
    return min(stamps, key=lambda s: abs(s - t))


def chapter_entries(timeline: Timeline, chapter: Chapter) -> list[Entry]:
    last = chapter.end >= max((e.t for e in timeline.entries), default=0)
    return [e for e in timeline.entries if chapter.start <= e.t and (e.t < chapter.end or last)]
