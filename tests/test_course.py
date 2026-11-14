import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from engram.course import chapters as chapters_module
from engram.course import mermaid
from engram.course.chapters import Timeline, plan_chapters, split_timeline
from engram.course.grounding import Item, Sources, ground
from engram.course.schemas import ChapterPlan, GroundingReport
from engram.errors import CreditsExhausted
from engram.llm.judge import HaikuJudge
from engram.pipeline import Runner, start_job
from engram.speech import Word
from engram.speech.fake import FakeTranscriber
from engram.timeline import Anchor, Anchors, Span
from tests import synthetic
from tests.fakes import BROKEN_DIAGRAM, FakeJudge, FakeReasoner


def lecture_words() -> list[Word]:
    text = "so here we write h of x equals the sum of theta j x j " * 30
    return [Word(text=w, start=i * 0.25, end=i * 0.25 + 0.2) for i, w in enumerate(text.split())]


def run_job(runner: Runner, tmp_path: Path, transcriber: FakeTranscriber) -> tuple[str, Path]:
    transcriber.words = lecture_words()
    job, _ = start_job(runner, synthetic.render(tmp_path / "board.mp4"), "Synthetic | CS000")
    assert runner.run(job.id), runner.store.stages(job.id)
    return job.id, runner.job_dir(job.id)


# --- The whole course, end to end on fakes ------------------------------------------------


def test_a_lecture_becomes_a_course_with_every_item_timed(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber
) -> None:
    _, folder = run_job(runner, tmp_path, transcriber)
    course = json.loads((folder / "course.json").read_text())
    assert (
        course["title"] == "A synthetic lecture" and course["lecture_title"] == "Synthetic | CS000"
    )
    chapter = course["chapters"][0]
    assert chapter["start"] == 0 and chapter["end"] == course["duration"]
    for item in chapter["flashcards"] + chapter["quiz"] + chapter["key_terms"]:
        assert item["t"] is not None and item["how"].startswith("quote")
        assert chapter["start"] <= item["t"] <= chapter["end"]
    assert chapter["quiz"][0]["check"] == "said at the anchor"
    # A point with no anchor gets no invented time; it's listed instead.
    assert "c1-s1-p2" in course["unplaced"]
    for frame in course["frames"]:
        assert (folder / "thumbs" / f"{frame}.jpg").exists()
    assert course["stats"]["frames_opened_by_opus"]  # taken from Claude Code's tool calls
    md = (folder / "course.md").read_text()
    assert md.startswith("# A synthetic lecture") and "```mermaid" in md and "<details>" in md


def test_a_second_run_asks_opus_and_haiku_nothing(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, reasoner: FakeReasoner,
    judge: FakeJudge,
) -> None:  # fmt: skip
    job_id, _ = run_job(runner, tmp_path, transcriber)
    asked, judged = len(reasoner.calls), len(judge.prompts)
    for stage in ("chapters", "write", "check"):
        runner.store.update_stage(job_id, stage, status="pending")
    assert runner.run(job_id)
    assert (len(reasoner.calls), len(judge.prompts)) == (asked, judged)  # all from the cache


def test_unsupported_cards_are_removed_with_the_reason(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, reasoner: FakeReasoner
) -> None:
    reasoner.bad_card = True
    _, folder = run_job(runner, tmp_path, transcriber)
    course = json.loads((folder / "course.json").read_text())
    fronts = [c["front"] for c in course["chapters"][0]["flashcards"]]
    assert fronts == ["What is h of x?"]
    [removed] = course["removed"]
    assert removed["verdict"] == "unsupported" and removed["item"]["front"] == "UNSUPPORTED claim?"


needs_node = pytest.mark.skipif(not mermaid.checker_ready(), reason="npm ci --prefix tools/mermaid")


@needs_node
def test_a_broken_diagram_is_repaired_once(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, reasoner: FakeReasoner
) -> None:
    reasoner.diagram = BROKEN_DIAGRAM
    _, folder = run_job(runner, tmp_path, transcriber)
    [diagram] = json.loads((folder / "course.json").read_text())["chapters"][0]["diagrams"]
    assert diagram["repaired"] and "Update θ" in diagram["mermaid"]


