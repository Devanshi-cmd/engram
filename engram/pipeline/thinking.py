"""Opus calls for the course, cached and recorded.

Every answer is kept in the paid cache under a key of the reasoner, prompt version, system
prompt, prompt and schema. Re-running a job, or a stage downstream of a change, never asks
Opus the same question twice. Each call's real usage is recorded (time, turns, tokens, the
files Opus opened) so the job page and the docs can show it.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from engram.course.prompts import PROMPT_VERSION
from engram.llm.reasoner import Reasoner
from engram.pipeline.hashing import key_of
from engram.pipeline.runner import PaidCache


@dataclass(frozen=True)
class Call:
    label: str
    seconds: float
    turns: int
    input_tokens: int
    output_tokens: int
    api_equivalent_usd: float  # what the tokens would cost on the API; the plan isn't billed
    files_read: tuple[str, ...]
    cached: bool


class Thinker:
    def __init__(self, reasoner: Reasoner, cache: PaidCache, workdir: Path) -> None:
        self.reasoner, self.cache, self.workdir = reasoner, cache, workdir
        self.calls: list[Call] = []
        self._lock = threading.Lock()

    def ask[T: BaseModel](self, label: str, prompt: str, *, system: str, schema: type[T]) -> T:
        key = key_of(
            {
                "by": self.reasoner.name,
                "v": PROMPT_VERSION,
                "system": system,
                "prompt": prompt,
                "schema": schema.model_json_schema(),
            }
        )
        saved = self.cache.get("reasoning", key)
        if saved is not None:
            value = schema.model_validate(saved["value"])
            call = Call(**{**saved["call"], "files_read": tuple(saved["call"]["files_read"]),
                           "label": label, "cached": True})  # fmt: skip
        else:
            value, result = self.reasoner.run(
                prompt, system=system, schema=schema, workdir=self.workdir
            )
            call = Call(
                label=label, seconds=round(result.seconds, 1), turns=result.turns,
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                api_equivalent_usd=round(result.cost_usd, 4), files_read=result.files_read,
                cached=False,
            )  # fmt: skip
            self.cache.put("reasoning", key, {"value": value.model_dump(), "call": asdict(call)})
        with self._lock:
            self.calls.append(call)
        return value

    def report(self) -> dict[str, Any]:
        fresh = [c for c in self.calls if not c.cached]
        return {
            "reasoner": self.reasoner.name,
            "calls": [asdict(c) for c in self.calls],
            "fresh_calls": len(fresh),
            "seconds": round(sum(c.seconds for c in fresh), 1),
            "api_equivalent_usd": round(sum(c.api_equivalent_usd for c in fresh), 4),
        }
