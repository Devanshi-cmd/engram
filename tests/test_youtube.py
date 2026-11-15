"""YouTube links: parsed, checked before a job exists, downloaded by the worker."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engram.config import Settings
from engram.media.youtube import video_id
from engram.pipeline import Runner
from engram.web.app import create_app
from engram.worker import run_queue
from tests.fakes import FakeYouTube
from tests.test_storage import FakeBucket

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "link",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/watch?v={VID}&t=42s&list=PL123",
        f"youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"https://m.youtube.com/watch?v={VID}",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=share",
        f"https://www.youtube.com/embed/{VID}",
        f"  https://www.youtube.com/watch?v={VID}  ",
    ],
)
def test_every_common_youtube_link_gives_the_video_id(link: str) -> None:
    assert video_id(link) == VID


@pytest.mark.parametrize(
    "link",
    ["", "hello", "https://vimeo.com/123456", f"https://notyoutube.com/watch?v={VID}",
     "https://www.youtube.com/watch?v=short", "https://www.youtube.com/@mitocw",
     "https://www.youtube.com/playlist?list=PL123", "javascript:alert(1)"],
)  # fmt: skip
def test_anything_else_is_not_a_youtube_video(link: str) -> None:
    assert video_id(link) is None


def app(settings: Settings, youtube: FakeYouTube, storage: FakeBucket | None = None) -> TestClient:
    s = Settings(data_dir=settings.data_dir, access_codes=frozenset({"let-me-in"}))
    return TestClient(create_app(s, allowed_hosts=["testserver"], accept_uploads=True,
                                 storage=storage, youtube=youtube))  # fmt: skip


def send(client: TestClient, link: str, code: str = "let-me-in", title: str = "") -> Any:
    return client.post("/links", json={"access_code": code, "url": link, "title": title})


@pytest.fixture
def youtube(lecture_video: Path) -> FakeYouTube:
    return FakeYouTube(lecture_video)


def test_a_link_is_checked_before_any_job_exists(settings: Settings, youtube: FakeYouTube) -> None:
    c = app(settings, youtube)
    link = f"https://youtu.be/{VID}"
    assert send(c, link, code="nope").status_code == 403
    bad = send(c, "https://vimeo.com/1")
    assert bad.status_code == 400 and "YouTube video" in bad.json()["error"]
    private = send(c, "https://youtu.be/xxxxxxxxxxx")
    assert "private" in private.json()["error"]
    youtube.add("aaaaaaaaaaa", "Too long", 3 * 3600)
    long = send(c, "https://youtu.be/aaaaaaaaaaa")
    assert long.status_code == 400 and "The limit is" in long.json()["error"]
    jobs = settings.data_dir / "jobs"
    assert not jobs.exists() or not any(jobs.iterdir())


def test_a_link_becomes_a_job_named_after_the_video_and_built_by_the_worker(
    runner: Runner, settings: Settings, youtube: FakeYouTube
) -> None:
    runner.services.youtube = youtube
    c = app(settings, youtube)
    first = send(c, f"https://www.youtube.com/watch?v={VID}&t=10s")
    assert first.status_code == 200, first.text
    job_id = first.json()["job"].rsplit("/", 1)[1]
    again = send(c, f"https://youtu.be/{VID}")  # the same video is the same job
    assert again.json()["job"].endswith(job_id)
    job = runner.store.job(job_id)
    assert job.title == "Linear Algebra, Lecture 1" and job.source_sha == f"youtube:{VID}"
    source = json.loads((runner.job_dir(job_id) / "source.json").read_text())
    assert source["url"] == f"https://www.youtube.com/watch?v={VID}"
    assert youtube.downloads == []  # the web app only reads the details

    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    assert runner.store.job(job_id).status == "done" and youtube.downloads == [VID]
    page = c.get(f"/courses/{job_id}").text
    assert f'href="https://www.youtube.com/watch?v={VID}"' in page and "on YouTube" in page
    events = [m for _, _, _, m in runner.store.events(job_id)]
    assert "Downloading the lecture from YouTube" in events


def test_links_count_against_the_daily_allowance(settings: Settings, youtube: FakeYouTube) -> None:
    c = app(settings, youtube)
    for i in range(3):
        youtube.add(f"video{i}abcde", f"Lecture {i}", 600)
        assert send(c, f"https://youtu.be/video{i}abcde").status_code == 200
    youtube.add("video3abcde", "Lecture 3", 600)
    over = send(c, "https://youtu.be/video3abcde")
    assert over.status_code == 429


def test_with_r2_the_download_is_kept_in_the_bucket_not_on_disk(
    runner: Runner, settings: Settings, youtube: FakeYouTube
) -> None:
    bucket = FakeBucket()
    runner.services.storage = bucket
    runner.services.youtube = youtube
    c = app(settings, youtube, storage=bucket)
    job_id = send(c, f"https://youtu.be/{VID}").json()["job"].rsplit("/", 1)[1]
    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    job = runner.store.job(job_id)
    key = f"uploads/youtube-{VID}.mp4"
    assert job.status == "done" and job.source_key == key and key in bucket.objects
    assert not job.source_path.exists()
    assert json.loads((runner.job_dir(job_id) / "playback.json").read_text()) == {"key": key}


def test_a_failed_download_fails_the_job_with_a_readable_message(
    runner: Runner, settings: Settings, youtube: FakeYouTube
) -> None:
    runner.services.youtube = youtube
    youtube.fail_download = "YouTube is refusing downloads from this server right now."
    c = app(settings, youtube)
    job_id = send(c, f"https://youtu.be/{VID}").json()["job"].rsplit("/", 1)[1]
    run_queue(runner, stop=lambda: runner.store.next_queued() is None)
    job = runner.store.job(job_id)
    assert job.status == "failed" and job.error and "refusing downloads" in job.error
    youtube.fail_download = None  # sending the link again retries it
    assert send(c, f"https://youtu.be/{VID}").status_code == 200
    assert runner.store.job(job_id).status == "queued"


def test_the_home_page_offers_a_link_field(settings: Settings, youtube: FakeYouTube) -> None:
    page = app(settings, youtube).get("/").text
    assert 'name="url"' in page and "paste a YouTube link" in page
