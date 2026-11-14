"""The web app's routes. Pages are rendered on the server, and HTMX refreshes the live parts.

The web app never processes a lecture itself: an upload becomes a queued job, and the
worker (`engram worker`) runs it. So restarting the site never interrupts a lecture.
"""

from __future__ import annotations

import hmac
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from engram import __version__
from engram.config import Settings
from engram.errors import EngramError
from engram.media import probe
from engram.pipeline import Store, start_job
from engram.pipeline.stages import STAGES
from engram.pipeline.store import Job
from engram.web import course as course_view
from engram.web import exports

_PACKAGE = Path(__file__).parent

# Pages show text read off lecture frames and written by models. Escaping is the main
# defence; this policy means a mistake there still can't run injected script. Styles may be
# inline only because KaTeX and Mermaid position maths and diagrams with style attributes.
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; connect-src 'self'; img-src 'self' data:; media-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# What one lecture becomes. The home page and the course page both use this list.
OUTPUTS = [
    ("notes", "Notes", "Chapters with equations, key terms and the slides that matter."),
    ("diagram", "Diagrams", "Whiteboard sketches redrawn as clean, readable diagrams."),
    ("cards", "Flashcards", "Cards to drill in the browser, or export to Anki."),
    ("quiz", "Quiz", "Questions checked against what was actually said."),
    ("video", "Summary video", "A narrated recap of about 80 seconds, also cut for LinkedIn."),
    ("sync", "Every moment linked", "Click any note, card or answer to jump to it in the lecture."),
]

VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
MAX_UPLOAD_BYTES = 2 * 1024**3