@needs_node
def test_a_diagram_that_cant_be_repaired_is_never_shown(
    runner: Runner, tmp_path: Path, transcriber: FakeTranscriber, reasoner: FakeReasoner
) -> None:
    reasoner.diagram = reasoner.repair = BROKEN_DIAGRAM
    _, folder = run_job(runner, tmp_path, transcriber)
    chapter = json.loads((folder / "course.json").read_text())["chapters"][0]
    assert chapter["diagrams"] == [] and chapter["dropped_diagrams"][0]["title"] == "Update"
    assert "```mermaid" not in (folder / "course.md").read_text()


@needs_node
def test_mermaid_is_checked_by_its_own_parser() -> None:
    ok, broken = mermaid.check(
        ['flowchart LR\n  a["θ := θ − α∂J/∂θ"] --> b', "flowchart TD\n a -->"]
    )
    assert ok is None and "Parse error" in (broken or "")
    assert mermaid.tidy("```mermaid\nflowchart TD\n a --> b\n```") == "flowchart TD\n a --> b"


# --- Chapters ------------------------------------------------------------------------------


def timeline(minutes: int) -> Timeline:
    lines = ["# Lecture", "Duration", ""]
    for m in range(minutes):
        lines.append(f"[{m:02d}:00] talk about topic {m // 10} at minute {m}")
        if m % 7 == 0:
            lines += ["", f"[{m:02d}:05] FRAME f{m:04d} (whiteboard, conf 0.90): A board.", "```"]
            lines += [f"line {m}", "```", ""]
    return split_timeline("\n".join(lines))


class StubAsker:
    def __init__(self, *plans: dict[str, Any]) -> None:
        self.plans = list(plans)
        self.labels: list[str] = []
        self.prompts: list[str] = []

    def ask[T: BaseModel](self, label: str, prompt: str, *, system: str, schema: type[T]) -> T:
        self.labels.append(label)
        self.prompts.append(prompt)
        return schema.model_validate(self.plans.pop(0))


def plan(*starts: str) -> dict[str, Any]:
    chapters = [{"starts_at": s, "title": f"Topic at {s}", "summary": "s"} for s in starts]
    return {"title": "Clean title", "overview": "o", "chapters": chapters}


def test_the_timeline_splits_into_stamped_entries() -> None:
    t = timeline(15)
    assert t.header.startswith("# Lecture")
    frame = next(e for e in t.entries if "FRAME f0007" in e.text)
    assert frame.t == 425 and "line 7" in frame.text  # the board lines stay with their frame


def test_chapter_starts_are_snapped_sorted_and_sane() -> None:
    # Out of order, off-stamp (14:07 snaps to the frame at 14:05), too close to the previous
    # one (14:50 → 15:00), past the end, and not a stamp at all.
    asker = StubAsker(plan("[20:00]", "00:00", "14:07", "14:50", "99:00", "soon"))
    title, _, chapters = plan_chapters(asker, timeline(30), 30 * 60)
    assert title == "Clean title"
    assert [(c.start, c.end) for c in chapters] == [(0, 845), (845, 1200), (1200, 1800)]
    assert [c.number for c in chapters] == [1, 2, 3]


def test_long_lectures_are_chaptered_window_by_window_then_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chapters_module, "SINGLE_PASS_CHARS", 500)
    asker = StubAsker(plan("00:00", "10:00"), plan("20:00"), plan("00:00", "20:00"))
    _, _, chapters = plan_chapters(asker, timeline(30), 30 * 60)
    assert asker.labels == ["chapters part 1", "chapters part 2", "chapters merge"]
    assert "[10:00] Topic at 10:00" in asker.prompts[-1]  # the merge sees every proposal
    assert "[25:00]" not in asker.prompts[0]  # each window sees only its own part
    assert [c.start for c in chapters] == [0, 1200]


