"""One Opus call per chapter writes its notes, key terms, diagrams, flashcards and quiz.

Chapters run in parallel. Each call sees only its own slice of the timeline plus the outline
of the whole lecture (so chapters don't repeat each other), and can open frame images.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engram.course import mermaid
from engram.course.chapters import Asker, Chapter, Timeline, chapter_entries
from engram.course.prompts import CONTENT_PROMPT, CONTENT_SYSTEM, MERMAID_PROMPT, MERMAID_SYSTEM
from engram.course.schemas import ChapterContent, Diagram, MermaidFix


def counts(chapter: Chapter) -> tuple[int, int]:
    """(flashcards, quiz questions) to ask for: more for longer chapters."""
    minutes = (chapter.end - chapter.start) / 60
    return max(3, min(10, round(minutes * 0.6))), max(1, min(5, round(minutes / 3)))


def write_chapter(
    asker: Asker, course: str, chapters: list[Chapter], chapter: Chapter, timeline: Timeline
) -> ChapterContent:
    cards, questions = counts(chapter)
    outline = "; ".join(f"{c.number}. {c.title} ({c.span})" for c in chapters)
    prompt = CONTENT_PROMPT.format(
        course=course, outline=outline, number=chapter.number, title=chapter.title,
        span=chapter.span, summary=chapter.summary, cards=cards, questions=questions,
        timeline=timeline.render(chapter_entries(timeline, chapter)),
    )  # fmt: skip
    return asker.ask(
        f"chapter {chapter.number}", prompt, system=CONTENT_SYSTEM, schema=ChapterContent
    )


@dataclass
class DiagramCheck:
    kept: list[Diagram]
    repaired: list[str] = field(default_factory=list)  # titles
    dropped: list[dict[str, str]] = field(default_factory=list)  # title, error


def check_diagrams(asker: Asker, label: str, diagrams: list[Diagram]) -> DiagramCheck:
    tidied = [d.model_copy(update={"mermaid": mermaid.tidy(d.mermaid)}) for d in diagrams]
    errors = mermaid.check([d.mermaid for d in tidied])
    result = DiagramCheck(kept=[])
    for diagram, error in zip(tidied, errors, strict=True):
        if error is None:
            result.kept.append(diagram)
            continue
        fix = asker.ask(
            f"{label} diagram repair",
            MERMAID_PROMPT.format(title=diagram.title, error=error, source=diagram.mermaid),
            system=MERMAID_SYSTEM,
            schema=MermaidFix,
        )
        source = mermaid.tidy(fix.mermaid)
        second = mermaid.check([source])[0] if source else "the repair gave up"
        if second is None:
            result.kept.append(diagram.model_copy(update={"mermaid": source}))
            result.repaired.append(diagram.title)
        else:
            result.dropped.append({"title": diagram.title, "error": second[:300]})
    return result
