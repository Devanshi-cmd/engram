"""Command line: `lectern serve` runs the web app."""

from __future__ import annotations

import typer

from lectern import __version__

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

    from lectern.config import load_settings
    from lectern.web.app import create_app

    uvicorn.run(create_app(load_settings(), allowed_hosts=allowed_host), host=host, port=port)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)
