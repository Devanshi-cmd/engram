"""Assembles the course: anchors become timestamps, checked items stay, the rest is recorded.

Writes `course.json` (what the course page reads) and `course.md` (the Markdown export: notes
with LaTeX, embedded frames, Mermaid blocks, cards and quiz with collapsible answers).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engram.course.chapters import Chapter
from engram.course.schemas import ChapterContent, ItemVerdict
from engram.timeline.anchor import Anchor, Anchors, Moment
from engram.timeline.compact import clock

LETTERS = "ABCD"


@dataclass
class Placer:
    """Resolves anchors and remembers which frames the course uses and what couldn't be placed."""

    anchors: Anchors
    frames_used: dict[str, float] = field(default_factory=dict)  # frame → first time cited
    unplaced: list[str] = field(default_factory=list)

    def place(self, item_id: str, anchor: Anchor, chapter: Chapter) -> dict[str, Any]:
        moment: Moment | None = self.anchors.locate(anchor, (chapter.start, chapter.end))
        if moment is None:
            self.unplaced.append(item_id)
            return {"t": None, "frame": None, "how": "unplaced"}
        if moment.frame:
            self.frames_used.setdefault(moment.frame, moment.t)
        return {"t": moment.t, "frame": moment.frame, "how": moment.how}

    def figure(self, frame: str, chapter: Chapter) -> dict[str, Any] | None:
        t = self.anchors.frame_time(frame.strip(), (chapter.start, chapter.end))
        if t is None:
            return None
        self.frames_used.setdefault(frame.strip(), t)
        return {"frame": frame.strip(), "t": round(t, 2)}


def item_ids(number: int, content: ChapterContent) -> tuple[list[str], list[str]]:
    """Stable ids of a chapter's (flashcards, quiz questions)."""
    return (
        [f"c{number}-card{i + 1}" for i in range(len(content.flashcards))],
        [f"c{number}-q{i + 1}" for i in range(len(content.quiz))],
    )


def build_course(
    *,
    title: str,
    lecture_title: str,
    overview: str,
    duration: float,
    chapters: list[Chapter],
    contents: dict[int, ChapterContent],
    diagram_notes: dict[int, dict[str, Any]],
    verdicts: dict[str, ItemVerdict],
    anchors: Anchors,
) -> dict[str, Any]:
    placer = Placer(anchors)
    out_chapters: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for chapter in chapters:
        content = contents[chapter.number]
        n = chapter.number
        sections = []
        for s_i, section in enumerate(content.sections, 1):
            points = [
                {"id": f"c{n}-s{s_i}-p{p_i}", "text": p.text,
                 **placer.place(f"c{n}-s{s_i}-p{p_i}", p.anchor, chapter)}
                for p_i, p in enumerate(section.points, 1)
            ]  # fmt: skip
            figure = placer.figure(section.figure, chapter) if section.figure.strip() else None
            sections.append({"heading": section.heading, "figure": figure, "points": points})
        terms = [
            {"id": f"c{n}-term{i}", "term": k.term, "definition": k.definition,
             **placer.place(f"c{n}-term{i}", k.anchor, chapter)}
            for i, k in enumerate(content.key_terms, 1)
        ]  # fmt: skip
        repaired = set(diagram_notes.get(n, {}).get("repaired", []))
        diagrams = [
            {"id": f"c{n}-d{i}", "title": d.title, "mermaid": d.mermaid, "caption": d.caption,
             "repaired": d.title in repaired, **placer.place(f"c{n}-d{i}", d.anchor, chapter)}
            for i, d in enumerate(content.diagrams, 1)
        ]  # fmt: skip
        card_ids, quiz_ids = item_ids(n, content)
        cards, quiz = [], []
        for item_id, card in zip(card_ids, content.flashcards, strict=True):
            entry = {"id": item_id, "front": card.front, "back": card.back,
                     **placer.place(item_id, card.anchor, chapter)}  # fmt: skip
            if _kept(item_id, "flashcard", entry, verdicts, removed):
                cards.append(entry)
        for item_id, q in zip(quiz_ids, content.quiz, strict=True):
            entry = {"id": item_id, "question": q.question, "options": q.options,
                     "answer": q.answer, "explanation": q.explanation, "evidence": q.evidence,
                     **placer.place(item_id, q.anchor, chapter)}  # fmt: skip
            if _kept(item_id, "quiz", entry, verdicts, removed):
                quiz.append(entry)
        out_chapters.append(
            {
                "id": f"c{n}", "number": n, "title": chapter.title, "summary": content.summary,
                "start": chapter.start, "end": chapter.end, "sections": sections,
                "key_terms": terms, "diagrams": diagrams, "flashcards": cards, "quiz": quiz,
                "corrections": [c.model_dump() for c in content.corrections],
                "dropped_diagrams": diagram_notes.get(n, {}).get("dropped", []),
            }
        )  # fmt: skip
    return {
        "title": title or lecture_title,
        "lecture_title": lecture_title,
        "overview": overview,
        "duration": duration,
        "chapters": out_chapters,
        "frames": {
            f: {"file": f"frames/{f}.jpg", "thumb": f"thumbs/{f}.jpg", "t": t}
            for f, t in sorted(placer.frames_used.items(), key=lambda x: x[1])
        },
        "removed": removed,
        "unplaced": placer.unplaced,
    }


