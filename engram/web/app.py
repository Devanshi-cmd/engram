"""The web app's routes. Pages are rendered on the server, and HTMX refreshes the live parts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from http import HTTPStatus
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from engram import __version__
from engram.config import Settings

_PACKAGE = Path(__file__).parent

# Pages show text read off lecture frames and written by models. Escaping is the main
# defence; this policy means a mistake there still can't run injected script.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; media-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# What one lecture becomes. The home page and the course page both use this list.
OUTPUTS = [
    ("notes", "Notes", "Chapters with equations, key terms and the slides that matter."),
    ("diagram", "Diagrams", "Whiteboard sketches redrawn as clean, readable diagrams."),
    ("cards", "Flashcards", "Spaced-repetition cards, exportable to Anki."),
    ("quiz", "Quiz", "Questions checked against what was actually said."),
    ("video", "Summary video", "A 90-second narrated recap, also cut for LinkedIn."),
    ("sync", "Every moment linked", "Click any note, card or answer to jump to it in the lecture."),
]


def create_app(
    settings: Settings,
    allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost"),
    *,
    accept_uploads: bool = False,
) -> FastAPI:
    """`accept_uploads` stays off until the processing pipeline is wired in, so the form
    never offers something the server can't do."""
    app = FastAPI(title="Engram", docs_url=None, redoc_url=None, openapi_url=None)
    # Rejecting unknown Host headers stops a malicious site reading the app via DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=_PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=_PACKAGE / "templates")
    templates.env.globals["version"] = __version__

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    # Registered on Starlette's base class, which unknown URLs raise, so they get it too.
    @app.exception_handler(StarletteHTTPException)
    async def themed_error(request: Request, exc: StarletteHTTPException) -> Response:
        message = HTTPStatus(exc.status_code).phrase
        return templates.TemplateResponse(
            request,
            "error.html",
            {"status_code": exc.status_code, "message": message},
            status_code=exc.status_code,
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "outputs": OUTPUTS,
                "uploads_enabled": accept_uploads and settings.uploads_enabled,
                "courses": [],
            },
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
