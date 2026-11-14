"""Headless Claude Code (`claude -p`): Claude on the Claude plan, not the API.

Three things keep it cheap and safe:

- **No API keys in its environment.** With `ANTHROPIC_API_KEY` set, Claude Code bills the
  API instead of the plan. Every secret Engram knows about is stripped (Recall's lesson).
- **A minimal session.** Our own system prompt replaces Claude Code's (about 21,000 tokens
  of coding-agent instructions), with only the tools the job needs, and no MCP servers,
  settings, skills or saved session. A trivial call drops from about 43,000 input tokens
  to about 1,800.
- **Read-only, inside one folder.** Lecture frames can contain any text, so the model gets
  the Read tool at most, with the job folder as its working directory. No shell, no network,
  no writes. Output must match a JSON schema, which Claude Code validates.

The prompt goes in on stdin (a lecture timeline is bigger than the 128 KB a single
command-line argument may be), and the output is the event stream, so Engram records which
files the model actually opened instead of asking it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from engram.errors import EngramError, ProviderError

SECRETS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ELEVENLABS_API_KEY",
    "ELEVENLABS_VOICE_ID",
    "ENGRAM_ACCESS_CODES",
    "OPENAI_API_KEY",
    "GITHUB_TOKEN",
}


@dataclass(frozen=True)
class ClaudeCodeResult:
    value: Any
    cost_usd: float  # what the same tokens would cost on the API; the plan isn't billed per call
    seconds: float
    turns: int
    input_tokens: int
    output_tokens: int
    files_read: tuple[str, ...] = ()  # paths the model opened with Read, relative to workdir


def claude_binary() -> str:
    found = shutil.which("claude") or str(Path.home() / ".local/bin/claude")
    if not Path(found).exists():
        raise EngramError("Claude Code isn't installed on this machine (`claude` not found).")
    return found


def clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SECRETS}
    env["PATH"] = f"{Path.home()}/.local/bin:" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", str(Path.home()))
    return env


def run_claude_code[T: BaseModel](
    prompt: str,
    *,
    system: str,
    schema: type[T],
    model: str,
    workdir: Path,
    tools: tuple[str, ...] = (),
    effort: str | None = None,
    max_turns: int = 8,
    timeout: float = 600,
) -> tuple[T, ClaudeCodeResult]:
    """One headless Claude Code call that must return an instance of `schema`."""
    cmd = [
        claude_binary(), "-p",
        "--model", model,
        "--system-prompt", system,
        "--tools", ",".join(tools),
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--max-turns", str(max_turns),
    ]  # fmt: skip
    if tools:
        cmd += ["--allowedTools", *tools]
    if effort:
        cmd += ["--effort", effort]
    started = time.monotonic()
    try:
        done = subprocess.run(
            cmd, cwd=workdir, env=clean_env(), capture_output=True, text=True,
            timeout=timeout, input=prompt,
        )  # fmt: skip
    except subprocess.TimeoutExpired as e:
        raise ProviderError(f"Claude Code took longer than {timeout:.0f} s and was stopped.") from e
    events = _events(done.stdout)
    out = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if out is None and events:
        out = events[-1]
    if out is None:
        detail = (done.stderr or done.stdout or f"exit code {done.returncode}").strip()
        if "login" in detail.lower() or "auth" in detail.lower():
            raise EngramError("Claude Code on this machine isn't logged in (run `claude`).")
        raise ProviderError(f"Claude Code failed: {detail[-300:]}")
    if out.get("is_error") or out.get("subtype") != "success":
        raise ProviderError(f"Claude Code failed: {str(out.get('result') or out)[:300]}")
    try:
        value = schema.model_validate(out.get("structured_output"))
    except ValidationError as e:
        raise ProviderError(f"Claude Code returned the wrong shape: {e.errors()[:2]}") from e
    usage: dict[str, Any] = next(iter((out.get("modelUsage") or {}).values()), {})
    return value, ClaudeCodeResult(
        value=out.get("structured_output"),
        cost_usd=float(out.get("total_cost_usd") or 0),
        seconds=time.monotonic() - started,
        turns=int(out.get("num_turns") or 0),
        input_tokens=int(usage.get("inputTokens", 0))
        + int(usage.get("cacheReadInputTokens", 0))
        + int(usage.get("cacheCreationInputTokens", 0)),
        output_tokens=int(usage.get("outputTokens", 0)),
        files_read=_files_read(events, workdir),
    )


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _files_read(events: list[dict[str, Any]], workdir: Path) -> tuple[str, ...]:
    root = workdir.resolve()
    seen: list[str] = []
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == "Read":
                path = Path(str(block.get("input", {}).get("file_path", "")))
                if path.is_relative_to(root):
                    path = path.relative_to(root)
                if str(path) not in seen:
                    seen.append(str(path))
    return tuple(seen)