def _kept(
    item_id: str,
    kind: str,
    entry: dict[str, Any],
    verdicts: dict[str, ItemVerdict],
    removed: list[dict[str, Any]],
) -> bool:
    verdict = verdicts.get(item_id)
    if verdict is not None and verdict.verdict == "supported":
        entry["check"] = verdict.reason
        return True
    removed.append(
        {
            "id": item_id,
            "kind": kind,
            "verdict": verdict.verdict if verdict else "unchecked",
            "reason": verdict.reason if verdict else "not checked",
            "item": entry,
        }
    )
    return False


def to_markdown(course: dict[str, Any]) -> str:
    def at(item: dict[str, Any]) -> str:
        return f" `{clock(item['t'])}`" if item.get("t") is not None else ""

    lines = [f"# {course['title']}", "", f"> {course['overview']}", ""]
    lines += [
        f"Lecture: {course['lecture_title']} · {clock(course['duration'])}",
        "",
        "## Contents",
        "",
    ]
    for c in course["chapters"]:
        lines.append(f"{c['number']}. {c['title']} · `{clock(c['start'])}`")
    for c in course["chapters"]:
        lines += [
            "",
            f"## {c['number']}. {c['title']}",
            "",
            f"*{clock(c['start'])}–{clock(c['end'])}*",
            "",
        ]
        lines += [c["summary"], ""]
        for s in c["sections"]:
            lines += [f"### {s['heading']}", ""]
            if s["figure"]:
                f = s["figure"]["frame"]
                lines += [f"![The board at {clock(s['figure']['t'])}](frames/{f}.jpg)", ""]
            lines += [f"- {p['text']}{at(p)}" for p in s["points"]]
            lines.append("")
        if c["key_terms"]:
            lines += ["### Key terms", ""]
            lines += [f"- **{k['term']}**: {k['definition']}" for k in c["key_terms"]]
            lines.append("")
        for d in c["diagrams"]:
            lines += [f"### Diagram: {d['title']}", "", "```mermaid", d["mermaid"], "```", ""]
            lines += [f"*{d['caption']}*", ""]
        if c["flashcards"]:
            lines += ["### Flashcards", ""]
            for card in c["flashcards"]:
                lines += [f"- **Q:** {card['front']}", f"  **A:** {card['back']}{at(card)}"]
            lines.append("")
        if c["quiz"]:
            lines += ["### Quiz", ""]
            for i, q in enumerate(c["quiz"], 1):
                lines.append(f"{i}. {q['question']}")
                lines += [f"   - {LETTERS[j]}) {o}" for j, o in enumerate(q["options"])]
                answer = f"{LETTERS[q['answer']]}. {q['explanation']}{at(q)}"
                lines += ["", f"   <details><summary>Answer</summary>{answer}</details>", ""]
    return "\n".join(lines).rstrip() + "\n"
