import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from engram.course.schemas import Scene, SummaryScript
from engram.course.script import checked, speakable
from engram.errors import CreditsExhausted
from engram.pipeline import Runner, start_job
from engram.render import compose, design
from engram.render.captions import chunk, webvtt
from engram.speech import Word
from engram.speech.fake import FakeTranscriber
from engram.voice import SpokenWord, words_from_characters
from engram.voice.elevenlabs import ElevenLabsNarrator
from engram.voice.fake import FakeNarrator
from tests import synthetic
from tests.fakes import FakeReasoner


def run_job(runner: Runner, tmp_path: Path, transcriber: FakeTranscriber) -> tuple[str, Path]:
    text = "so here we write h of x equals the sum of theta j x j " * 30
    words = [Word(text=w, start=i * 0.25, end=i * 0.25 + 0.2) for i, w in enumerate(text.split())]
    transcriber.words = words
    job, _ = start_job(runner, synthetic.render(tmp_path / "board.mp4"), "Synthetic")
    runner.run(job.id)
    return job.id, runner.job_dir(job.id)


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    info: dict[str, Any] = json.loads(out.stdout)
    return info


# --- End to end -------------------------------------------------------------------------


def test_a_course_becomes_a_narrated_video_in_two_shapes(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, narrator: FakeNarrator
) -> None:
    job_id, folder = run_job(runner, tmp_path, transcriber)
    assert runner.store.job(job_id).status == "done"
    # The voice gets words, never symbols: "θ_j" from the script became "theta j".
    assert narrator.texts == ["Here is what h of x means. It's theta j x j, summed."]
    narration = json.loads((folder / "narration.json").read_text())
    for name, (w, h) in {"summary.mp4": (320, 180), "summary-portrait.mp4": (180, 226)}.items():
        info = probe(folder / name)
        video = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (video["width"], video["height"]) == (w, h)
        assert any(s["codec_type"] == "audio" for s in info["streams"])
        length = float(info["format"]["duration"])
        expected = compose.LEAD + narration["duration"] + compose.TAIL + compose.END_CARD
        assert abs(length - expected) < 0.3
    assert (folder / "summary.vtt").read_text().startswith("WEBVTT")
    assert (folder / "summary.jpg").stat().st_size > 1000


def test_a_second_run_records_nothing_again(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, narrator: FakeNarrator
) -> None:
    job_id, _ = run_job(runner, tmp_path, transcriber)
    for stage in ("script", "narrate", "video"):
        runner.store.update_stage(job_id, stage, status="pending")
    assert runner.run(job_id)
    assert len(narrator.texts) == 1  # the paid recording came from the cache


def test_a_frame_opus_made_up_becomes_a_title_card(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, reasoner: FakeReasoner
) -> None:
    reasoner.script_frame = "f9999"
    _, folder = run_job(runner, tmp_path, transcriber)
    scenes = json.loads((folder / "script.json").read_text())["scenes"]
    assert [s["frame"] for s in scenes] == ["", ""]
    assert (folder / "summary.mp4").exists()


def test_without_a_voice_the_video_waits_and_the_course_is_still_made(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber
) -> None:
    runner.services.narrator = None
    job_id, folder = run_job(runner, tmp_path, transcriber)
    rows = runner.store.stages(job_id)
    assert rows["narrate"].status == "failed" and "ELEVENLABS_VOICE_ID" in (
        rows["narrate"].error or ""
    )
    assert rows["video"].status == "blocked"
    assert rows["course"].status == "done" and (folder / "course.json").exists()


# --- Script, voice and captions -----------------------------------------------------------


def test_the_script_is_made_speakable_and_safe() -> None:
    assert speakable("θⱼ := θⱼ − α ∂J/∂θ, with $x^2$") == (
        "theta j becomes theta j minus alpha partial J over partial theta, with x 2"
    )
    script = SummaryScript(
        title="T" * 80, subtitle="s",
        scenes=[Scene(frame="f0001", heading="ok", narration="Fine."),
                Scene(frame="nope", heading="h", narration="Also fine."),
                Scene(frame="f0001", heading="h", narration="$$")],
    )  # fmt: skip
    out = checked(script, {"f0001"})
    assert [s.frame for s in out.scenes] == ["f0001", ""] and len(out.title) == 60


def test_character_timings_become_word_timings() -> None:
    text = "Hi  there."
    starts = [i * 0.1 for i in range(len(text))]
    words = words_from_characters(text, starts, [s + 0.1 for s in starts])
    assert [(w.text, w.start, w.offset) for w in words] == [("Hi", 0.0, 0), ("there.", 0.4, 4)]
    assert words[1].end == pytest.approx(1.0)


def spoken(text: str, gap_after: int = -1) -> list[SpokenWord]:
    out, t = [], 0.0
    for i, w in enumerate(text.split()):
        out.append(SpokenWord(text=w, start=t, end=t + 0.3, offset=0))
        t += 0.35 + (1.0 if i == gap_after else 0)
    return out


def test_captions_break_at_sentences_pauses_and_length() -> None:
    chunks = chunk(spoken("One two. Three four five six seven eight nine ten", gap_after=4), 20)
    # A sentence, a pause, then a long run split evenly: no orphaned last word.
    assert [c.text for c in chunks] == [
        "One two.",
        "Three four five",
        "six seven eight",
        "nine ten",
    ]
    assert chunks[0].end == chunks[1].start  # held until the next caption
    vtt = webvtt(chunk(spoken("Hello world."), 40, offset=0.5))
    assert "00:00:00.500 --> 00:00:01.750\nHello world." in vtt


