"""Runs a job's stages as a DAG, skipping any stage whose inputs haven't changed.

A stage's *input key* hashes its name, version, parameters and the output digests of the
stages it depends on (root stages hash the source video instead). If the key matches the
last successful run and the outputs on disk still match their recorded digest, the stage
is skipped. So:

- re-running a finished job does nothing and costs nothing;
- after a crash, only unfinished stages run again;
- bumping one stage's version re-runs that stage, and downstream stages re-run only if
  its outputs actually changed.

Independent stages run in parallel threads. A failed stage blocks the stages that need it,
but every other branch still finishes.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engram.config import Settings
from engram.errors import EngramError
from engram.pipeline.hashing import key_of, outputs_digest
from engram.pipeline.store import Job, Store
from engram.speech.models import Transcriber

if TYPE_CHECKING:
    from engram.llm.frames import FrameReader

EventSink = Callable[[str | None, str], None]


@dataclass
class Services:
    """Everything a stage may call out to. Tests swap in fakes."""

    settings: Settings
    transcriber: Transcriber
    cache: PaidCache
    # None means headless Claude Code on the plan (needs the job folder, so it's built per job).
    frame_reader: FrameReader | None = None

    @property
    def frame_reader_name(self) -> str:
        from engram.llm.frames import ClaudeCodeFrameReader

        return self.frame_reader.name if self.frame_reader else ClaudeCodeFrameReader.name


@dataclass(frozen=True)
class Stage:
    name: str
    label: str
    run: Callable[[StageContext], str | None]
    deps: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    version: int = 1
    params: Callable[[Services], dict[str, Any]] = field(default=lambda _: {})


class PaidCache:
    """Results of paid API calls, shared by every job: the same audio is never paid for twice."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, namespace: str, key: str) -> Any | None:
        path = self.root / namespace / f"{key}.json"
        return json.loads(path.read_text()) if path.exists() else None

    def put(self, namespace: str, key: str, value: Any) -> None:
        write_json(self.root / namespace / f"{key}.json", value)


def write_json(path: Path, value: Any) -> None:
    """Writes atomically, so a crash never leaves half a file that looks finished."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False))
    tmp.replace(path)


@dataclass
class StageContext:
    job: Job
    job_dir: Path
    stage: str
    services: Services
    store: Store
    sink: EventSink | None = None

    def path(self, name: str) -> Path:
        return self.job_dir / name

    def progress(self, message: str) -> None:
        self.store.add_event(self.job.id, self.stage, message)
        self.store.update_stage(self.job.id, self.stage, detail=message)
        if self.sink:
            self.sink(self.stage, message)

    def add_cost(self, usd: float) -> None:
        self.store.add_cost(self.job.id, self.stage, usd)

    def read_json(self, name: str) -> Any:
        return json.loads(self.path(name).read_text())

    def write_json(self, name: str, value: Any) -> None:
        write_json(self.path(name), value)


class Runner:
    def __init__(
        self,
        store: Store,
        services: Services,
        stages: list[Stage],
        jobs_root: Path,
        max_workers: int = 4,
    ) -> None:
        names = [s.name for s in stages]
        if len(set(names)) != len(names):
            raise ValueError("stage names must be unique")
        for s in stages:
            missing = [d for d in s.deps if d not in names[: names.index(s.name)]]
            if missing:
                raise ValueError(f"{s.name} depends on {missing}, which must come before it")
        self.store = store
        self.services = services
        self.stages = {s.name: s for s in stages}
        self.jobs_root = jobs_root
        self.max_workers = max_workers

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def run(self, job_id: str, sink: EventSink | None = None) -> bool:
        """Runs every stage that isn't up to date. True when all of them succeed."""
        job = self.store.job(job_id)
        job_dir = self.job_dir(job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        self.store.set_job_status(job_id, "running")
        for name in self.stages:
            self.store.update_stage(job_id, name, error=None)
            if self.store.stages(job_id)[name].status != "done":
                self.store.update_stage(job_id, name, status="pending")

        digests: dict[str, str] = {}
        failed: set[str] = set()
        waiting = list(self.stages)
        running: dict[Future[str | None], tuple[Stage, str]] = {}

        with ThreadPoolExecutor(self.max_workers) as pool:
            while waiting or running:
                for name in list(waiting):
                    stage = self.stages[name]
                    if any(d in failed for d in stage.deps):
                        waiting.remove(name)
                        failed.add(name)
                        self.store.update_stage(job_id, name, status="blocked")
                        continue
                    if not all(d in digests for d in stage.deps):
                        continue
                    waiting.remove(name)
                    key = self._input_key(job, stage, digests)
                    cached = self._cached_digest(job_id, job_dir, stage, key)
                    if cached is not None:
                        digests[name] = cached
                        self.store.update_stage(job_id, name, status="done", cached=1)
                        continue
                    attempts = self.store.stages(job_id)[name].attempts + 1
                    self.store.update_stage(
                        job_id,
                        name,
                        status="running",
                        started_at=time.time(),
                        finished_at=None,
                        attempts=attempts,
                        cached=0,
                        cost_usd=0,
                        detail=None,
                    )
                    ctx = StageContext(job, job_dir, name, self.services, self.store, sink)
                    if sink:
                        sink(name, stage.label)
                    running[pool.submit(stage.run, ctx)] = (stage, key)

                if not running:
                    if waiting:  # can't happen while deps are ordered, but never spin
                        raise RuntimeError(f"stages can't start: {waiting}")
                    continue
                finished, _ = wait(running, return_when=FIRST_COMPLETED)
                for future in finished:
                    stage, key = running.pop(future)
                    try:
                        detail = future.result()
                    except Exception as e:  # noqa: BLE001  # one stage must not stop the others
                        failed.add(stage.name)
                        message = _readable(e)
                        self._log(job_dir, stage.name, e)
                        self.store.update_stage(
                            job_id,
                            stage.name,
                            status="failed",
                            error=message,
                            finished_at=time.time(),
                        )
                        self.store.add_event(job_id, stage.name, f"Failed: {message}")
                        if sink:
                            sink(stage.name, f"Failed: {message}")
                        continue
                    digest = outputs_digest(job_dir, stage.outputs) or key_of([])
                    digests[stage.name] = digest
                    self.store.update_stage(
                        job_id,
                        stage.name,
                        status="done",
                        input_key=key,
                        output_digest=digest,
                        finished_at=time.time(),
                        detail=detail,
                    )

        ok = not failed
        first_error = next(
            (r.error for r in self.store.stages(job_id).values() if r.status == "failed"), None
        )
        self.store.set_job_status(job_id, "done" if ok else "failed", first_error)
        return ok

    def _input_key(self, job: Job, stage: Stage, digests: dict[str, str]) -> str:
        return key_of(
            {
                "stage": stage.name,
                "version": stage.version,
                "params": stage.params(self.services),
                "deps": {d: digests[d] for d in stage.deps},
                "source": job.source_sha,
            }
        )

    def _cached_digest(self, job_id: str, job_dir: Path, stage: Stage, key: str) -> str | None:
        row = self.store.stages(job_id).get(stage.name)
        if row is None or row.status != "done" or row.input_key != key:
            return None
        current = outputs_digest(job_dir, stage.outputs) or key_of([])
        return current if current == row.output_digest else None

    @staticmethod
    def _log(job_dir: Path, stage: str, error: Exception) -> None:
        with (job_dir / "engram.log").open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {stage} failed\n")
            f.write("".join(traceback.format_exception(error)))


def _readable(error: Exception) -> str:
    if isinstance(error, EngramError):
        return str(error)
    return f"{type(error).__name__}: {str(error)[:300]}"
