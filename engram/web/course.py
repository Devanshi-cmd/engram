"""What the course page shows, built from a finished job's files.

Everything here is text written by models or read off lecture frames, so it is untrusted:
Markdown is rendered with raw HTML switched off, maths is passed through escaped for KaTeX
to typeset in the browser, and the templates autoescape everything else.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from markupsafe import Markup, escape

from engram.timeline.compact import clock

_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
# Maths, by Pandoc's rules: $$display$$, or $inline$ where the opening $ is followed by a
# non-space and the closing $ follows a non-space and isn't followed by a digit or letter.
# So "price in $1000s, about $5" stays text, and an escaped \$ is never maths.
MATH = re.compile(
    r"(?<!\\)\$\$(?P<display>.+?)(?<!\\)\$\$"
    r"|(?<![\\$\w])\$(?P<inline>[^\s$](?:[^$\n]*?[^\s\\$])?)\$(?![\w$])",
    re.S,
)
_HOLD = "\ue000{}\ue001"  # private-use characters: Markdown leaves them alone

JOB_ID = re.compile(r"^[0-9a-f]{12}$")
# The only job files the web app serves. Anything else in the folder stays private.
PUBLIC_FILE = re.compile(
    r"^(?:(?:frames|thumbs)/f\d{4}\.jpg|summary(?:-portrait)?\.mp4|summary\.(?:jpg|vtt))$"
)


def md(text: str, *, inline: bool = False) -> Markup:
    """Markdown → HTML. Maths becomes <span class="tex">, which the page typesets with
    KaTeX; anything else that looks like a dollar sign stays plain text."""
    held: list[str] = []

    def hold(m: re.Match[str]) -> str:
        display = m.group("display") is not None
        latex = m.group("display") if display else m.group("inline")
        css = "tex tex-display" if display else "tex"
        held.append(f'<span class="{css}">{escape(latex.strip())}</span>')
        return _HOLD.format(len(held) - 1)

    source = MATH.sub(hold, text)
    html = _MD.renderInline(source) if inline else _MD.render(source)
    for i, span in enumerate(held):
        html = html.replace(_HOLD.format(i), span)
    return Markup(html)


def stamp(t: float | None) -> str:
    return clock(t) if t is not None else ""


@dataclass(frozen=True)
class CourseFiles:
    folder: Path
    course: dict[str, Any]

    def has(self, name: str) -> bool:
        """On disk, or (for the summary videos) recorded as stored in R2."""
        videos = (self.read("summary.json") or {}).get("videos", {})
        return name in videos or (self.folder / name).exists()

    def read(self, name: str) -> dict[str, Any] | None:
        path = self.folder / name
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text())
        return data


def load(folder: Path) -> CourseFiles | None:
    path = folder / "course.json"
    if not path.exists():
        return None
    return CourseFiles(folder, json.loads(path.read_text()))


def view(files: CourseFiles, job_id: str) -> dict[str, Any]:
    """The template's context: the course with Markdown rendered and the quiz shuffled."""
    c = files.course
    chapters = []
    for ch in c["chapters"]:
        shown: set[str] = set()
        sections = []
        for s in ch["sections"]:
            figure = s["figure"]
            if figure and figure["frame"] in shown:
                figure = None  # the same board twice in a chapter: show it once
            if figure:
                shown.add(figure["frame"])
            points = [{**p, "html": md(p["text"])} for p in s["points"]]
            sections.append({"heading": s["heading"], "figure": figure, "points": points})
        chapters.append(
            {
                **ch,
                "span": f"{clock(ch['start'])}–{clock(ch['end'])}",
                "summary_html": md(ch["summary"], inline=True),
                "sections": sections,
                "key_terms": [
                    {**k, "html": md(k["definition"], inline=True)} for k in ch["key_terms"]
                ],  # fmt: skip
                "flashcards": [
                    {**f, "front_html": md(f["front"]), "back_html": md(f["back"])}
                    for f in ch["flashcards"]
                ],
                "quiz": [_quiz(q) for q in ch["quiz"]],
            }
        )
    cards = sum(len(ch["flashcards"]) for ch in chapters)
    questions = sum(len(ch["quiz"]) for ch in chapters)
    diagrams = [d | {"chapter": ch["number"]} for ch in chapters for d in ch["diagrams"]]
    return {
        "id": job_id,
        "title": c["title"],
        "lecture_title": c["lecture_title"],
        "overview": c["overview"],
        "credit": files.read("credit.json"),  # the original lecture's source and licence
        "duration": clock(c["duration"]),
        "chapters": chapters,
        "diagrams": diagrams,
        "filmstrip": [
            {"frame": f, "t": info["t"], "time": clock(info["t"])}
            for f, info in c["frames"].items()
        ],  # fmt: skip
        "counts": {
            "chapters": len(chapters),
            "cards": cards,
            "questions": questions,
            "diagrams": len(diagrams),
        },  # fmt: skip
        "removed": len(c.get("removed", [])),
        "stats": c.get("stats", {}),
        "summary_video": files.has("summary.mp4"),
        "portrait_video": files.has("summary-portrait.mp4"),
        "captions": files.has("summary.vtt"),
    }


def _quiz(q: dict[str, Any]) -> dict[str, Any]:
    """Options in a stable shuffled order, so the answer isn't always where Opus put it."""
    order = list(range(len(q["options"])))
    random.Random(q["id"]).shuffle(order)
    return {
        **q,
        "question_html": md(q["question"], inline=True),
        "options_html": [md(q["options"][i], inline=True) for i in order],
        "answer": order.index(q["answer"]),
        "explanation_html": md(q["explanation"], inline=True),
    }


@lru_cache(maxsize=16)
def transcript(folder: Path, mtime: float) -> dict[str, Any]:
    """The transcript for the live player: paragraphs of [start, end, word] triples.

    `mtime` is part of the cache key, so a re-run job is never served stale."""
    data = json.loads((folder / "transcript.json").read_text())
    words = data["words"]
    speakers: dict[str, int] = {}
    for w in words:
        if w.get("speaker"):
            speakers[w["speaker"]] = speakers.get(w["speaker"], 0) + 1
    lecturer = max(speakers, key=lambda k: speakers[k]) if speakers else None
    paragraphs: list[dict[str, Any]] = []
    for w in words:
        start = round(w["start"], 2)
        who = "student" if lecturer and w.get("speaker") not in (None, lecturer) else ""
        last = paragraphs[-1] if paragraphs else None
        if (
            last is None
            or last["who"] != who
            or start - last["t"] > 30
            or (start - last["w"][-1][1] > 1.5)
        ):
            last = {"t": start, "who": who, "w": []}
            paragraphs.append(last)
        last["w"].append([start, round(w["end"], 2), w["text"]])
    return {"paragraphs": paragraphs}
