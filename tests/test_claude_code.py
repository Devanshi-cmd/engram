import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from engram.errors import EngramError, ProviderError
from engram.llm import claude_code
from engram.llm.claude_code import run_claude_code


class Answer(BaseModel):
    word: str


def fake_cli(monkeypatch: pytest.MonkeyPatch, stdout: str, seen: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(claude_code, "claude_binary", lambda: "/usr/bin/claude")

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr("engram.llm.claude_code.subprocess.run", run)


READ_EVENT = json.dumps(
    {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": "Read",
                                 "input": {"file_path": "WORKDIR/frames/f0044.jpg"}}]},
    }
)  # fmt: skip
SUCCESS = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "structured_output": {"word": "hi"},
        "total_cost_usd": 0.01,
        "num_turns": 2,
        "modelUsage": {"m": {"inputTokens": 5, "cacheReadInputTokens": 100, "outputTokens": 7}},
    }
)


def test_runs_lean_read_only_and_without_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-secret")
    seen: list[dict[str, Any]] = []
    fake_cli(monkeypatch, READ_EVENT.replace("WORKDIR", str(tmp_path)) + "\n" + SUCCESS, seen)
    value, result = run_claude_code(
        "hi", system="Be brief.", schema=Answer, model="opus", workdir=tmp_path,
        tools=("Read",), effort="medium",
    )  # fmt: skip
    assert value == Answer(word="hi")
    assert (result.turns, result.input_tokens, result.output_tokens) == (2, 105, 7)
    assert result.files_read == ("frames/f0044.jpg",)  # seen in the event stream
    call = seen[0]
    cmd = call["cmd"]
    assert call["input"] == "hi" and "hi" not in cmd  # on stdin: timelines outgrow argv
    assert "ANTHROPIC_API_KEY" not in call["env"] and "ELEVENLABS_API_KEY" not in call["env"]
    assert cmd[cmd.index("--system-prompt") + 1] == "Be brief."  # replaces Claude Code's own
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert {"--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"} <= set(
        cmd
    )
    assert json.loads(cmd[cmd.index("--json-schema") + 1])["required"] == ["word"]
    assert call["cwd"] == tmp_path


def test_no_tools_means_none_at_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    fake_cli(monkeypatch, SUCCESS, seen)
    run_claude_code("hi", system="s", schema=Answer, model="haiku", workdir=tmp_path)
    cmd = seen[0]["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--allowedTools" not in cmd


def test_errors_are_readable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_cli(monkeypatch, json.dumps({"is_error": True, "result": "rate limited"}), [])
    with pytest.raises(ProviderError, match="rate limited"):
        run_claude_code("x", system="s", schema=Answer, model="opus", workdir=tmp_path)

    fake_cli(monkeypatch, "Please run /login", [])
    with pytest.raises(EngramError, match="isn't logged in"):
        run_claude_code("x", system="s", schema=Answer, model="opus", workdir=tmp_path)

    wrong = json.dumps({"subtype": "success", "structured_output": {"nope": 1}})
    fake_cli(monkeypatch, wrong, [])
    with pytest.raises(ProviderError, match="wrong shape"):
        run_claude_code("x", system="s", schema=Answer, model="opus", workdir=tmp_path)