# --- Anchors -------------------------------------------------------------------------------


def anchors() -> Anchors:
    text = ("we start with the cost function " + "filler words here " * 20) * 2
    text += "and then gradient descent keeps changing theta to reduce the cost"
    words = [Word(text=w, start=i * 0.5, end=i * 0.5 + 0.4) for i, w in enumerate(text.split())]
    spans = [
        Span(frame="f0001", start=0, end=40, first_word=0, end_word=80, text=""),
        Span(frame="f0002", start=40, end=200, first_word=80, end_word=len(words), text=""),
    ]
    return Anchors(words, spans)


def test_a_quote_resolves_to_the_word_where_it_starts() -> None:
    m = anchors().locate(Anchor(frame="", quote="keeps changing theta to reduce the cost"))
    assert m is not None and m.t == 68.0 and m.frame == "f0002" and m.how == "quote 1.00"


def test_a_repeated_quote_prefers_the_items_own_chapter() -> None:
    a = anchors()
    first = a.locate(Anchor(frame="", quote="we start with the cost function"))
    second = a.locate(Anchor(frame="", quote="we start with the cost function"), (30, 100))
    assert first is not None and first.t == 0
    assert second is not None and second.t == 33.0


def test_a_weak_quote_falls_back_to_the_frame_and_nothing_is_invented() -> None:
    a = anchors()
    m = a.locate(Anchor(frame="f0002", quote="completely different words about kites"))
    assert m is not None and m.t == 40 and m.how.startswith("frame (quote")
    assert a.locate(Anchor(frame="f9999", quote="nothing like this was said")) is None


# --- Grounding -----------------------------------------------------------------------------


def test_the_judge_sees_speech_and_board_and_silence_means_unsupported() -> None:
    sources = Sources(anchors(), {"f0002": "θ := θ − α ∂J/∂θ"})
    items = [
        Item("c1-q1", "Quiz question: what changes?", 67.0, "f0002", "changing theta"),
        Item("c1-card1", "Flashcard. Front: cost?", 0.0, "f0001", ""),
    ]

    class Forgetful:
        name = "forgetful"
        prompt = ""

        def run[T: BaseModel](
            self, prompt: str, *, system: str, schema: type[T]
        ) -> tuple[T, float]:
            Forgetful.prompt = prompt
            v = {"id": "c1-q1", "verdict": "supported", "reason": "said"}
            return schema.model_validate({"verdicts": [v]}), 0.001

    verdicts, cost = ground(Forgetful(), sources, items, (0, 200))
    assert "keeps changing theta" in Forgetful.prompt and "θ := θ − α" in Forgetful.prompt
    assert verdicts["c1-q1"].verdict == "supported"
    assert verdicts["c1-card1"].verdict == "unsupported"  # no verdict returned for it
    assert cost == 0.001


def test_haiku_judges_through_the_api_and_stops_when_credits_run_out() -> None:
    report = GroundingReport(verdicts=[])
    calls: list[dict[str, Any]] = []

    def parse(**kwargs: Any) -> Any:
        calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=3000, output_tokens=300)
        return SimpleNamespace(parsed_output=report, stop_reason="end_turn", usage=usage)

    judge = HaikuJudge("key", client=SimpleNamespace(messages=SimpleNamespace(parse=parse)))
    value, cost = judge.run("check", system="s", schema=GroundingReport)
    assert value == report and cost == pytest.approx((3000 * 0.10 + 300 * 0.50) / 1e6)
    assert calls[0]["model"] == "claude-haiku-5-5" and calls[0]["output_format"] is GroundingReport

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.BadRequestError(
        "Your credit balance is too low", response=httpx2.Response(400, request=request), body=None
    )

    def broke(**kwargs: Any) -> Any:
        raise error

    poor = HaikuJudge("key", client=SimpleNamespace(messages=SimpleNamespace(parse=broke)))
    with pytest.raises(CreditsExhausted):
        poor.run("check", system="s", schema=ChapterPlan)
