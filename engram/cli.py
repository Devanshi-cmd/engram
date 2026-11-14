"""Command line: `engram serve` runs the web app."""

from __future__ import annotations

import time
from pathlib import Path

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
) -> None:
    """Run the web app."""
    import uvicorn

    from engram.config import load_settings
    from engram.web.app import create_app

    uvicorn.run(create_app(load_settings(), allowed_hosts=allowed_host), host=host, port=port)


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
        typer.echo(f"{job.id}  {job.status:<7}  {job.title}")


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
