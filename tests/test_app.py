"""M6: uploads, the worker queue, the live job page, the course page and its downloads."""

import io
import json
import re
import shutil
import zipfile
from pathlib import Path

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


def test_an_upload_is_queued_and_the_worker_builds_it(
    runner: Runner, settings: Settings, lecture_video: Path
) -> None:
    client = web(settings)
    with lecture_video.open("rb") as f:
        response = client.post(
            "/jobs", data={"access_code": "let-me-in", "title": "My lecture"},
            files={"video": ("talk.mp4", f, "video/mp4")}, follow_redirects=False,
        )  # fmt: skip
    assert response.status_code == 303
    job_id = response.headers["location"].rsplit("/", 1)[1]
    job = runner.store.job(job_id)
    assert job.status == "queued" and job.title == "My lecture"
    assert job.source_path.parent == settings.data_dir / "uploads"
    page = client.get(f"/jobs/{job_id}").text
    assert "Waiting for its turn" in page and 'hx-trigger="every 2s"' in page

    assert run_queue(runner, stop=lambda: runner.store.next_queued() is None) == 1
    assert runner.store.job(job_id).status == "done"
    live = client.get(f"/jobs/{job_id}/live")
    assert live.headers["HX-Redirect"] == f"/courses/{job_id}"


def test_the_same_video_twice_reuses_the_job(settings: Settings, lecture_video: Path) -> None:
    client = web(settings)
    ids = []
    for _ in range(2):
        with lecture_video.open("rb") as f:
            r = client.post("/jobs", data={"access_code": "let-me-in"},
                            files={"video": ("a.mp4", f)}, follow_redirects=False)  # fmt: skip
        ids.append(r.headers["location"])
    assert ids[0] == ids[1]
    assert len(list((settings.data_dir / "uploads").iterdir())) == 1  # the copy was dropped


@pytest.mark.parametrize(
    ("code", "name", "content", "status", "message"),
    [
        ("wrong", "a.mp4", b"x", 403, "access code"),
        ("let-me-in", "notes.pdf", b"%PDF", 400, "Upload a video"),
        ("let-me-in", "fake.mp4", b"not a video at all", 400, ""),
    ],
)
def test_bad_uploads_are_refused_and_nothing_is_kept(
    settings: Settings, code: str, name: str, content: bytes, status: int, message: str
) -> None:
    r = web(settings).post("/jobs", data={"access_code": code},
                           files={"video": (name, content)})  # fmt: skip
    assert r.status_code == status and 'role="alert"' in r.text and message in r.text
    uploads = settings.data_dir / "uploads"
    assert not uploads.exists() or not list(uploads.iterdir())
    assert Store(settings.data_dir / "engram.sqlite3").jobs() == []


def test_without_uploads_the_endpoint_does_not_exist(settings: Settings) -> None:
    r = web(settings, uploads=False).post("/jobs", files={"video": ("a.mp4", b"x")})
    assert r.status_code == 404


def test_a_stopped_worker_leaves_nothing_stuck(runner: Runner, lecture_video: Path) -> None:
    job, _ = start_job(runner, lecture_video)
    runner.store.set_job_status(job.id, "running")  # the worker died mid-job
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
    with lecture_video.open("rb") as f:
        r = web(settings).post("/jobs", data={"access_code": "let-me-in"},
                               files={"video": ("a.mp4", f)})  # fmt: skip
    assert r.status_code == 503 and "busy" in r.text
