"""The summary video's script: Opus picks the story and the frames, code keeps it speakable.

Opus sees the finished course (not the timeline: the course is already the distilled
lecture) and the frames the course uses, with Haiku's description of each, and can open
their thumbnails to pick the clearest ones. Code then checks the script: unknown frames
become title cards, symbols a voice would stumble on become words, and empty scenes go.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from engram.course.chapters import Asker
from engram.course.prompts import SCRIPT_PROMPT, SCRIPT_SYSTEM, SHORTEN_PROMPT
from engram.course.schemas import Scene, SummaryScript
from engram.timeline.compact import clock

SPOKEN = {
    ":=": " becomes ", "−": " minus ", "/": " over ",
    "θ": " theta ", "α": " alpha ", "β": " beta ", "λ": " lambda ", "σ": " sigma ",
    "∂": " partial ", "∑": " sum ", "Σ": " sum ", "∇": " gradient ", "≈": " approximately ",
    "≤": " at most ", "≥": " at least ", "→": " to ", "×": " times ", "²": " squared ",
    "=": " equals ", "+": " plus ", "%": " percent", "&": " and ",
}  # fmt: skip
WORDS_PER_SECOND = 2.4  # the narrator's pace


def word_range(duration: float) -> tuple[int, int]:
    """How long the summary should be: about 12 words per lecture minute, between 60 words
    (25 s, for a short clip) and 190 (80 s, for an hour or more)."""
    high = max(60, min(190, round(duration / 60 * 12)))
    return max(40, high - 40), high


UNSPEAKABLE = re.compile(r"[$\\^_{}|<>`*#~]")


def outline(course: dict[str, Any]) -> str:
    lines = [f"# {course['title']}", course["overview"], ""]
    for c in course["chapters"]:
        lines.append(f"## {c['number']}. {c['title']} ({clock(c['start'])}–{clock(c['end'])})")
        lines.append(c["summary"])
        lines += [f"- {s['heading']}" for s in c["sections"]]
        if c["key_terms"]:
            lines.append("Key terms: " + ", ".join(k["term"] for k in c["key_terms"]))
        lines.append("")
    return "\n".join(lines)


def frame_list(course: dict[str, Any], readings: dict[str, dict[str, Any]]) -> str:
    def chapter_of(t: float) -> int:
        return next((c["number"] for c in course["chapters"] if c["start"] <= t < c["end"]), 1)

    lines = []
    for frame, info in course["frames"].items():
        r = readings.get(frame, {})
        what = " ".join(f"{r.get('title', '')}. {r.get('description', '')}".split())[:180]
        lines.append(f"{frame} · {clock(info['t'])} · ch {chapter_of(info['t'])} · {what}")
    return "\n".join(lines)


def write_script(
    asker: Asker, course: dict[str, Any], readings: dict[str, dict[str, Any]]
) -> SummaryScript:
    low, high = word_range(course["duration"])
    prompt = SCRIPT_PROMPT.format(
        low=low, high=high, seconds=round(high / WORDS_PER_SECOND),
        course=outline(course), frames=frame_list(course, readings),
    )  # fmt: skip
    script = asker.ask("summary script", prompt, system=SCRIPT_SYSTEM, schema=SummaryScript)
    script = checked(script, set(course["frames"]))
    if words(script) > high + 10:  # one revision; Opus overshot 210 words with 246
        shorten = SHORTEN_PROMPT.format(
            words=words(script), low=low, high=high,
            script=script.model_dump_json(indent=1),
        )  # fmt: skip
        script = asker.ask("summary script, shorter", shorten, system=SCRIPT_SYSTEM,
                           schema=SummaryScript)  # fmt: skip
        script = checked(script, set(course["frames"]))
    return script


def speakable(text: str) -> str:
    for symbol, words in SPOKEN.items():
        text = text.replace(symbol, words)
    text = unicodedata.normalize("NFKC", text)  # subscripts and superscripts: ⱼ → j
    text = UNSPEAKABLE.sub(" ", text)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def checked(script: SummaryScript, frames: set[str]) -> SummaryScript:
    scenes = []
    for scene in script.scenes:
        narration = speakable(scene.narration)
        if not narration:
            continue
        frame = scene.frame.strip() if scene.frame.strip() in frames else ""
        heading = " ".join(scene.heading.split())[:40]
        scenes.append(Scene(frame=frame, heading=heading, narration=narration))
    return SummaryScript(
        title=" ".join(script.title.split())[:60],
        subtitle=" ".join(script.subtitle.split())[:70],
        scenes=scenes,
    )


def words(script: SummaryScript) -> int:
    return sum(len(s.narration.split()) for s in script.scenes)
