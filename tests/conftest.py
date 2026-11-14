"""Shared fixtures. Videos are generated with ffmpeg, so the tests need no downloads."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from engram.config import Settings
from engram.pipeline import Runner, Services, build_runner
from engram.pipeline.runner import PaidCache
from engram.speech.fake import FakeTranscriber


def make_video(path: Path, seconds: float = 3, *, audio: bool = True, video: bool = True) -> Path:
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error"]
    if video:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    cmd += ["-shortest", str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.fixture(scope="session")
def lecture_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_video(tmp_path_factory.mktemp("media") / "lecture.mp4")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def services(settings: Settings, transcriber: FakeTranscriber) -> Services:
    return Services(settings, transcriber, PaidCache(settings.data_dir / "cache"))


@pytest.fixture
def runner(services: Services) -> Runner:
    return build_runner(services)
