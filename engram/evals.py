"""`engram eval`: Engram's quality, measured against hand-made answer keys.

Three numbers, each with a method a reader can check:

- **Board states found (keyframe recall).** A person marked, on pictures every 5 s, the
  moment each board was most complete. A state counts as found if a keyframe was taken
  from it (between the previous state and up to 10 s after this one), or if a later
  keyframe's view includes it: consolidation's claim that a fuller frame of the same board
  replaced it. Both are reported, plus keyframes that match no state (redundant frames).
- **Handwriting character error rate (CER).** A person typed what's on 20 boards, without
  seeing Engram's readings. CER is the edit distance between Haiku's reading and theirs,
  over the length of theirs, after normalising notation both sides write differently
  (LaTeX vs Unicode, spacing, `_`, `^`, braces, `$`). Lower is better.
- **Grounding check.** No labels: every real flashcard and quiz question is judged as it
  is (it should stay), and again with a planted fault: the wrong answer marked, or a card's
  answer swapped with the next card's in the same chapter (a near miss on the same topic).
  The check should remove those.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engram.course.grounding import Item, Sources, card_item, ground, quiz_item
from engram.course.schemas import Flashcard, QuizQuestion
from engram.llm.judge import Judge
from engram.timeline.anchor import Anchor, Anchors
from engram.timeline.compact import clock
from engram.vision.keyframes import Keyframe

if TYPE_CHECKING:
    from engram.config import Settings

TOLERANCE = 10.0  # seconds after a labelled moment a keyframe may still capture it

LATEX = {
    r"\theta": "θ", r"\alpha": "α", r"\beta": "β", r"\lambda": "λ", r"\sigma": "σ",
    r"\mu": "μ", r"\Sigma": "Σ", r"\sum": "Σ", r"\partial": "∂", r"\nabla": "∇",
    r"\pi": "π", r"\epsilon": "ε", r"\varepsilon": "ε", r"\eta": "η", r"\in": "∈",
    r"\rightarrow": "→", r"\to": "→", r"\Rightarrow": "⇒", r"\approx": "≈", r"\neq": "≠",
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥", r"\pm": "±", r"\times": "×",
    r"\cdot": "·", r"\sqrt": "√", r"\infty": "∞", r"\int": "∫", r"\prime": "′",
    r"\vec": "", r"\mathbf": "", r"\text": "", r"\mathrm": "", r"\frac": "", r"\hat": "",
    r"\left": "", r"\right": "", r"\quad": "", r"\,": "", r"\;": "", r"\!": "",
}  # fmt: skip
SUPERSCRIPT = {"²": "2", "³": "3", "ᵀ": "T", "⁻": "-", "¹": "1", "⁰": "0", "ⁱ": "i"}


def normalise(text: str) -> str:
    """One spelling for notation that people and models write differently."""
    for latex in sorted(LATEX, key=len, reverse=True):
        text = text.replace(latex, LATEX[latex])
    for sup, plain in SUPERSCRIPT.items():
        text = text.replace(sup, plain)
    text = unicodedata.normalize("NFKC", text).replace("⃗", "")  # combining arrow
    text = text.replace("[?]", "").replace(":=", "=").replace("−", "-").replace("*", "")
    text = re.sub(r"[\s_^{}$\\`]", "", text)
    return text.lower()


def edit_distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


# --- Handwriting --------------------------------------------------------------------------


@dataclass
class BoardScore:
    lecture: str
    frame: str
    chars: int
    errors: int

    @property
    def cer(self) -> float:
        return self.errors / max(1, self.chars)


def handwriting(labels: list[dict[str, Any]], jobs_root: Path) -> list[BoardScore]:
    scores = []
    for label in labels:
        readings = json.loads((jobs_root / label["job"] / "readings.json").read_text())
        reading = readings["frames"][label["frame"]]["key_text"]
        truth, guess = normalise(label["text"]), normalise(reading)
        scores.append(BoardScore(label["lecture"], label["frame"], len(truth),
                                 edit_distance(truth, guess)))  # fmt: skip
    return scores


# --- Board states (keyframe recall) -------------------------------------------------------


@dataclass
class StateScore:
    lecture: str
    states: int
    direct: int  # a keyframe was taken from the state itself
    via_later: int  # a later, fuller keyframe of the same board covers it
    missed: list[float] = field(default_factory=list)
    keyframes: int = 0
    extra: int = 0  # keyframes in range that match no labelled state

    @property
    def recall(self) -> float:
        return (self.direct + self.via_later) / max(1, self.states)


def board_states(
    lecture: str, marked: list[float], keyframes: list[Keyframe], until: float
) -> StateScore:
    marked = sorted(marked)
    in_range = [k for k in keyframes if k.t <= until + TOLERANCE]
    score = StateScore(lecture, len(marked), 0, 0, keyframes=len(in_range))
    used: set[str] = set()
    for i, t in enumerate(marked):
        lo = marked[i - 1] if i else -1.0
        direct = [k for k in in_range if lo < k.t <= t + TOLERANCE]
        if direct:
            score.direct += 1
            used.update(k.id for k in direct)
            continue
        later = [k for k in in_range if k.t > t and any(a <= t < b for a, b in k.views)]
        if later:
            score.via_later += 1
            used.add(later[0].id)
        else:
            score.missed.append(t)
    score.extra = sum(1 for k in in_range if k.id not in used)
    return score


# --- Grounding ----------------------------------------------------------------------------


@dataclass
class GroundingScore:
    kind: str  # "original" | "wrong answer" | "swapped answer"
    items: int
    kept: int
    kept_ids: list[str] = field(default_factory=list)  # planted faults that got through

    @property
    def kept_rate(self) -> float:
        return self.kept / max(1, self.items)


def grounding(
    course: dict[str, Any], anchors: Anchors, board: dict[str, str], judge: Judge
) -> tuple[list[GroundingScore], float]:
    sources = Sources(anchors, board)
    groups: dict[str, list[Item]] = {"original": [], "wrong answer": [], "swapped answer": []}
    windows: dict[str, tuple[float, float]] = {}
    chapters = course["chapters"]
    for ch in chapters:
        within = (ch["start"], ch["end"])
        cards = ch["flashcards"]
        for c_i, c in enumerate(cards):
            card = Flashcard(front=c["front"], back=c["back"], anchor=Anchor(frame="", quote=""))
            item = card_item(c["id"], card, c["t"], c["frame"])
            groups["original"].append(item)
            windows[item.id] = within
            if len(cards) > 1:  # the next card's answer, same chapter: a near miss, not nonsense
                neighbour = cards[(c_i + 1) % len(cards)]["back"]
                swapped = card.model_copy(update={"back": neighbour})
                bad = card_item(c["id"] + "-swap", swapped, c["t"], c["frame"])
                groups["swapped answer"].append(bad)
                windows[bad.id] = within
        for q in ch["quiz"]:
            question = QuizQuestion(
                question=q["question"], options=q["options"], answer=q["answer"],
                explanation=q["explanation"], evidence=q["evidence"],
                anchor=Anchor(frame="", quote=""),
            )  # fmt: skip
            item = quiz_item(q["id"], question, q["t"], q["frame"])
            groups["original"].append(item)
            windows[item.id] = within
            wrong = question.model_copy(update={"answer": (q["answer"] + 1) % 4})
            bad = quiz_item(q["id"] + "-wrong", wrong, q["t"], q["frame"])
            groups["wrong answer"].append(bad)
            windows[bad.id] = within
    cost = 0.0
    scores = []
    for kind, items in groups.items():
        kept = 0
        through: list[str] = []
        for ch in chapters:  # one judge call per chapter and kind, as in the pipeline
            mine = [i for i in items if windows[i.id] == (ch["start"], ch["end"])]
            verdicts, spent = ground(judge, sources, mine, (ch["start"], ch["end"]))
            cost += spent
            ok = [k for k, v in verdicts.items() if v.verdict == "supported"]
            kept += len(ok)
            if kind != "original":
                through += ok
        scores.append(GroundingScore(kind, len(items), kept, through))
    return scores, cost


# --- The report ---------------------------------------------------------------------------


def report(boards: list[BoardScore], states: list[StateScore], checks: list[GroundingScore]) -> str:
    lines = ["# Engram evals", ""]
    if boards:
        chars = sum(b.chars for b in boards)
        errors = sum(b.errors for b in boards)
        lines += ["## Handwriting (character error rate, lower is better)", "",
                  "| Lecture | Boards | Characters | CER |", "|---|---:|---:|---:|"]  # fmt: skip
        for lecture in sorted({b.lecture for b in boards}):
            mine = [b for b in boards if b.lecture == lecture]
            c, e = sum(b.chars for b in mine), sum(b.errors for b in mine)
            lines.append(f"| {lecture} | {len(mine)} | {c:,} | {e / max(1, c):.1%} |")
        lines += [f"| **All** | {len(boards)} | {chars:,} | **{errors / max(1, chars):.1%}** |",
                  "", "Per board: " + ", ".join(f"{b.lecture} {b.frame} {b.cer:.0%}"
                                               for b in boards), ""]  # fmt: skip
    if states:
        head = (
            "| Lecture | States | Found | Via a later frame | Missed | Recall | Keyframes | Extra |"
        )
        lines += ["## Board states found (keyframe recall)", "", head,
                  "|---|---:|---:|---:|---:|---:|---:|---:|"]  # fmt: skip
        for s in states:
            lines.append(
                f"| {s.lecture} | {s.states} | {s.direct} | {s.via_later} | {len(s.missed)} | "
                f"**{s.recall:.0%}** | {s.keyframes} | {s.extra} |"
            )
        for s in states:
            if s.missed:
                lines.append(f"\nMissed in {s.lecture}: " + ", ".join(clock(t) for t in s.missed))
        lines.append("")
    if checks:
        lines += ["## Grounding check", "", "| Items | Count | Kept | Should be |",
                  "|---|---:|---:|---|"]  # fmt: skip
        for g in checks:
            should = "kept" if g.kind == "original" else "removed"
            lines.append(f"| {g.kind} | {g.items} | {g.kept} ({g.kept_rate:.0%}) | {should} |")
        lines.append("")
    return "\n".join(lines)


def run(settings: Settings, root: Path, only: str = "", say: Callable[[str], None] = print) -> str:
    """Runs the evals whose labels exist, writes evals/report.md and evals/results.json."""
    from engram.llm.judge import ClaudeCodeJudge, HaikuJudge
    from engram.media import probe
    from engram.pipeline.runner import PaidCache
    from engram.pipeline.stages import _CachedJudge
    from engram.speech import Transcript
    from engram.timeline.align import Span
    from engram.vision.keyframes import find_keyframes

    jobs = settings.data_dir / "jobs"
    config = json.loads((root / "evals/lectures.json").read_text())
    lectures = {k: v for k, v in config.items() if not k.startswith("_")}
    labels = root / "evals/labels"
    boards: list[BoardScore] = []
    states: list[StateScore] = []
    checks: list[GroundingScore] = []
    if only in ("", "handwriting") and (labels / "handwriting.json").exists():
        boards = handwriting(json.loads((labels / "handwriting.json").read_text()), jobs)
        say(f"handwriting: {len(boards)} boards")
    if only in ("", "states") and (labels / "states.json").exists():
        for lecture, marked in json.loads((labels / "states.json").read_text()).items():
            video = root / lectures[lecture]["video"]
            say(f"states: finding keyframes in {video.name}")
            result, _ = find_keyframes(video, probe(video).duration)
            until = lectures[lecture]["states_until"]
            states.append(board_states(lecture, marked, result.keyframes, until))
    if only in ("", "grounding"):
        key = settings.anthropic_api_key
        base: Judge = HaikuJudge(key) if key else ClaudeCodeJudge()
        judge = _CachedJudge(base, PaidCache(settings.data_dir / "cache"))
        cost = 0.0
        for lecture, info in lectures.items():
            folder = jobs / info["course_job"]
            if not (folder / "course.json").exists():
                continue
            course = json.loads((folder / "course.json").read_text())
            words = Transcript.model_validate_json((folder / "transcript.json").read_text()).words
            alignment = json.loads((folder / "alignment.json").read_text())
            readings = json.loads((folder / "readings.json").read_text())["frames"]
            board = {f: r["key_text"] for f, r in readings.items()}
            anchors = Anchors(words, [Span.model_validate(s) for s in alignment])
            found, spent = grounding(course, anchors, board, judge)
            cost += spent
            for score in found:
                score.kept_ids = [f"{lecture}:{i}" for i in score.kept_ids]
            for score in found:
                match = next((c for c in checks if c.kind == score.kind), None)
                if match is None:
                    checks.append(score)
                else:
                    match.items += score.items
                    match.kept += score.kept
                    match.kept_ids += score.kept_ids
        say(f"grounding: {sum(c.items for c in checks)} items judged for ${cost:.3f}")
    text = report(boards, states, checks)
    (root / "evals/report.md").write_text(text)
    results = {
        "handwriting": [vars(b) | {"cer": b.cer} for b in boards],
        "states": [vars(s) | {"recall": s.recall} for s in states],
        "grounding": [vars(c) | {"kept_rate": c.kept_rate} for c in checks],
    }
    (root / "evals/results.json").write_text(json.dumps(results, indent=1) + "\n")
    return text
