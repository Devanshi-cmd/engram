"""Opus, through headless Claude Code on the Claude plan: the part of Engram that reasons.

Opus reads the compact timeline, not every image. When something it relies on is flagged
or matters, it opens that one frame with the Read tool, inside the job folder.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from engram.llm.claude_code import ClaudeCodeResult, run_claude_code


class Reasoner(Protocol):
    name: str

    def run[T: BaseModel](
        self, prompt: str, *, system: str, schema: type[T], workdir: Path
    ) -> tuple[T, ClaudeCodeResult]: ...


class OpusReasoner:
    """`claude -p --model opus --effort medium`, read-only inside the job folder."""

    def __init__(self, model: str = "opus", effort: str = "medium", timeout: float = 900) -> None:
        self.model, self.effort, self.timeout = model, effort, timeout
        self.name = f"claude-code-{model}-{effort}"

    def run[T: BaseModel](
        self, prompt: str, *, system: str, schema: type[T], workdir: Path
    ) -> tuple[T, ClaudeCodeResult]:
        return run_claude_code(
            prompt, system=system, schema=schema, model=self.model, workdir=workdir,
            tools=("Read",), effort=self.effort, max_turns=40, timeout=self.timeout,
        )  # fmt: skip


class ScriptedReasoner:
    """Offline reasoner for tests: answers each call with the next scripted function."""

    name = "scripted"

    def __init__(self, *answers: Callable[[str], BaseModel]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    def run[T: BaseModel](
        self, prompt: str, *, system: str, schema: type[T], workdir: Path
    ) -> tuple[T, ClaudeCodeResult]:
        self.prompts.append(prompt)
        value = schema.model_validate(self.answers.pop(0)(prompt).model_dump())
        return value, ClaudeCodeResult(value.model_dump(), 0.0, 0.0, 1, 0, 0)
