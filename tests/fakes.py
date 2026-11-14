"""Offline stand-ins for Opus and the Haiku judge. They answer from the prompt itself, so
the whole course pipeline runs in tests without a model, a login or a bill."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from engram.course.schemas import ChapterContent, ChapterPlan, GroundingReport, MermaidFix
from engram.llm.claude_code import ClaudeCodeResult

STAMPED = re.compile(r"^\[([\d:]+)\] (?!FRAME)(?:\(student\) )?(.+)$", re.M)
FRAME = re.compile(r"^\[[\d:]+\] FRAME (f\d+)", re.M)
GOOD_DIAGRAM = 'flowchart TD\n  A["Start with θ"] --> B["Update θ"]'
BROKEN_DIAGRAM = "flowchart TD\n  A --> "


class FakeReasoner:
    name = "fake-opus"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []  # (schema name, prompt)
        self.diagram = GOOD_DIAGRAM
        self.repair = GOOD_DIAGRAM
        self.bad_card = False  # write one card the lecture doesn't support
        self._lock = threading.Lock()

    def run[T: BaseModel](
        self, prompt: str, *, system: str, schema: type[T], workdir: Path
    ) -> tuple[T, ClaudeCodeResult]:
        with self._lock:
            self.calls.append((schema.__name__, prompt))
        files: tuple[str, ...] = ()
        if schema is ChapterPlan:
            value: Any = self._plan(prompt)
        elif schema is ChapterContent:
            value = self._content(prompt)
            frames = FRAME.findall(prompt)
            files = (f"frames/{frames[0]}.jpg",) if frames else ()
        elif schema is MermaidFix:
            value = MermaidFix(mermaid=self.repair)
        else:
            raise AssertionError(f"unexpected schema {schema}")
        out = schema.model_validate(value.model_dump())
        return out, ClaudeCodeResult(out.model_dump(), 0.5, 2.0, 3, 1000, 200, files)

    def count(self, schema: type[BaseModel]) -> int:
        return sum(1 for name, _ in self.calls if name == schema.__name__)

    @staticmethod
    def _plan(prompt: str) -> ChapterPlan:
        stamps = [m.group(1) for m in STAMPED.finditer(prompt)]
        middle = stamps[len(stamps) // 2]
        return ChapterPlan.model_validate(
            {
                "title": "A synthetic lecture",
                "overview": "How h of x is written as a sum.",
                "chapters": [
                    {"starts_at": stamps[0], "title": "The hypothesis", "summary": "h(x)."},
                    {"starts_at": middle, "title": "The sum again", "summary": "Again."},
                ],
            }
        )

    def _content(self, prompt: str) -> ChapterContent:
        slice_ = prompt.split("Timeline of this chapter:", 1)[1]
        line = STAMPED.search(slice_)
        quote = " ".join(line.group(2).split()[:8]) if line else ""
        frames = FRAME.findall(slice_)
        frame = frames[0] if frames else ""
        anchor = {"quote": quote, "frame": frame}
        cards = [{"front": "What is h of x?", "back": "The sum of theta j x j.", "anchor": anchor}]
        if self.bad_card:
            cards.append({"front": "UNSUPPORTED claim?", "back": "Bananas.", "anchor": anchor})
        return ChapterContent.model_validate(
            {
                "summary": "We write the hypothesis as a sum.",
                "sections": [
                    {
                        "heading": "The hypothesis",
                        "figure": frame,
                        "points": [
                            {"text": "$h(x) = \\sum_j \\theta_j x_j$.", "anchor": anchor},
                            {"text": "Nowhere said.", "anchor": {"quote": "", "frame": ""}},
                        ],
                    }
                ],
                "key_terms": [{"term": "hypothesis", "definition": "h.", "anchor": anchor}],
                "diagrams": [
                    {"title": "Update", "mermaid": self.diagram, "caption": "c", "anchor": anchor}
                ],
                "flashcards": cards,
                "quiz": [
                    {
                        "question": "What is h of x?",
                        "options": ["A sum", "A product", "A ratio", "A constant"],
                        "answer": 0,
                        "explanation": "It is written as a sum.",
                        "evidence": quote,
                        "anchor": anchor,
                    }
                ],
                "corrections": [],
            }
        )


class FakeJudge:
    """Supports everything except items that say UNSUPPORTED."""

    name = "fake-judge"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run[T: BaseModel](self, prompt: str, *, system: str, schema: type[T]) -> tuple[T, float]:
        assert schema is GroundingReport
        self.prompts.append(prompt)
        verdicts = []
        for block in prompt.split("### Item ")[1:]:
            item_id = block.split("\n", 1)[0].strip()
            bad = "UNSUPPORTED" in block
            verdict = "unsupported" if bad else "supported"
            reason = "not in the lecture" if bad else "said at the anchor"
            verdicts.append({"id": item_id, "verdict": verdict, "reason": reason})
        return schema.model_validate({"verdicts": verdicts}), 0.0002
