"""R2 mode: uploads go to the bucket, the worker fetches and cleans up, pages play signed links."""

import json
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from engram.config import Settings
from engram.pipeline import Runner
from engram.storage import R2Storage
from engram.web.app import create_app
from engram.worker import run_queue

ORIGIN = "https://acct.r2.cloudflarestorage.com"


class FakeBucket:
    """An in-memory R2: signed URLs are plain strings the test 'uploads' to by hand."""

    origin = ORIGIN

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fetched: list[str] = []

    def upload_url(self, key: str, size: int, content_type: str) -> str:
        return f"{ORIGIN}/bucket/{key}?signed=put&size={size}"

    def url(self, key: str, *, download: str | None = None) -> str:
        return f"{ORIGIN}/bucket/{key}?signed=get" + (f"&as={download}" if download else "")

    def head(self, key: str) -> dict[str, Any] | None:
        data = self.objects.get(key)
        return None if data is None else {"size": len(data), "etag": str(hash(data))}

    def put_file(self, path: Path, key: str, content_type: str) -> None:
        self.objects[key] = path.read_bytes()

    def get_file(self, key: str, path: Path) -> None:
        self.fetched.append(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.objects[key])

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def test_an_r2_upload_never_touches_the_server_disk_and_plays_from_r2(
    runner: Runner, settings: Settings, lecture_video: Path
) -> None:
    bucket = FakeBucket()
    runner.services.storage = bucket
    s = Settings(data_dir=settings.data_dir, access_codes=frozenset({"code"}))
    client = TestClient(create_app(s, allowed_hosts=["testserver"], accept_uploads=True,
                                   storage=bucket))  # fmt: skip
    size = lecture_video.stat().st_size
    slot = client.post("/uploads", json={"access_code": "code", "filename": "talk.mp4",
                                         "size": size}).json()  # fmt: skip
    assert (
        slot["put_url"].startswith(f"{ORIGIN}/bucket/uploads/")
        and f"size={size}" in slot["put_url"]
    )
    key = slot["put_url"].split("/bucket/")[1].split("?")[0]

    assert client.post(f"/uploads/{slot['id']}/done").status_code == 400  # nothing arrived yet
    bucket.objects[key] = lecture_video.read_bytes()[:-1]
    assert client.post(f"/uploads/{slot['id']}/done").status_code == 400  # incomplete
    bucket.objects[key] = lecture_video.read_bytes()  # what the browser's PUT does
    job_id = client.post(f"/uploads/{slot['id']}/done").json()["job"].rsplit("/", 1)[1]
    assert not (settings.data_dir / "uploads").exists()

    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    job = runner.store.job(job_id)
    assert job.status == "done" and bucket.fetched == [key]
    assert not job.source_path.exists()  # the local copy is gone once the job is done
    folder = runner.job_dir(job_id)
    assert json.loads((folder / "playback.json").read_text()) == {"key": key}  # plays as is
    videos = json.loads((folder / "summary.json").read_text())["videos"]
    assert set(videos) == {"summary.mp4", "summary-portrait.mp4"}
    assert not (folder / "summary.mp4").exists() and f"jobs/{job_id}/summary.mp4" in bucket.objects

    video = client.get(f"/courses/{job_id}/video", follow_redirects=False)
    assert video.status_code == 302 and video.headers["location"].startswith(f"{ORIGIN}/bucket/")
    saved = client.get(f"/courses/{job_id}/files/summary.mp4?download=1", follow_redirects=False)
    assert "&as=" in saved.headers["location"]
    csp = client.get("/").headers["Content-Security-Policy"]
    assert f"media-src 'self' {ORIGIN}" in csp and f"connect-src 'self' {ORIGIN}" in csp
    assert "summary.mp4" in client.get(f"/courses/{job_id}").text


def test_a_copy_made_for_browsers_goes_to_r2_and_not_the_disk(
    runner: Runner, lecture_video: Path, tmp_path: Path
) -> None:
    from engram.pipeline import start_job

    bucket = FakeBucket()
    runner.services.storage = bucket
    mkv = tmp_path / "talk.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(lecture_video), "-c", "copy",
                    str(mkv)], check=True)  # fmt: skip
    job, _ = start_job(runner, mkv)
    assert runner.run(job.id)
    playback = json.loads((runner.job_dir(job.id) / "playback.json").read_text())
    assert playback["key"] == f"jobs/{job.id}/playback.mp4" and playback["key"] in bucket.objects
    assert not (runner.job_dir(job.id) / "playback.mp4").exists()


def test_signed_links_carry_the_size_and_expire() -> None:
    r2 = R2Storage("acct", "AKIDEXAMPLE", "secret", "lectures")
    put = urlparse(r2.upload_url("uploads/a.mp4", 1234, "video/mp4"))
    assert put.netloc == "acct.r2.cloudflarestorage.com" and put.path == "/lectures/uploads/a.mp4"
    query = parse_qs(put.query)
    assert "content-length" in query["X-Amz-SignedHeaders"][0]  # a bigger file fails the check
    assert query["X-Amz-Expires"] == ["3600"]
    get = parse_qs(urlparse(r2.url("jobs/x/summary.mp4", download="s.mp4")).query)
    assert get["response-content-disposition"] == ['attachment; filename="s.mp4"']
