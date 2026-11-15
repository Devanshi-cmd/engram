"""The web app's routes. Pages are rendered on the server, and HTMX refreshes the live parts.

The web app never processes a lecture itself: an upload becomes a queued job, and the
worker (`engram worker`) runs it. So restarting the site never interrupts a lecture.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Iterable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
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
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from engram import __version__
from engram.config import Settings
from engram.errors import EngramError
from engram.media import probe
from engram.pipeline import Store, start_job
from engram.pipeline.stages import STAGES
from engram.pipeline.store import Job
from engram.storage import Storage, storage_from
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

VIDEO_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".avi": "video/x-msvideo",
}  # fmt: skip
UPLOAD_ID = re.compile(r"^[0-9a-f]{32}$")
WRONG_CODE_LIMIT, WRONG_CODE_WINDOW = 10, 600.0  # per address: guessing codes gets slow
MAX_UPLOAD_BYTES = 2 * 1024**3
DAY = 24 * 3600.0
MAX_QUEUED = 5  # more waiting than this and a new upload is turned away, not left for hours
MIN_FREE_BYTES = 4 * 1024**3  # an upload, its web copy and its frames need room


class HeadAsGet:
    """Answers HEAD like GET, without the body. FastAPI routes only answer the methods they
    declare, so uptime monitors and link checkers that send HEAD got 405 for every page."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "HEAD":
            await self.app(scope, receive, send)
            return
        finished = False

        async def headers_only(message: Message) -> None:
            nonlocal finished
            if message["type"] != "http.response.body":
                await send(message)
            elif not finished:
                finished = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})

        await self.app({**scope, "method": "GET"}, receive, headers_only)


