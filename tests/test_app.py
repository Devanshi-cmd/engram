"""M6: uploads, the worker queue, the live job page, the course page and its downloads."""

import io
import json
import re
import shutil
import sqlite3
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from engram.config import Settings
from engram.media import make_web_copy, web_playable
from engram.pipeline import Runner, Store, start_job
from engram.speech import Word
from engram.speech.fake import FakeTranscriber
from engram.web.app import create_app
from engram.web.course import md
from engram.web.exports import anki
from engram.worker import run_queue
from tests import synthetic
from tests.conftest import make_video


def web(settings: Settings, *, uploads: bool = True) -> TestClient:
    s = Settings(data_dir=settings.data_dir, access_codes=frozenset({"let-me-in"}))
    return TestClient(create_app(s, allowed_hosts=["testserver"], accept_uploads=uploads))


@pytest.fixture
def finished(runner: Runner, tmp_path: Path, transcriber: FakeTranscriber) -> str:
    text = "so here we write h of x equals the sum of theta j x j " * 30
    transcriber.words = [Word(text=w, start=i * 0.25, end=i * 0.25 + 0.2)
                         for i, w in enumerate(text.split())]  # fmt: skip
    job, _ = start_job(runner, synthetic.render(tmp_path / "board.mp4"), "Synthetic")
    assert runner.run(job.id)
    return job.id


# --- Uploads and the queue ---------------------------------------------------------------


def upload(client: TestClient, video: Path, code: str = "let-me-in", title: str = "") -> Any:
    """What the page's script does: the code first, then the file, then 'done'."""
    slot = client.post("/uploads", json={"access_code": code, "filename": video.name,
                                         "size": video.stat().st_size, "title": title})  # fmt: skip
    if slot.status_code != 200:
        return slot
    body = slot.json()
    sent = client.put(body["put_url"], content=video.read_bytes(),
                      headers={"Content-Type": body["content_type"]})  # fmt: skip
    assert sent.status_code == 200, sent.text
    return client.post(f"/uploads/{body['id']}/done")


def test_an_upload_is_queued_and_the_worker_builds_it(
    runner: Runner, settings: Settings, lecture_video: Path
) -> None:
    client = web(settings)
    done = upload(client, lecture_video, title="My lecture")
    assert done.status_code == 200
    job_id = done.json()["job"].rsplit("/", 1)[1]
    job = runner.store.job(job_id)
    assert job.status == "queued" and job.title == "My lecture"
    assert job.source_path.parent == settings.data_dir / "uploads"
    page = client.get(f"/jobs/{job_id}").text
    assert "Waiting for its turn" in page and 'hx-trigger="every 2s"' in page

    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    assert runner.store.job(job_id).status == "done"
    live = client.get(f"/jobs/{job_id}/live")
    assert live.headers["HX-Redirect"] == f"/courses/{job_id}"


def test_a_wrong_code_gets_no_upload_slot_and_guessing_slows_down(
    settings: Settings, lecture_video: Path
) -> None:
    client = web(settings)
    for _ in range(10):
        r = upload(client, lecture_video, code="guess")
        assert r.status_code == 403 and r.json()["error"] == "That access code isn't valid."
    assert upload(client, lecture_video).status_code == 429  # even the right code, for a while
    assert not (settings.data_dir / "uploads").exists()  # not a byte was stored


def test_an_upload_slot_takes_only_what_was_declared_and_only_once(
    settings: Settings, lecture_video: Path
) -> None:
    client = web(settings)
    body = client.post("/uploads", json={"access_code": "let-me-in", "filename": "a.mp4",
                                         "size": 10}).json()  # fmt: skip
    r = client.put(body["put_url"], content=b"x" * 11)
    assert r.status_code == 413 and not list((settings.data_dir / "uploads").iterdir())
    assert client.put("/uploads/" + "0" * 32 + "/file", content=b"x").status_code == 404
    assert client.post("/uploads/" + "0" * 32 + "/done").status_code == 404


def test_the_same_video_twice_reuses_the_job(settings: Settings, lecture_video: Path) -> None:
    client = web(settings)
    first = upload(client, lecture_video).json()["job"]
    assert upload(client, lecture_video).json()["job"] == first
    assert len(list((settings.data_dir / "uploads").iterdir())) == 1  # the copy was dropped


@pytest.mark.parametrize(
    ("name", "content", "status", "message"),
    [
        ("notes.pdf", b"%PDF", 400, "Upload a video"),
        ("fake.mp4", b"not a video at all", 400, ""),
    ],
)
def test_bad_uploads_are_refused_and_nothing_is_kept(
    settings: Settings, tmp_path: Path, name: str, content: bytes, status: int, message: str
) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    r = upload(web(settings), path)
    assert r.status_code == status and message in r.json()["error"]
    uploads = settings.data_dir / "uploads"
    assert not uploads.exists() or not list(uploads.iterdir())
    assert Store(settings.data_dir / "engram.sqlite3").jobs() == []