def test_elevenlabs_returns_audio_and_timings_and_stops_on_credits() -> None:
    body = {
        "audio_base64": "SUQz",
        "alignment": {"characters": list("Hi you"),
                      "character_start_times_seconds": [0, 0.1, 0.2, 0.3, 0.4, 0.5],
                      "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]},
    }  # fmt: skip
    seen: list[httpx.Request] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=body)

    narrator = ElevenLabsNarrator(
        "k", "voice1", client=httpx.Client(transport=httpx.MockTransport(ok))
    )
    audio, narration = narrator.speak("Hi you")
    assert audio == b"ID3" and [w.text for w in narration.words] == ["Hi", "you"]
    assert "/text-to-speech/voice1/with-timestamps" in str(seen[0].url)
    assert json.loads(seen[0].content)["model_id"] == "eleven_multilingual_v2"

    def broke(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": {"status": "quota_exceeded"}})

    poor = ElevenLabsNarrator("k", "v", client=httpx.Client(transport=httpx.MockTransport(broke)))
    with pytest.raises(CreditsExhausted):
        poor.speak("Hi")


# --- Real-size frames --------------------------------------------------------------------


def board(tmp_path: Path) -> Path:
    import cv2

    image = np.full((720, 1280, 3), 40, np.uint8)  # a blackboard
    cv2.putText(
        image,
        "h(x) = sum theta_j x_j",
        (700, 200),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.5,
        (230, 230, 230),
        3,
    )
    path = tmp_path / "f0001.jpg"
    cv2.imwrite(str(path), image)
    return path


def test_full_size_frames_have_the_layout(tmp_path: Path) -> None:
    board(tmp_path)
    script = SummaryScript(title="Linear regression", subtitle="CS000", scenes=[
        Scene(frame="", heading="", narration="Welcome to the lecture."),
        Scene(frame="f0001", heading="The hypothesis", narration="We write h of x as a sum."),
    ])  # fmt: skip
    _, narration = FakeNarrator().speak(" ".join(s.narration for s in script.scenes))
    shots, _ = compose.plan_shots(script, narration, tmp_path)
    assert [s.kind for s in shots] == ["title", "frame", "end"]
    middle = (shots[1].start + shots[1].end) / 2

    landscape = compose.Renderer(compose.Format("l", 1920, 1080), script, shots, narration)
    frame = landscape.frame(middle)
    assert frame.shape == (1080, 1920, 3)
    assert frame[1000:1060, 700:1200].min() < 100  # the dark caption pill is there

    portrait = compose.Renderer(compose.Format("p", 1080, 1350), script, shots, narration)
    frame = portrait.frame(middle)
    x0, y0, x1, y1 = portrait.panel
    assert frame[y0 + 1, x0 + 1].min() > 200  # the panel's corner is rounded: page shows through
    assert frame[(y0 + y1) // 2, x0 + 40].max() < 100  # inside it, the blackboard
    title = portrait.frame(0.5)
    assert tuple(title[5, 5][::-1]) == design.PAGE


def test_the_move_heads_for_the_writing(tmp_path: Path) -> None:
    import cv2

    source = cv2.imread(str(board(tmp_path)))
    assert source is not None
    focus = compose._ink_centre(source)
    assert focus[0] > 900 and 150 < focus[1] < 250  # the text sits right of centre, near the top


def test_letterbox_bars_are_trimmed_and_slides_fit_whole(tmp_path: Path) -> None:
    import cv2

    frame = np.zeros((720, 1280, 3), np.uint8)
    frame[:, 160:1120] = 250  # a 4:3 slide with black bars either side
    frame[650:700, 1130:1270] = 255  # a watermark over the right bar
    trimmed = compose.trim_bars(frame)
    assert trimmed.shape[:2] == (720, 960)
    fitted = compose.ken_burns(trimmed, (480, 360), (1920, 1080), 0.0, slide=True)
    assert tuple(fitted[540, 20][::-1]) == design.PAGE  # page colour beside it, not black
    board = np.full((720, 1280, 3), 230, np.uint8)  # a filmed board in a 4:3 panel: cropped
    assert compose.ken_burns(board, (640, 360), (984, 738), 0.0)[369, 2].min() == 230
    blackboard = np.full((720, 1280, 3), 20, np.uint8)
    cv2.putText(blackboard, "x", (600, 360), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    assert compose.trim_bars(blackboard).shape == blackboard.shape  # not bars: a dark board


def test_a_long_script_gets_one_shortening_pass() -> None:
    from pydantic import BaseModel

    from engram.course.script import write_script

    def script(n: int) -> dict[str, Any]:
        scene = {"frame": "f0001", "heading": "h", "narration": "word " * n}
        return {"title": "t", "subtitle": "s", "scenes": [scene]}

    class Asker:
        labels: list[str] = []

        def ask[T: BaseModel](self, label: str, prompt: str, *, system: str, schema: type[T]) -> T:
            self.labels.append(label)
            return schema.model_validate(script(250 if len(self.labels) == 1 else 180))

    course = {"title": "T", "overview": "o", "chapters": [], "frames": {"f0001": {"t": 1.0}},
              "duration": 3600.0}  # fmt: skip
    asker = Asker()
    out = write_script(asker, course, {})
    assert asker.labels == ["summary script", "summary script, shorter"]
    assert len(out.scenes[0].narration.split()) == 180


def test_the_summary_is_shorter_for_a_short_lecture() -> None:
    from engram.course.script import word_range

    assert word_range(4 * 60) == (40, 60)
    assert word_range(10 * 60) == (80, 120)
    assert word_range(78 * 60) == (150, 190)
