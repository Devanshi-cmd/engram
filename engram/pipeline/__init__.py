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
    reader = judge = None
    if settings.anthropic_api_key:  # otherwise Haiku runs through Claude Code on the plan
        from engram.llm.frames import HaikuFrameReader
        from engram.llm.judge import HaikuJudge

        reader = HaikuFrameReader(settings.anthropic_api_key)
        judge = HaikuJudge(settings.anthropic_api_key)
    narrator = None
    if settings.elevenlabs_api_key and settings.elevenlabs_voice_id:
        from engram.voice.elevenlabs import ElevenLabsNarrator

        narrator = ElevenLabsNarrator(settings.elevenlabs_api_key, settings.elevenlabs_voice_id)
    from engram.storage import storage_from

    cache = PaidCache(settings.data_dir / "cache")
    return Services(settings, transcriber, cache, reader, judge=judge, narrator=narrator,
                    storage=storage_from(settings))  # fmt: skip


def build_runner(services: Services, stages: list[Stage] | None = None) -> Runner:
    data = services.settings.data_dir
    return Runner(Store(data / "engram.sqlite3"), services, stages or STAGES, data / "jobs")


def start_job(target: Runner | Store, source: Path, title: str | None = None) -> tuple[Job, bool]:
    """The job for this video, and whether it already existed (same file content = resume).
    The web app passes just the store: it records jobs, and the worker runs them."""
    store = target.store if isinstance(target, Runner) else target
    source = source.resolve()
    if not source.is_file():
        raise EngramError(f"No such file: {source}")
    sha = file_sha256(source)
    existing = store.find_by_source(sha)
    if existing is not None:
        return existing, True
    name = title or source.stem.replace("_", " ").replace("-", " ").strip()
    return store.create_job(name, source, sha), False
