"""A cheap, independent text model for checks: Haiku judges what Opus wrote.

The model that wrote a quiz question is a poor judge of whether the lecture supports it, so
the grounding check uses a different one. Haiku through the API when there's a key (a
chapter's check costs a fraction of a cent), otherwise Haiku through Claude Code on the plan.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel

from engram.errors import CreditsExhausted, ProviderError
from engram.llm.claude_code import run_claude_code
from engram.llm.frames import HAIKU, HAIKU_IN_PER_MTOK, HAIKU_OUT_PER_MTOK


class Judge(Protocol):
    name: str

    def run[T: BaseModel](self, prompt: str, *, system: str, schema: type[T]) -> tuple[T, float]:
        """The answer and its cost in USD (0 on the plan)."""
        ...


class HaikuJudge:
    name = f"api-{HAIKU}"

    def __init__(self, api_key: str, *, client: object | None = None) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = client or anthropic.Anthropic(api_key=api_key, max_retries=8)

    def run[T: BaseModel](self, prompt: str, *, system: str, schema: type[T]) -> tuple[T, float]:
        anthropic = self._anthropic
        try:
            response = self._client.messages.parse(  # type: ignore[attr-defined]
                model=HAIKU,
                max_tokens=8000,
                system=system,
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
        except anthropic.BadRequestError as e:
            if "credit balance" in str(e).lower():
                raise CreditsExhausted("Anthropic") from e
            raise ProviderError(f"Haiku rejected the check: {e.message}") from e
        except anthropic.AuthenticationError as e:
            raise ProviderError("The Anthropic API key was rejected.") from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise ProviderError(f"Haiku couldn't run the check: {e}") from e
        usage = response.usage
        cost = (
            usage.input_tokens * HAIKU_IN_PER_MTOK + usage.output_tokens * HAIKU_OUT_PER_MTOK
        ) / 1e6
        if response.parsed_output is None:
            raise ProviderError(f"Haiku gave no verdict (stop reason {response.stop_reason}).")
        return response.parsed_output, cost


class ClaudeCodeJudge:
    """Haiku through headless Claude Code, with no tools at all."""

    name = "claude-code-haiku"

    def run[T: BaseModel](self, prompt: str, *, system: str, schema: type[T]) -> tuple[T, float]:
        with tempfile.TemporaryDirectory(prefix="engram-judge-") as tmp:
            value, _ = run_claude_code(
                prompt, system=system, schema=schema, model="haiku", workdir=Path(tmp),
                effort="medium", max_turns=3, timeout=300,
            )  # fmt: skip
        return value, 0.0
