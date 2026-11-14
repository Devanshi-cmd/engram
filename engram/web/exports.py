"""Downloads: the notes as a Markdown zip, and the flashcards as an Anki import file."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Any

from markupsafe import escape

from engram.timeline.compact import clock
from engram.web.course import MATH


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "lecture"


def markdown_zip(folder: Path, course: dict[str, Any]) -> bytes:
    """`course.md` with the frames it shows, so the images work offline (Obsidian, GitHub)."""
    text = (folder / "course.md").read_text()
    frames = sorted(set(re.findall(r"\(frames/(f\d{4}\.jpg)\)", text)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        name = slug(course["title"])
        z.writestr(f"{name}/{name}.md", text)
        for frame in frames:
            z.write(folder / "frames" / frame, f"{name}/frames/{frame}")
    return buffer.getvalue()


def _anki_html(text: str) -> str:
    """Anki renders HTML and typesets \\(...\\) and \\[...\\] with MathJax."""
    out: list[str] = []
    last = 0
    for m in MATH.finditer(text):
        out.append(_plain(text[last : m.start()]))
        if m.group("display") is not None:
            out.append(f"\\[{escape(m.group('display'))}\\]")
        else:
            out.append(f"\\({escape(m.group('inline'))}\\)")
        last = m.end()
    out.append(_plain(text[last:]))
    return "".join(out).replace("\t", " ")


def _plain(text: str) -> str:
    bold = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", str(escape(text)))
    return bold.replace("\n", "<br>")


def anki(course: dict[str, Any]) -> str:
    """A tab-separated file Anki imports directly (File → Import), with the deck, HTML and
    tags set by its header. The back of each card says where in the lecture it's from."""
    deck = f"Engram::{course['title']}".replace("\t", " ")
    lines = ["#separator:tab", "#html:true", f"#deck:{deck}", "#tags column:3"]
    for ch in course["chapters"]:
        tag = f"engram ch{ch['number']}-{slug(ch['title'])}"
        for card in ch["flashcards"]:
            where = f"<br><br><small>{escape(ch['title'])}"
            if card.get("t") is not None:
                where += f" · lecture at {clock(card['t'])}"
            where += "</small>"
            lines.append(f"{_anki_html(card['front'])}\t{_anki_html(card['back'])}{where}\t{tag}")
    return "\n".join(lines) + "\n"
