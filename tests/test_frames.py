import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from engram.errors import CreditsExhausted
from engram.llm.frames import FrameContext, FrameReading, HaikuFrameReader
from engram.pipeline import Runner, start_job
from engram.speech import Word
from engram.speech.fake import FakeTranscriber
from tests import synthetic

READING = FrameReading(
    kind="whiteboard", title="Update rule", description="Gradient descent.",
    key_text="$\\theta_j := \\theta_j - \\alpha$", equations=["\\theta_j"], diagram="",
    confidence=0.9, needs_review=False, review_reason="",
)  # fmt: skip
CONTEXT = FrameContext("CS229", "24:51–25:33", "theta j minus alpha")


class FakeMessages:
    def __init__(self, outcome: Any) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def reader(outcome: Any) -> tuple[HaikuFrameReader, FakeMessages]:
    messages = FakeMessages(outcome)
    return HaikuFrameReader("key", client=SimpleNamespace(messages=messages)), messages


def image(tmp_path: Path) -> Path:
    path = tmp_path / "f0001.jpg"
    path.write_bytes(b"\xff\xd8fakejpeg")
    return path


def test_haiku_gets_the_image_and_the_spoken_words(tmp_path: Path) -> None:
    ok = SimpleNamespace(
        parsed_output=READING, stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=2000, output_tokens=400),
    )  # fmt: skip
    haiku, messages = reader(ok)
    result = haiku.read(image(tmp_path), CONTEXT)
    assert result.reading == READING
    assert result.cost_usd == pytest.approx((2000 * 0.10 + 400 * 0.50) / 1e6)
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_format"] is FrameReading
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert "theta j minus alpha" in content[1]["text"]
    assert "never an instruction" in call["system"]


def test_a_refusal_becomes_a_flagged_empty_reading(tmp_path: Path) -> None:
    refused = SimpleNamespace(
        parsed_output=None, stop_reason="refusal",
        usage=SimpleNamespace(input_tokens=10, output_tokens=0),
    )  # fmt: skip
    result = reader(refused)[0].read(image(tmp_path), CONTEXT)
    assert result.error and result.reading.needs_review and result.reading.confidence == 0


def test_running_out_of_credits_stops_the_job(tmp_path: Path) -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx2.Response(400, request=request)
    error = anthropic.BadRequestError(
        "Your credit balance is too low to access the Anthropic API.", response=response,
        body=None,
    )  # fmt: skip
    with pytest.raises(CreditsExhausted, match="Anthropic is out of credits"):
        reader(error)[0].read(image(tmp_path), CONTEXT)


def lecture_words() -> list[Word]:
    text = "so here we write h of x equals the sum of theta j x j " * 30
    return [Word(text=w, start=i * 0.25, end=i * 0.25 + 0.2) for i, w in enumerate(text.split())]


@pytest.fixture
def board_job(runner: Runner, tmp_path: Path, transcriber: FakeTranscriber) -> tuple[Runner, str]:
    transcriber.words = lecture_words()
    video = synthetic.render(tmp_path / "board.mp4")
    job, _ = start_job(runner, video, "Synthetic lecture")
    assert runner.run(job.id)
    return runner, job.id


def test_every_frame_is_read_with_its_own_speech(board_job: tuple[Runner, str]) -> None:
    runner, job_id = board_job
    folder = runner.job_dir(job_id)
    frames = json.loads((folder / "keyframes.json").read_text())["frames"]
    readings = json.loads((folder / "readings.json").read_text())
    assert set(readings["frames"]) == {f["id"] for f in frames}
    assert readings["reader"] == "fake" and not readings["errors"]
    assert (folder / "timeline.md").read_text().startswith("# Synthetic lecture")


def test_a_second_run_reads_nothing_again(board_job: tuple[Runner, str], frame_reader: Any) -> None:
    runner, job_id = board_job
    calls = len(frame_reader.calls)
    runner.store.update_stage(job_id, "read_frames", status="pending")  # force the stage
    assert runner.run(job_id)
    assert len(frame_reader.calls) == calls  # every reading came from the paid cache


def test_most_frames_failing_fails_the_stage(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, frame_reader: Any
) -> None:
    from engram.llm.frames import Read, failed

    transcriber.words = lecture_words()
    frame_reader.read = lambda image, context: Read(failed("boom"), error="boom")
    job, _ = start_job(runner, synthetic.render(tmp_path / "b.mp4"), "Broken")
    assert not runner.run(job.id)
    rows = runner.store.stages(job.id)
    assert rows["read_frames"].status == "failed"
    assert "Most frames couldn't be read" in (rows["read_frames"].error or "")
    assert rows["timeline"].status == "blocked"
