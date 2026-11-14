import json
import threading
from pathlib import Path

import pytest

from engram.pipeline import Runner, Services, Stage, StageContext, build_runner, start_job
from engram.pipeline.stages import STAGES
from engram.speech.fake import FakeTranscriber
from tests.conftest import make_video


def test_a_lecture_becomes_media_audio_and_transcript(runner: Runner, lecture_video: Path) -> None:
    job, resumed = start_job(runner, lecture_video, "Calculus | Chain rule")
    assert not resumed
    assert runner.run(job.id)
    folder = runner.job_dir(job.id)
    assert json.loads((folder / "media.json").read_text())["width"] == 320
    assert (folder / "audio.mp3").stat().st_size > 0
    transcript = json.loads((folder / "transcript.json").read_text())
    assert transcript["segments"][0]["text"] == "Today we derive the chain rule."
    assert runner.store.job(job.id).status == "done"


def test_the_title_becomes_scribe_keyterms(
    runner: Runner, lecture_video: Path, transcriber: FakeTranscriber
) -> None:
    job, _ = start_job(runner, lecture_video, "Calculus | Chain rule")
    runner.run(job.id)
    assert transcriber.calls[0][1] == ["Calculus | Chain rule", "Calculus", "Chain rule"]


def test_rerunning_a_finished_job_does_nothing(
    runner: Runner, lecture_video: Path, transcriber: FakeTranscriber
) -> None:
    job, _ = start_job(runner, lecture_video)
    runner.run(job.id)
    again, resumed = start_job(runner, lecture_video)
    assert resumed and again.id == job.id
    assert runner.run(job.id)
    assert len(transcriber.calls) == 1
    assert all(row.cached for row in runner.store.stages(job.id).values())


def test_a_crash_resumes_without_redoing_finished_stages(
    services: Services, lecture_video: Path, transcriber: FakeTranscriber
) -> None:
    calls = {"audio": 0}
    real_audio = STAGES[1]

    def flaky_transcribe(ctx: StageContext) -> str:
        raise RuntimeError("power cut")

    def counting_audio(ctx: StageContext) -> str | None:
        calls["audio"] += 1
        return real_audio.run(ctx)

    broken = [
        STAGES[0],
        _with(real_audio, run=counting_audio),
        _with(STAGES[2], run=flaky_transcribe),
    ]
    first = build_runner(services, broken)
    job, _ = start_job(first, lecture_video)
    assert not first.run(job.id)
    rows = first.store.stages(job.id)
    assert rows["transcribe"].status == "failed"
    assert rows["transcribe"].error == "RuntimeError: power cut"
    assert first.store.job(job.id).status == "failed"
    assert "power cut" in (first.job_dir(job.id) / "engram.log").read_text()

    fixed = build_runner(services, [STAGES[0], _with(real_audio, run=counting_audio), STAGES[2]])
    assert fixed.run(job.id)
    assert calls["audio"] == 1
    assert fixed.store.stages(job.id)["audio"].cached
    assert fixed.store.stages(job.id)["transcribe"].attempts == 2


def test_a_paid_transcript_is_reused_by_a_new_job(
    runner: Runner, services: Services, tmp_path: Path, transcriber: FakeTranscriber
) -> None:
    a = make_video(tmp_path / "a.mp4")
    job, _ = start_job(runner, a, "Same talk")
    runner.run(job.id)
    # A fresh data folder for jobs, sharing the same paid cache: no second API call.
    job_b = runner.store.create_job("Same talk", a, "another-sha")
    assert runner.run(job_b.id)
    assert len(transcriber.calls) == 1


def test_editing_an_output_on_disk_reruns_that_stage(
    runner: Runner, lecture_video: Path, transcriber: FakeTranscriber
) -> None:
    job, _ = start_job(runner, lecture_video)
    runner.run(job.id)
    (runner.job_dir(job.id) / "transcript.json").write_text("{}")
    runner.run(job.id)
    rows = runner.store.stages(job.id)
    assert not rows["transcribe"].cached and rows["audio"].cached
    assert json.loads((runner.job_dir(job.id) / "transcript.json").read_text())["words"]


def test_a_failure_blocks_dependents_but_not_other_branches(
    services: Services, lecture_video: Path
) -> None:
    def boom(ctx: StageContext) -> str:
        raise RuntimeError("nope")

    def write(name: str) -> Stage:
        def run(ctx: StageContext) -> str:
            ctx.path(name).write_text(name)
            return name

        return Stage(name, name, run, outputs=(name,))

    stages = [
        write("left"),
        Stage("bad", "bad", boom, deps=("left",)),
        Stage("after_bad", "after", lambda c: None, deps=("bad",)),
        write("right"),
    ]
    r = build_runner(services, stages)
    job, _ = start_job(r, lecture_video)
    assert not r.run(job.id)
    status = {n: row.status for n, row in r.store.stages(job.id).items()}
    assert status == {"left": "done", "bad": "failed", "after_bad": "blocked", "right": "done"}


def test_independent_stages_run_at_the_same_time(services: Services, lecture_video: Path) -> None:
    barrier = threading.Barrier(2, timeout=5)

    def meet(ctx: StageContext) -> str:
        barrier.wait()  # deadlocks (and times out) unless both run concurrently
        return "met"

    r = build_runner(services, [Stage("a", "a", meet), Stage("b", "b", meet)])
    job, _ = start_job(r, lecture_video)
    assert r.run(job.id)


def test_bumping_a_version_reruns_only_what_changed(
    services: Services, lecture_video: Path
) -> None:
    runs: list[str] = []

    def stage(name: str, version: int, deps: tuple[str, ...] = ()) -> Stage:
        def run(ctx: StageContext) -> str:
            runs.append(name)
            ctx.path(name).write_text("same output")
            return name

        return Stage(name, name, run, deps=deps, outputs=(name,), version=version)

    r = build_runner(services, [stage("one", 1), stage("two", 1, ("one",))])
    job, _ = start_job(r, lecture_video)
    r.run(job.id)
    r2 = build_runner(services, [stage("one", 2), stage("two", 1, ("one",))])
    r2.run(job.id)
    # "one" re-ran, but wrote the same bytes, so "two" stayed cached.
    assert runs == ["one", "two", "one"]


def test_too_long_lectures_are_refused_with_a_clear_message(
    services: Services, lecture_video: Path
) -> None:
    from dataclasses import replace

    services.settings = replace(services.settings, max_minutes=0)
    r = build_runner(services)
    job, _ = start_job(r, lecture_video)
    assert not r.run(job.id)
    assert r.store.job(job.id).error == "This lecture is 0 minutes long. The limit is 0 minutes."


def test_missing_file_is_a_readable_error(runner: Runner, tmp_path: Path) -> None:
    from engram.errors import EngramError

    with pytest.raises(EngramError, match="No such file"):
        start_job(runner, tmp_path / "nope.mp4")


def _with(stage: Stage, **changes: object) -> Stage:
    from dataclasses import replace

    return replace(stage, **changes)  # type: ignore[arg-type]


def test_a_low_balance_is_announced_after_paying(
    runner: Runner, lecture_video: Path, transcriber: FakeTranscriber
) -> None:
    transcriber.balance_minutes = 12
    job, _ = start_job(runner, lecture_video)
    runner.run(job.id)
    messages = [m for _, _, _, m in runner.store.events(job.id)]
    assert any("about 12 minutes of lecture left" in m for m in messages)