def test_without_uploads_the_endpoints_do_not_exist(settings: Settings) -> None:
    client = web(settings, uploads=False)
    assert client.post("/uploads", json={}).status_code == 404
    assert client.post("/jobs").status_code == 404
    assert "needs JavaScript" in web(settings).post("/jobs").text


def test_a_stopped_worker_leaves_nothing_stuck(runner: Runner, lecture_video: Path) -> None:
    job, _ = start_job(runner, lecture_video)
    runner.store.set_job_status(job.id, "running")  # the worker died mid-job
    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    assert runner.store.job(job.id).status == "done"


def test_a_worker_starting_up_leaves_a_job_another_process_is_running(
    runner: Runner, lecture_video: Path
) -> None:
    from engram.pipeline.runner import job_lock

    job, _ = start_job(runner, lecture_video)
    runner.store.set_job_status(job.id, "running")
    with job_lock(runner.job_dir(job.id)) as mine:  # `engram run` in a shell, mid-job
        assert mine and runner.busy(job.id)
        assert run_queue(runner, stop=lambda: True) == 0
        assert runner.store.job(job.id).status == "running"  # not taken over
        assert runner.run(job.id) is False  # a second runner backs off, changing nothing
        assert runner.store.job(job.id).status == "running"
    assert not runner.busy(job.id)  # the lock goes with the process that held it
    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    assert runner.store.job(job.id).status == "done"


def test_a_crashing_job_does_not_stop_the_queue(runner: Runner, lecture_video: Path) -> None:
    job, _ = start_job(runner, lecture_video)

    def boom(job_id: str, sink: object = None) -> bool:
        raise RuntimeError("bug")

    runner.run = boom  # type: ignore[method-assign]
    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    assert runner.store.job(job.id).status == "failed"


# --- The course page ------------------------------------------------------------------


def test_the_course_page_has_everything_linked_to_the_lecture(
    finished: str, settings: Settings
) -> None:
    page = web(settings).get(f"/courses/{finished}").text
    assert '<video id="player"' in page and f"/courses/{finished}/video" in page
    for tab in ("Notes", "Diagrams", "Flashcards", "Quiz", "Summary"):
        assert f">{tab}</button>" in page
    assert page.count('class="moment"') > 5  # timestamps that seek the player
    assert '<span class="tex">h(x) = \\sum_j \\theta_j x_j</span>' in page
    assert 'class="mermaid-source"' in page and "filmstrip-frame" in page
    assert "/static/vendor/katex/katex.min.js" in page


def test_the_quiz_answer_moves_but_stays_correct(finished: str, settings: Settings) -> None:
    page = web(settings).get(f"/courses/{finished}").text
    answer = int(re.search(r'class="question card" data-answer="(\d)"', page).group(1))  # type: ignore[union-attr]
    options = re.findall(r'class="option-text">([^<]+)<', page)[:4]
    assert options[answer] == "A sum"  # the fake's right answer, wherever it landed


def test_media_is_served_with_seeking_and_nothing_private_is(
    finished: str, settings: Settings
) -> None:
    c = web(settings)
    video = c.get(f"/courses/{finished}/video", headers={"Range": "bytes=0-99"})
    assert video.status_code == 206 and len(video.content) == 100
    course = json.loads((settings.data_dir / "jobs" / finished / "course.json").read_text())
    frame = next(iter(course["frames"]))
    assert c.get(f"/courses/{finished}/files/thumbs/{frame}.jpg").status_code == 200
    assert (
        c.get(f"/courses/{finished}/files/summary.vtt")
        .headers["content-type"]
        .startswith("text/vtt")
    )
    for private in ("course.json", "transcript.json", "audio.mp3", "../../engram.sqlite3",
                    "frames/../course.json", "engram.log"):  # fmt: skip
        assert c.get(f"/courses/{finished}/files/{private}").status_code == 404, private
    assert c.get("/courses/nothexatall/video").status_code == 404
    assert c.get("/courses/0123456789ab").status_code == 404


def test_the_transcript_marks_students_and_times_words(finished: str, settings: Settings) -> None:
    data = web(settings).get(f"/courses/{finished}/transcript.json").json()
    first = data["paragraphs"][0]
    assert first["w"][0] == [0.0, 0.2, "so"] and first["who"] == ""


