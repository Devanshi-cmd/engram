"""The grounding check: every quiz question and flashcard must be supported by the lecture.

For each item, code gathers the source passages (what was said around the item's anchor,
around its quoted evidence, and the writing on the frames shown then) and a separate model
(Haiku) decides whether they support the answer. Anything unsupported or ambiguous is
removed, with the reason kept in the course file, so the check itself can be audited.
"""

from __future__ import annotations

from dataclasses import dataclass

from engram.course.prompts import JUDGE_SYSTEM
from engram.course.schemas import Flashcard, GroundingReport, ItemVerdict, QuizQuestion
from engram.llm.judge import Judge
from engram.timeline.anchor import Anchors
from engram.timeline.compact import clock

BEFORE, AFTER = 40, 70  # seconds of speech around an anchor: the idea, then its explanation
MAX_BOARD_CHARS = 1500

LETTERS = "ABCD"


@dataclass(frozen=True)
class Item:
    id: str
    body: str  # the item as the judge sees it
    t: float | None
    frame: str | None
    evidence: str


def quiz_item(item_id: str, q: QuizQuestion, t: float | None, frame: str | None) -> Item:
    options = "\n".join(f"{LETTERS[i]}) {o}" for i, o in enumerate(q.options))
    body = (
        f"Quiz question: {q.question}\n{options}\nMarked correct: {LETTERS[q.answer]}\n"
        f"Explanation given: {q.explanation}"
    )
    return Item(item_id, body, t, frame, q.evidence)


def card_item(item_id: str, card: Flashcard, t: float | None, frame: str | None) -> Item:
    return Item(item_id, f"Flashcard. Front: {card.front}\nBack: {card.back}", t, frame, "")


class Sources:
    def __init__(self, anchors: Anchors, board: dict[str, str]) -> None:
        self.anchors = anchors
        self.board = board  # frame id → its writing

    def speech(self, t: float) -> str:
        words = [w for w in self.anchors.words if t - BEFORE <= w.start <= t + AFTER]
        return f"[{clock(max(0, t - BEFORE))}] " + " ".join(w.text for w in words)

    def frames_between(self, lo: float, hi: float) -> list[str]:
        return list(
            dict.fromkeys(s.frame for s in self.anchors.spans if s.end > lo and s.start < hi)
        )

    def passages(self, item: Item, within: tuple[float, float]) -> str:
        parts: list[str] = []
        times: list[float] = []
        if item.t is not None:
            times.append(item.t)
        if item.evidence:
            found = self.anchors.quote_time(item.evidence, within)
            if found and found[1] >= 0.6 and all(abs(found[0] - t) > AFTER for t in times):
                times.append(found[0])
        frames: list[str] = [item.frame] if item.frame else []
        for t in times:
            parts.append(f"Speech: {self.speech(t)}")
            frames += self.frames_between(t - BEFORE, t + AFTER)
        board = [
            f"Board/slide {f}: {self.board[f][:MAX_BOARD_CHARS]}"
            for f in dict.fromkeys(frames)
            if self.board.get(f, "").strip()
        ]
        return "\n".join(parts + board) or "(no source found)"


def ground(
    judge: Judge, sources: Sources, items: list[Item], within: tuple[float, float]
) -> tuple[dict[str, ItemVerdict], float]:
    """A verdict for every item (missing ones count as unsupported) and the cost."""
    if not items:
        return {}, 0.0
    blocks = [
        f"### Item {item.id}\n{item.body}\n\nSource passages:\n{sources.passages(item, within)}"
        for item in items
    ]
    prompt = "Check each item below. Return one verdict per item id.\n\n" + "\n\n".join(blocks)
    report, cost = judge.run(prompt, system=JUDGE_SYSTEM, schema=GroundingReport)
    verdicts = {v.id: v for v in report.verdicts}
    for item in items:
        verdicts.setdefault(
            item.id, ItemVerdict(id=item.id, verdict="unsupported", reason="no verdict returned")
        )
    return {i.id: verdicts[i.id] for i in items}, cost