def create_app(
    settings: Settings,
    allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost"),
    *,
    accept_uploads: bool = False,
) -> FastAPI:
    """`accept_uploads` is on when a worker runs alongside (`engram serve --uploads`)."""
    app = FastAPI(title="Engram", docs_url=None, redoc_url=None, openapi_url=None)
    # Rejecting unknown Host headers stops a malicious site reading the app via DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=_PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=_PACKAGE / "templates")
    templates.env.globals["version"] = __version__
    templates.env.filters["clock"] = course_view.stamp
    store = Store(settings.data_dir / "engram.sqlite3")
    jobs_root = settings.data_dir / "jobs"
    uploads = accept_uploads and settings.uploads_enabled

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

    def home_page(request: Request, *, error: str | None = None, status: int = 200) -> Response:
        listed = []
        for job in store.jobs():
            files = course_view.load(jobs_root / job.id) if job.listed else None
            if files is not None:
                c = files.course
                listed.append({"id": job.id, "title": c["title"], "summary": c["overview"]})
        return templates.TemplateResponse(
            request,
            "home.html",
            {"outputs": OUTPUTS, "uploads_enabled": uploads, "courses": listed, "error": error},
            status_code=status,
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        return home_page(request)

    @app.post("/jobs")
    def upload(
        request: Request,
        video: UploadFile = File(...),  # noqa: B008
        access_code: str = Form(""),
        title: str = Form(""),
    ) -> Response:
        if not uploads:
            raise StarletteHTTPException(404)
        if not any(hmac.compare_digest(access_code.encode(), c.encode())
                   for c in settings.access_codes):  # fmt: skip
            return home_page(request, error="That access code isn't valid.", status=403)
        suffix = Path(video.filename or "").suffix.lower()
        if suffix not in VIDEO_TYPES:
            return home_page(request, error="Upload a video file: MP4, MOV, MKV or WebM.",
                             status=400)  # fmt: skip
        folder = settings.data_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{uuid.uuid4().hex}{suffix}"
        with target.open("wb") as out:
            shutil.copyfileobj(video.file, out, length=1024 * 1024)
        if target.stat().st_size > MAX_UPLOAD_BYTES:
            target.unlink()
            return home_page(request, error="That file is over 2 GB.", status=413)
        try:
            info = probe(target)
            if info.duration > settings.max_minutes * 60:
                raise EngramError(
                    f"This lecture is {info.duration / 60:.0f} minutes long. "
                    f"The limit is {settings.max_minutes} minutes."
                )
        except EngramError as e:
            target.unlink()
            return home_page(request, error=str(e), status=400)
        name = title.strip()[:120] or Path(video.filename or "Lecture").stem
        job, existed = start_job(store, target, name)
        if existed:
            target.unlink()  # the same bytes were uploaded before: reuse that job
            unfinished = not (jobs_root / job.id / "course.json").exists()
            if job.status == "failed" or (job.status == "done" and unfinished):
                store.set_job_status(job.id, "queued")  # retry, or catch up on newer stages
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    def find_job(job_id: str) -> Job:
        if not course_view.JOB_ID.match(job_id):
            raise StarletteHTTPException(404)
        try:
            return store.job(job_id)
        except KeyError:
            raise StarletteHTTPException(404) from None

    def job_context(job: Job) -> dict[str, Any]:
        rows = store.stages(job.id)
        stages = []
        for stage in STAGES:
            row = rows.get(stage.name)
            status = row.status if row else "pending"
            detail = (row.error if status == "failed" else row.detail) if row else None
            stages.append({"label": stage.label, "status": status, "detail": detail or "",
                           "seconds": row.seconds if row and not row.cached else None})  # fmt: skip
        done = sum(1 for s in stages if s["status"] == "done")
        ahead = [j for j in store.jobs() if j.status == "queued" and j.created_at < job.created_at]
        events = [(time.strftime("%H:%M:%S", time.localtime(at)), msg)
                  for _, _, at, msg in store.events(job.id)][-6:]  # fmt: skip
        return {
            "job": job,
            "stages": stages,
            "percent": round(100 * done / len(stages)),
            "ahead": len(ahead),
            "events": list(reversed(events)),
            "ready": (jobs_root / job.id / "course.json").exists(),
            "active": job.status in ("queued", "running"),
        }

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> Response:
        return templates.TemplateResponse(request, "job.html", job_context(find_job(job_id)))

    @app.get("/jobs/{job_id}/live", response_class=HTMLResponse)
    def job_live(request: Request, job_id: str) -> Response:
        context = job_context(find_job(job_id))
        response = templates.TemplateResponse(request, "_job_live.html", context)
        if context["job"].status == "done" and context["ready"]:
            response.headers["HX-Redirect"] = f"/courses/{job_id}"
        return response

    def course_files(job_id: str) -> course_view.CourseFiles:
        find_job(job_id)
        files = course_view.load(jobs_root / job_id)
        if files is None:
            raise StarletteHTTPException(404)
        return files

    @app.get("/courses/{job_id}", response_class=HTMLResponse)
    def course_page(request: Request, job_id: str) -> Response:
        files = course_files(job_id)
        return templates.TemplateResponse(
            request, "course.html", {"c": course_view.view(files, job_id), "outputs": OUTPUTS}
        )

    @app.get("/courses/{job_id}/print", response_class=HTMLResponse)
    def print_page(request: Request, job_id: str) -> Response:
        files = course_files(job_id)
        return templates.TemplateResponse(
            request, "print.html", {"c": course_view.view(files, job_id)}
        )

    @app.get("/courses/{job_id}/video")
    def lecture_video(job_id: str) -> Response:
        job = find_job(job_id)
        files = course_files(job_id)
        copy = files.folder / "playback.mp4"
        path = copy if copy.exists() else job.source_path
        if not path.exists():
            raise StarletteHTTPException(404)
        return FileResponse(path, media_type="video/mp4")  # supports Range: seeking works

    @app.get("/courses/{job_id}/files/{name:path}")
    def job_file(job_id: str, name: str) -> Response:
        if not course_view.PUBLIC_FILE.match(name):
            raise StarletteHTTPException(404)
        path = course_files(job_id).folder / name
        if not path.is_file():
            raise StarletteHTTPException(404)
        media = "text/vtt" if name.endswith(".vtt") else None
        return FileResponse(path, media_type=media, headers={"Cache-Control": "max-age=3600"})

    @app.get("/courses/{job_id}/transcript.json")
    def transcript(job_id: str) -> Response:
        folder = course_files(job_id).folder
        data = course_view.transcript(folder, (folder / "transcript.json").stat().st_mtime)
        return JSONResponse(data, headers={"Cache-Control": "max-age=600"})

    @app.get("/courses/{job_id}/export/notes.zip")
    def notes_zip(job_id: str) -> Response:
        files = course_files(job_id)
        name = exports.slug(files.course["title"])
        return Response(
            exports.markdown_zip(files.folder, files.course), media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
        )  # fmt: skip

    @app.get("/courses/{job_id}/export/anki.txt")
    def anki(job_id: str) -> Response:
        files = course_files(job_id)
        name = exports.slug(files.course["title"])
        return PlainTextResponse(
            exports.anki(files.course),
            headers={"Content-Disposition": f'attachment; filename="{name}-anki.txt"'},
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app
