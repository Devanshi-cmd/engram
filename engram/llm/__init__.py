"""Claude: Haiku reads frames (API, or headless Claude Code as a fallback); Opus reasons."""

from engram.llm.claude_code import ClaudeCodeResult, run_claude_code
from engram.llm.frames import (
    ClaudeCodeFrameReader,
    FakeFrameReader,
    FrameContext,
    FrameReader,
    FrameReading,
    HaikuFrameReader,
)

__all__ = [
    "ClaudeCodeFrameReader",
    "ClaudeCodeResult",
    "FakeFrameReader",
    "FrameContext",
    "FrameReader",
    "FrameReading",
    "HaikuFrameReader",
    "run_claude_code",
]