def test_exports_unzip_and_import(finished: str, settings: Settings) -> None:
    c = web(settings)
    z = zipfile.ZipFile(io.BytesIO(c.get(f"/courses/{finished}/export/notes.zip").content))
    names = z.namelist()
    md_file = next(n for n in names if n.endswith(".md"))
    assert any(n.endswith(".jpg") for n in names)  # the frames the notes show
    for ref in re.findall(r"\((frames/f\d{4}\.jpg)\)", z.read(md_file).decode()):
        assert f"{md_file.split('/')[0]}/{ref}" in names
    deck = c.get(f"/courses/{finished}/export/anki.txt")
    assert "attachment" in deck.headers["content-disposition"]
    assert deck.text.startswith("#separator:tab\n#html:true\n#deck:Engram::A synthetic lecture")
    print_page = c.get(f"/courses/{finished}/print").text
    assert "Answers" in print_page and "Save as PDF" in print_page


def test_only_listed_courses_reach_the_home_page(finished: str, settings: Settings) -> None:
    c = web(settings)
    assert f"/courses/{finished}" not in c.get("/").text
    Store(settings.data_dir / "engram.sqlite3").set_listed(finished, True)
    assert f"/courses/{finished}" in c.get("/").text


def test_a_listed_lecture_credits_its_source_and_licence(
    finished: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from engram.cli import app as cli

    monkeypatch.setenv("ENGRAM_DATA_DIR", str(settings.data_dir))
    half = CliRunner().invoke(cli, ["list", finished, "--source", "MIT OpenCourseWare"])
    assert half.exit_code == 1  # a credit without its licence is refused
    ok = CliRunner().invoke(cli, ["list", finished, "--source", "MIT OpenCourseWare",
                                  "--url", "https://ocw.mit.edu/x", "--license", "CC BY-NC-SA 4.0",
                                  "--notice", "Free at ocw.mit.edu."])  # fmt: skip
    assert ok.exit_code == 0, ok.output
    c = web(settings)
    page = c.get(f"/courses/{finished}").text
    assert 'href="https://ocw.mit.edu/x"' in page and "CC BY-NC-SA 4.0" in page
    assert "shared under the same licence" in page and "Free at ocw.mit.edu." in page
    assert "MIT OpenCourseWare · CC BY-NC-SA 4.0" in c.get("/").text


# --- Text and formats ----------------------------------------------------------------------


def test_maths_survives_markdown_and_dollars_stay_dollars() -> None:
    html = str(md("Price in $1000s: $x_1 = 2104$ and **bold** with <b>html</b>", inline=True))
    assert html == (
        'Price in $1000s: <span class="tex">x_1 = 2104</span> and <strong>bold</strong> '
        "with &lt;b&gt;html&lt;/b&gt;"
    )
    assert 'class="tex tex-display">J = \\frac12</span>' in str(md("$$J = \\frac12$$"))


def test_anki_cards_use_mathjax_and_escape_html() -> None:
    course = {"title": "T", "chapters": [{"number": 1, "title": "Ch", "flashcards": [
        {"front": "What is $\\theta$ <i>?</i>", "back": "**It**\tis", "t": 65.0}]}]}  # fmt: skip
    line = anki(course).splitlines()[-1]
    front, back, tags = line.split("\t")
    assert front == "What is \\(\\theta\\) &lt;i&gt;?&lt;/i&gt;"
    assert back.startswith("<b>It</b> is") and "lecture at 01:05" in back
    assert tags == "engram ch1-ch"


def test_only_browser_ready_videos_skip_the_web_copy(tmp_path: Path) -> None:
    mp4 = make_video(tmp_path / "ok.mp4", 2)
    assert web_playable(mp4)
    mkv = make_video(tmp_path / "talk.mkv", 2)
    assert not web_playable(mkv)
    seen: list[float] = []
    make_web_copy(mkv, tmp_path / "copy.mp4", 2.0, seen.append)
    assert web_playable(tmp_path / "copy.mp4")


def test_a_job_folder_survives_a_missing_source(finished: str, settings: Settings) -> None:
    store = Store(settings.data_dir / "engram.sqlite3")
    shutil.move(store.job(finished).source_path, settings.data_dir / "moved.mp4")
    c = web(settings)
    assert c.get(f"/courses/{finished}").status_code == 200  # the course still reads
    assert c.get(f"/courses/{finished}/video").status_code == 404


def test_a_course_link_for_an_unfinished_job_shows_its_progress(
    runner: Runner, settings: Settings, lecture_video: Path
) -> None:
    job, _ = start_job(runner, lecture_video)
    r = web(settings).get(f"/courses/{job.id}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/jobs/{job.id}"


def test_a_full_queue_turns_uploads_away(
    runner: Runner, settings: Settings, lecture_video: Path, tmp_path: Path
) -> None:
    from engram.web.app import MAX_QUEUED

    for i in range(MAX_QUEUED):
        copy = tmp_path / f"v{i}.mp4"
        copy.write_bytes(lecture_video.read_bytes() + bytes([i]))  # different bytes, new job
        start_job(runner, copy)
    r = upload(web(settings), lecture_video)
    assert r.status_code == 503 and "busy" in r.json()["error"]


def test_each_access_code_gets_a_few_lectures_a_day(
    settings: Settings, lecture_video: Path, tmp_path: Path
) -> None:
    s = Settings(data_dir=settings.data_dir, access_codes=frozenset({"ana", "bo"}),
                 daily_uploads=2)  # fmt: skip
    client = TestClient(create_app(s, allowed_hosts=["testserver"], accept_uploads=True))
    videos = []
    for i in range(3):
        copy = tmp_path / f"talk{i}.mp4"
        copy.write_bytes(lecture_video.read_bytes() + bytes([i]))
        videos.append(copy)
    assert all(upload(client, v, "ana").status_code == 200 for v in videos[:2])
    refused = upload(client, videos[2], "ana")
    assert refused.status_code == 429 and "2 lectures for the day" in refused.json()["error"]
    assert "24 hours" in refused.json()["error"]
    assert upload(client, videos[2], "bo").status_code == 200  # another code has its own count
    slot = client.post("/uploads", json={"access_code": "ana", "filename": "x.mp4", "size": 9})
    assert slot.status_code == 429  # no slot at all, so nothing can be sent

    with sqlite3.connect(s.data_dir / "engram.sqlite3") as db:  # a day later
        db.execute("UPDATE uploads SET created_at = created_at - 86400")
    assert upload(client, videos[2], "ana").status_code == 200


def test_the_how_it_works_page_walks_through_every_step(settings: Settings) -> None:
    page = web(settings).get("/how-it-works")
    assert page.status_code == 200
    for step in ("Listen", "Watch", "Read", "Write", "Check", "Course"):
        assert f'<span class="hiw-node-label">{step}</span>' in page.text
    assert page.text.count('class="hiw-caption') == page.text.count('class="hiw-dot')
    assert 'href="/how-it-works"' in web(settings).get("/").text  # linked from every page


def test_links_pasted_elsewhere_preview_with_an_image(finished: str, settings: Settings) -> None:
    c = web(settings)
    home = c.get("/").text
    assert '<meta property="og:image" content="http://testserver/static/og.png">' in home
    assert c.get("/static/og.png").status_code == 200
    course = c.get(f"/courses/{finished}").text
    assert f'content="http://testserver/courses/{finished}/files/summary.jpg"' in course
    assert '<meta property="og:title" content="' in course and "· Engram" in course


def test_every_page_answers_head_without_a_body(finished: str, settings: Settings) -> None:
    c = web(settings)
    for path in ("/", "/how-it-works", f"/courses/{finished}", "/healthz"):
        r = c.head(path)
        assert r.status_code == 200 and r.content == b"", path
        assert int(r.headers["content-length"]) > 0  # what a GET would send
    assert c.head("/no-such-page").status_code == 404
    assert c.head("/uploads").status_code == 405  # POST-only stays POST-only


def test_deleting_jobs_cleans_up_r2_and_refuses_listed_ones(
    runner: Runner, settings: Settings, lecture_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from engram.cli import app as cli
    from tests.test_storage import FakeBucket

    bucket = FakeBucket()
    runner.services.storage = bucket
    monkeypatch.setattr("engram.storage.storage_from", lambda _s: bucket)
    monkeypatch.setenv("ENGRAM_DATA_DIR", str(settings.data_dir))
    bucket.objects["uploads/a.mp4"] = lecture_video.read_bytes()
    job = runner.store.create_job("Talk", runner.job_dir("x") / "a.mp4", "r2:e:1",
                                  source_key="uploads/a.mp4")  # fmt: skip
    assert runner.run(job.id)
    assert len(bucket.objects) == 3  # the upload (played as is) and two summary videos
    other, _ = start_job(runner, lecture_video)

    runner.store.set_listed(job.id, True)
    assert CliRunner().invoke(cli, ["delete", job.id, "--yes"]).exit_code == 1
    assert runner.store.job(job.id)  # a listed course is kept
    runner.store.set_listed(job.id, False)
    done = CliRunner().invoke(cli, ["delete", job.id, "--yes"])
    assert done.exit_code == 0, done.output
    assert bucket.objects == {} and not runner.job_dir(job.id).exists()
    with pytest.raises(KeyError):
        runner.store.job(job.id)
    assert runner.store.job(other.id)  # nothing else touched