def _asset_version(static: Path) -> str:
    """A short hash of every static file, so `?v=` changes whenever a stylesheet or script does
    and browsers never keep a stale copy (the package version only moves on a release)."""
    digest = hashlib.sha256(__version__.encode())
    for path in sorted(static.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(static).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def create_app(
    settings: Settings,
    allowed_hosts: Iterable[str] = ("127.0.0.1", "localhost"),
    *,
    accept_uploads: bool = False,
    storage: Storage | None = None,
) -> FastAPI:
    """`accept_uploads` is on when a worker runs alongside (`engram serve --uploads`)."""
    app = FastAPI(title="Engram", docs_url=None, redoc_url=None, openapi_url=None)
    # Rejecting unknown Host headers stops a malicious site reading the app via DNS rebinding.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=_PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=_PACKAGE / "templates")
    templates.env.globals["version"] = _asset_version(_PACKAGE / "static")
    templates.env.filters["clock"] = course_view.stamp
    store = Store(settings.data_dir / "engram.sqlite3")
    jobs_root = settings.data_dir / "jobs"
    uploads = accept_uploads and settings.uploads_enabled
    storage = storage or storage_from(settings)
    wrong_codes: dict[str, list[float]] = {}
    headers = dict(SECURITY_HEADERS)
    if storage is not None:  # pages play from, and upload to, the R2 bucket's own address
        csp = headers["Content-Security-Policy"]
        csp = csp.replace("connect-src 'self'", f"connect-src 'self' {storage.origin}")
        headers["Content-Security-Policy"] = csp.replace(
            "media-src 'self'", f"media-src 'self' {storage.origin}"
        )

    @app.middleware("http")
    async def add_security_headers(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.update(headers)
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
                listed.append({"id": job.id, "title": c["title"], "summary": c["overview"],
                               "credit": files.read("credit.json"),
                               "thumb": (jobs_root / job.id / "summary.jpg").exists()})  # fmt: skip
        return templates.TemplateResponse(
            request,
            "home.html",
            {"outputs": OUTPUTS, "uploads_enabled": uploads, "courses": listed, "error": error},
            status_code=status,
        )

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> Response:
        return home_page(request)

    # ---- Uploads: the access code is checked before a single byte of video is sent.
    #  1. POST /uploads           {access_code, filename, size, title} → a one-time upload slot
    #  2. PUT  <slot's put_url>   the file: straight to R2 (signed URL) or to this server
    #  3. POST /uploads/{id}/done → the job, queued for the worker

    def refused(message: str, status: int) -> Response:
        return JSONResponse({"error": message}, status_code=status)

    def client_ip(request: Request) -> str:
        return request.headers.get("x-real-ip") or (request.client.host if request.client else "")

    @app.post("/uploads")
    async def start_upload(request: Request) -> Response:
        if not uploads:
            raise StarletteHTTPException(404)
        ip = client_ip(request)
        now = time.time()
        recent = [t for t in wrong_codes.get(ip, []) if now - t < WRONG_CODE_WINDOW]
        wrong_codes[ip] = recent
        if len(recent) >= WRONG_CODE_LIMIT:
            return refused("Too many wrong access codes. Try again in 10 minutes.", 429)
        try:
            body = await request.json()
            code, filename = str(body.get("access_code", "")), str(body.get("filename", ""))
            size, title = int(body.get("size", 0)), str(body.get("title", ""))
        except (ValueError, TypeError, AttributeError):
            return refused("The upload request was malformed.", 400)
        if not any(hmac.compare_digest(code.encode(), c.encode()) for c in settings.access_codes):
            recent.append(now)
            return refused("That access code isn't valid.", 403)
        code_id = hashlib.sha256(code.encode()).hexdigest()[:16]
        used = store.uploads_since(code_id, now - DAY)
        if len(used) >= settings.daily_uploads:
            hours = max(1, math.ceil((used[0] + DAY - now) / 3600))
            return refused(
                f"This access code has used its {settings.daily_uploads} lectures for the day. "
                f"Try again in {hours} hour{'s' if hours != 1 else ''}.", 429)  # fmt: skip
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_TYPES:
            return refused("Upload a video file: MP4, MOV, MKV or WebM.", 400)
        if not 0 < size <= MAX_UPLOAD_BYTES:
            return refused("That file is over 2 GB.", 413)
        if sum(1 for j in store.jobs() if j.status == "queued") >= MAX_QUEUED:
            return refused("Engram is busy with other lectures. Try again in an hour.", 503)
        if storage is None and shutil.disk_usage(settings.data_dir).free < MIN_FREE_BYTES + size:
            return refused("The server is short of disk space, so uploads are paused.", 507)
        content_type = VIDEO_TYPES[suffix]
        name = title.strip()[:120] or Path(filename).stem[:120] or "Lecture"
        slot = store.create_upload("uploads/{id}" + suffix, filename[:200], name, size,
                                   content_type, code_id)  # fmt: skip
        put_url = (storage.upload_url(slot.key, size, content_type) if storage
                   else f"/uploads/{slot.id}/file")  # fmt: skip
        return JSONResponse({"id": slot.id, "put_url": put_url, "content_type": content_type})

    @app.put("/uploads/{upload_id}/file")
    async def receive_file(request: Request, upload_id: str) -> Response:
        """Without R2, the file comes here, streamed to disk, never more than was declared."""
        slot = store.upload(upload_id) if UPLOAD_ID.match(upload_id) else None
        if storage is not None or slot is None or slot.job_id:
            raise StarletteHTTPException(404)
        target = settings.data_dir / slot.key
        target.parent.mkdir(parents=True, exist_ok=True)
        received = 0
        with target.open("wb") as out:
            async for chunk in request.stream():
                received += len(chunk)
                if received > slot.size:
                    out.close()
                    target.unlink()
                    return refused("The file is bigger than declared.", 413)
                out.write(chunk)
        return JSONResponse({"received": received})

    @app.post("/uploads/{upload_id}/done")
    def finish_upload(upload_id: str) -> Response:
        slot = store.upload(upload_id) if UPLOAD_ID.match(upload_id) else None
        if slot is None:
            return refused("This upload has expired. Choose the file again.", 404)
        if slot.job_id:
            return JSONResponse({"job": f"/jobs/{slot.job_id}"})
        if storage is not None:
            found = storage.head(slot.key)
            if found is None or found["size"] != slot.size:
                return refused("The upload didn't arrive complete. Try again.", 400)
            # The worker downloads it here when the job runs, and deletes it afterwards.
            local = settings.data_dir / "work" / slot.key
            sha = f"r2:{found['etag']}:{found['size']}"
            existing = store.find_by_source(sha)
            if existing is not None:
                storage.delete(slot.key)  # the same bytes were uploaded before
                job = existing
            else:
                job = store.create_job(slot.title, local, sha, source_key=slot.key)
        else:
            target = settings.data_dir / slot.key
            if not target.exists() or target.stat().st_size != slot.size:
                return refused("The upload didn't arrive complete. Try again.", 400)
            try:
                info = probe(target)
                if info.duration > settings.max_minutes * 60:
                    raise EngramError(
                        f"This lecture is {info.duration / 60:.0f} minutes long. "
                        f"The limit is {settings.max_minutes} minutes."
                    )
            except EngramError as e:
                target.unlink()
                return refused(str(e), 400)
            job, existed = start_job(store, target, slot.title)
            if existed:
                target.unlink()  # the same bytes were uploaded before: reuse that job
                existing = job
            else:
                existing = None
        if existing is not None:
            unfinished = not (jobs_root / job.id / "course.json").exists()
            if job.status == "failed" or (job.status == "done" and unfinished):
                store.set_job_status(job.id, "queued")  # retry, or catch up on newer stages
        store.finish_upload(slot.id, job.id)
        return JSONResponse({"job": f"/jobs/{job.id}"})

    @app.post("/jobs")
    def upload_without_script(request: Request) -> Response:
        """The form needs JavaScript: it checks the access code before sending the file."""
        if not uploads:
            raise StarletteHTTPException(404)
        return home_page(request, error="Uploading needs JavaScript turned on.", status=400)

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
        find_job(job_id)
        if course_view.load(jobs_root / job_id) is None:  # not built yet: show its progress
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
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
        playback = files.read("playback.json") or {}
        if playback.get("key"):
            return remote(playback["key"])
        copy = files.folder / "playback.mp4"
        path = copy if copy.exists() else job.source_path
        if not path.exists():
            raise StarletteHTTPException(404)
        return FileResponse(path, media_type="video/mp4")  # supports Range: seeking works

    def remote(key: str, download: str | None = None) -> Response:
        """A short-lived signed link to the file in R2 (the bucket itself stays private)."""
        if storage is None:
            raise StarletteHTTPException(404)
        return RedirectResponse(storage.url(key, download=download), status_code=302)

    @app.get("/courses/{job_id}/files/{name:path}")
    def job_file(job_id: str, name: str, download: int = 0) -> Response:
        if not course_view.PUBLIC_FILE.match(name):
            raise StarletteHTTPException(404)
        files = course_files(job_id)
        where = (files.read("summary.json") or {}).get("videos", {}).get(name, {})
        if where.get("key"):
            title = exports.slug(files.course["title"])
            label = f"{title}-{name}" if download else None
            return remote(where["key"], label)
        path = files.folder / name
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

    @app.get("/how-it-works", response_class=HTMLResponse)
    def how_it_works(request: Request) -> Response:
        return templates.TemplateResponse(request, "how_it_works.html", {})

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    app.add_middleware(HeadAsGet)  # outermost: every page answers HEAD
    return app
