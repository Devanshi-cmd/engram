"""The worker: one process that runs queued jobs, oldest first, one at a time.

The web app only records uploads as queued jobs; this process does the work, so restarting
the web app never interrupts a lecture. A job left 'running' by a stopped worker is queued
again on start-up, and resumes from its last finished stage (the runner skips what's done).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from engram.pipeline import Runner

log = logging.getLogger("engram.worker")


def run_queue(
    runner: Runner,
    *,
    poll_seconds: float = 2.0,
    stop: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Runs jobs until `stop()` is true. Returns how many jobs it ran."""
    for job_id in runner.store.requeue_running():
        log.info("resuming %s, left running by a previous worker", job_id)
    ran = 0
    while not stop():
        job = runner.store.next_queued()
        if job is None:
            sleep(poll_seconds)
            continue
        log.info("running %s (%s)", job.id, job.title)
        try:
            ok = runner.run(job.id)
        except Exception:  # a bug in one job must not stop the queue
            log.exception("job %s crashed", job.id)
            runner.store.set_job_status(job.id, "failed", "Engram hit an internal error.")
            ok = False
        log.info("%s %s", job.id, "done" if ok else "failed")
        ran += 1
    return ran
