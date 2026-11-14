"""The job pipeline: stages as a resumable, cached DAG."""

from __future__ import annotations

from pathlib import Path

from engram.config import Settings
from engram.errors import EngramError
from engram.pipeline.hashing import file_sha256
from engram.pipeline.runner import PaidCache, Runner, Services, Stage, StageContext
from engram.pipeline.stages import STAGES
from engram.pipeline.store import Job, Store
from engram.speech.models import Transcriber

__all__ = [
    "Job",
    "PaidCache",
    "Runner",
    "Services",
    "Stage",
    "StageContext",
    "Store",
    "build_runner",
    "default_services",
    "start_job",
]


def default_services(settings: Settings, transcriber: Transcriber | None = None) -> Services:
    if transcriber is None:
        if not settings.elevenlabs_api_key:
            raise EngramError("ELEVENLABS_API_KEY is missing from .env.")
        from engram.speech.scribe import ScribeTranscriber

        transcriber = ScribeTranscriber(settings.elevenlabs_api_key)
    return Services(settings, transcriber, PaidCache(settings.data_dir / "cache"))


def build_runner(services: Services, stages: list[Stage] | None = None) -> Runner:
    data = services.settings.data_dir
    return Runner(Store(data / "engram.sqlite3"), services, stages or STAGES, data / "jobs")


def start_job(runner: Runner, source: Path, title: str | None = None) -> tuple[Job, bool]:
    """The job for this video, and whether it already existed (same file content = resume)."""
    source = source.resolve()
    if not source.is_file():
        raise EngramError(f"No such file: {source}")
    sha = file_sha256(source)
    existing = runner.store.find_by_source(sha)
    if existing is not None:
        return existing, True
    name = title or source.stem.replace("_", " ").replace("-", " ").strip()
    return runner.store.create_job(name, source, sha), False
