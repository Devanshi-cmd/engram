"""Command line: `engram serve` runs the web app."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated

import typer

from engram import __version__

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="A lecture video in, a course out."
)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Address to listen on."),
    port: int = typer.Option(4810, help="Port to listen on."),
    allowed_host: list[str] = typer.Option(  # noqa: B008
        ["127.0.0.1", "localhost"], help="Host header to accept; repeat for each public hostname."
    ),
    uploads: bool = typer.Option(
        False, help="Accept uploads (needs ENGRAM_ACCESS_CODES and a running `engram worker`)."
    ),
) -> None:
    """Run the web app."""
    import uvicorn

    from engram.config import load_settings
    from engram.web.app import create_app

    app_ = create_app(load_settings(), allowed_hosts=allowed_host, accept_uploads=uploads)
    uvicorn.run(app_, host=host, port=port)


@app.command()
def worker() -> None:
    """Run queued lectures, one at a time, until stopped."""
    import logging
    import signal

    from engram.config import load_settings
    from engram.errors import EngramError
    from engram.pipeline import build_runner, default_services
    from engram.worker import run_queue

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    stopping = False

    def stop(*_: object) -> None:
        nonlocal stopping
        stopping = True  # finish the current job's running stage, then exit

    signal.signal(signal.SIGTERM, stop)
    try:
        runner = build_runner(default_services(load_settings()))
    except EngramError as e:
        typer.secho(f"✕ {e}", fg="red", err=True)
        raise typer.Exit(1) from e
    run_queue(runner, stop=lambda: stopping)


@app.command(name="eval")
def eval_(
    only: str = typer.Option("", help="Run one part: handwriting, states or grounding."),
) -> None:
    """Measure quality against the hand-made answer keys in evals/; writes evals/report.md."""
    from engram.config import ROOT, load_settings
    from engram.evals import run

    typer.echo(run(load_settings(), ROOT, only, typer.echo))


@app.command(name="r2-check")
def r2_check(
    origin: list[str] = typer.Option(  # noqa: B008
        ["https://engram.devanshitiwari.com"], help="Site allowed to upload from a browser."
    ),
) -> None:
    """Check the R2 settings with a round trip, and let the site upload to the bucket."""
    import tempfile
    from pathlib import Path

    from engram.config import load_settings
    from engram.storage import R2Storage, storage_from

    storage = storage_from(load_settings())
    if not isinstance(storage, R2Storage):
        typer.secho("✕ R2 isn't configured: fill in the four R2_ values in .env.", fg="red")
        raise typer.Exit(1)
    with tempfile.TemporaryDirectory() as tmp:
        probe_file = Path(tmp) / "probe.txt"
        probe_file.write_text("engram r2 check")
        storage.put_file(probe_file, "checks/probe.txt", "text/plain")
        found = storage.head("checks/probe.txt")
        storage.delete("checks/probe.txt")
    typer.echo(f"✓ write, read and delete work in bucket {storage.bucket} ({found})")
    try:
        storage.allow_browser_uploads(origin)
        typer.echo(f"✓ browsers on {', '.join(origin)} may upload (bucket CORS set)")
    except Exception as e:  # noqa: BLE001  # an object-only token can't set CORS
        typer.secho(f"! Couldn't set the bucket's CORS rule ({e}). Set it in the Cloudflare "
                    "dashboard instead; docs/DEPLOY.md has the rule.", fg="yellow")  # fmt: skip


@app.command(name="list")
def list_course(
    job_id: str = typer.Argument(..., help="The job to show or hide."),
    hide: bool = typer.Option(False, "--hide", help="Take it off the home page."),
    source: str = typer.Option("", help="Who made the lecture, e.g. 'MIT OpenCourseWare'."),
    url: str = typer.Option("", help="Where the original lecture is."),
    license: str = typer.Option("", help="Its licence, e.g. 'CC BY-NC-SA 4.0'."),
    license_url: str = typer.Option("", help="Link to the licence text."),
    notice: str = typer.Option("", help="A notice the licence requires shown with it."),
) -> None:
    """Show a finished course among the home page's sample courses (or hide it).

    A lecture someone else made is credited on its course page: pass --source, --url and
    --license (all three) the first time it's listed.
    """
    from engram.config import load_settings
    from engram.pipeline import Store

    settings = load_settings()
    store = Store(settings.data_dir / "engram.sqlite3")
    try:
        store.job(job_id)
    except KeyError:
        typer.secho(f"✕ No job {job_id}", fg="red", err=True)
        raise typer.Exit(1) from None
    if source or url or license:
        if not (source and url and license):
            typer.secho("✕ A credit needs --source, --url and --license.", fg="red", err=True)
            raise typer.Exit(1)
        credit = {"source": source, "url": url, "license": license,
                  "license_url": license_url, "notice": notice}  # fmt: skip
        path = settings.data_dir / "jobs" / job_id / "credit.json"
        path.write_text(json.dumps(credit, indent=1, ensure_ascii=False))
    store.set_listed(job_id, not hide)
    typer.echo(f"{job_id} is {'hidden' if hide else 'listed on the home page'}.")


@app.command()
def cover(
    job_id: str = typer.Argument(..., help="The course to give a cover."),
    image: Path = typer.Argument(..., help="A still image, e.g. a frame taken from the lecture."),  # noqa: B008
    remove: bool = typer.Option(False, "--remove", help="Go back to the summary video's poster."),
) -> None:
    """Set the picture that stands for a course on the home page and in link previews.

    Without one, the summary video's poster is used. The image is saved as cover.jpg in the
    job folder, at most 1280 px wide, and isn't cropped: the site crops it to 16:9.
    """
    from engram.config import load_settings
    from engram.errors import MediaError
    from engram.media.ffmpeg import _run

    folder = load_settings().data_dir / "jobs" / job_id
    if not (folder / "course.json").exists():
        typer.secho(f"✕ No finished course {job_id}", fg="red", err=True)
        raise typer.Exit(1)
    target = folder / "cover.jpg"
    if remove:
        target.unlink(missing_ok=True)
        typer.echo(f"{job_id} uses its summary poster again.")
        return
    if not image.is_file():
        typer.secho(f"✕ No image at {image}", fg="red", err=True)
        raise typer.Exit(1)
    try:
        _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(image), "-frames:v", "1",
              "-vf", "scale='min(1280,iw)':-2", "-q:v", "2", str(target)], timeout=60)  # fmt: skip
    except MediaError as e:
        typer.secho(f"✕ {e}", fg="red", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{job_id} now uses {target.name}.")


@app.command()
def run(
    video: Path = typer.Argument(..., help="The lecture video file."),  # noqa: B008
    title: str | None = typer.Option(None, help="Lecture title; also a spelling hint for speech."),
) -> None:
    """Process a lecture. Running it again on the same file resumes where it stopped."""
    from engram.config import load_settings
    from engram.errors import EngramError
    from engram.pipeline import build_runner, default_services, start_job

    try:
        runner = build_runner(default_services(load_settings()))
        job, resumed = start_job(runner, video, title)
    except EngramError as e:
        typer.secho(f"✕ {e}", fg="red", err=True)
        raise typer.Exit(1) from e
    typer.secho(f"{'Resuming' if resumed else 'Starting'} “{job.title}”", bold=True)
    typer.echo(f"  job {job.id} · {runner.job_dir(job.id)}")
    started = time.monotonic()

    def show(stage: str | None, message: str) -> None:
        typer.echo(f"  {time.monotonic() - started:6.1f}s  {stage or '':<11} {message}")

    ok = runner.run(job.id, sink=show)
    typer.echo("")
    rows = runner.store.stages(job.id)
    for row in (rows[name] for name in runner.stages if name in rows):
        mark = {"done": "✓", "failed": "✕", "blocked": "–"}.get(row.status, "·")
        how = "cached" if row.cached else (f"{row.seconds:.1f}s" if row.seconds else "")
        text = row.error if row.status == "failed" else (row.detail or "")
        typer.echo(f"  {mark} {row.name:<11} {how:>7}  {text}")
    if not ok:
        raise typer.Exit(1)


@app.command()
def jobs() -> None:
    """List processed lectures."""
    from engram.config import load_settings
    from engram.pipeline import Store

    store = Store(load_settings().data_dir / "engram.sqlite3")
    for job in store.jobs():
        listed = "listed" if job.listed else ""
        typer.echo(f"{job.id}  {job.status:<7}  {listed:<6}  {job.title}")


@app.command()
def delete(
    job_ids: Annotated[list[str], typer.Argument(help="The jobs to delete.")],
    yes: bool = typer.Option(False, "--yes", help="Don't ask first."),
) -> None:
    """Delete jobs: their folders, their files in R2 and their records. Not undoable.

    A course on the home page has to be hidden first (`engram list <job> --hide`), and a job
    that's running can't be deleted. Cached transcripts and readings are kept, so the same
    lecture uploaded again costs nothing.
    """
    import shutil

    from engram.config import load_settings
    from engram.pipeline import Store
    from engram.pipeline.runner import job_lock
    from engram.storage import storage_from

    settings = load_settings()
    store = Store(settings.data_dir / "engram.sqlite3")
    storage = storage_from(settings)
    jobs_root = settings.data_dir / "jobs"
    plans: list[tuple[str, str, list[str]]] = []
    for job_id in job_ids:
        try:
            job = store.job(job_id)
        except KeyError:
            typer.secho(f"✕ No job {job_id}", fg="red", err=True)
            raise typer.Exit(1) from None
        if job.listed:
            typer.secho(f"✕ {job_id} is on the home page. Hide it first.", fg="red", err=True)
            raise typer.Exit(1)
        with job_lock(jobs_root / job_id) as free:
            if not free or job.status == "running":
                typer.secho(f"✕ {job_id} is running.", fg="red", err=True)
                raise typer.Exit(1)
        keys = _stored_keys(jobs_root / job_id)  # may include the upload: it can play as is
        if job.source_key:
            keys.append(job.source_key)
        shared = any(j.source_key == job.source_key for j in store.jobs() if j.id != job_id)
        keys = [k for k in dict.fromkeys(keys) if not (shared and k == job.source_key)]
        plans.append((job_id, job.title, keys))
        where = f", {len(keys)} file{'s' if len(keys) != 1 else ''} in R2" if keys else ""
        typer.echo(f"{job_id}  {job.title}{where}")
    if not yes and not typer.confirm(f"Delete {len(plans)} job(s)?"):
        raise typer.Exit(1)
    for job_id, _, keys in plans:
        if keys and storage is None:
            typer.secho(f"✕ {job_id} has files in R2, but R2 isn't set up.", fg="red", err=True)
            raise typer.Exit(1)
        for key in keys:
            assert storage is not None
            storage.delete(key)
        shutil.rmtree(jobs_root / job_id, ignore_errors=True)
        store.delete_job(job_id)
        typer.echo(f"Deleted {job_id}.")


def _stored_keys(folder: Path) -> list[str]:
    """The R2 keys a job's playback and summary videos were uploaded to."""
    keys = []
    for name in ("playback.json", "summary.json"):
        path = folder / name
        if not path.exists():
            continue
        data = json.loads(path.read_text())
        entries = data.get("videos", {}).values() if name == "summary.json" else [data]
        keys += [e["key"] for e in entries if e.get("key")]
    return keys


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
