from engram.llm.frames import FrameReading
from engram.speech import Segment, Transcript, Word
from engram.timeline import Span
from engram.timeline.compact import build_flags, build_timeline


def reading(key_text: str, confidence: float = 0.9, description: str = "A board.") -> FrameReading:
    return FrameReading(
        kind="whiteboard", title="", description=description, key_text=key_text, equations=[],
        diagram="", confidence=confidence, needs_review=confidence < 0.6,
        review_reason="hidden by the lecturer" if confidence < 0.6 else "",
    )  # fmt: skip


WORDS = [
    Word(text="Um,", start=0.0, end=0.3, speaker="s0"),
    Word(text="gradient", start=0.4, end=0.9, speaker="s0"),
    Word(text="descent.", start=1.0, end=1.5, speaker="s0"),
    Word(text="Why", start=10.0, end=10.2, speaker="s1"),
    Word(text="alpha?", start=10.3, end=10.8, speaker="s1"),
    Word(text="As", start=20.0, end=20.1, speaker="s0"),
    Word(text="you", start=20.2, end=20.3, speaker="s0"),
    Word(text="can", start=20.4, end=20.5, speaker="s0"),
    Word(text="see,", start=20.6, end=20.8, speaker="s0"),
    Word(text="uh,", start=20.9, end=21.0, speaker="s0"),
    Word(text="this.", start=21.1, end=21.5, speaker="s0"),
]
TRANSCRIPT = Transcript(
    language="en", words=WORDS, source="t",
    segments=[Segment(start=0, end=1.5, text="Um, gradient descent.", logprob_mean=-0.9)],
)  # fmt: skip
SPANS = [
    Span(frame="f1", start=0, end=15, first_word=0, end_word=5, text="Um, gradient descent. Why?"),
    Span(frame="f2", start=15, end=30, first_word=5, end_word=11, text="As you can see, uh, this."),
]


def test_flags_doubtful_speech_and_substantive_doubtful_frames() -> None:
    readings = {
        "f1": reading("theta_j := theta_j - alpha d/dtheta_j J(theta) for every j", 0.4),
        "f2": reading("ing rate", 0.3),  # a cut-off fragment: not worth a look
    }
    flags = build_flags(TRANSCRIPT, SPANS, readings)
    kinds = [(f.kind, f.frame) for f in flags]
    assert ("speech", None) in kinds  # log-probability -0.9
    assert ("frame", "f1") in kinds
    assert ("frame", "f2") in kinds  # not for low confidence, but: "as you can see" + thin reading
    assert all("fragment" not in f.reason for f in flags)


def test_the_timeline_interleaves_frames_and_speech_without_repeats_or_fillers() -> None:
    readings = {
        "f1": reading("Gradient descent\ntheta_j := theta_j - alpha dJ/dtheta_j"),
        "f2": reading("Gradient descent!\ntheta j := theta_j - alpha dJ/dtheta j\nalpha = 0.01"),
    }
    text = build_timeline("CS229", 30, TRANSCRIPT, SPANS, readings, [])
    assert text.startswith("# CS229")
    assert "FRAME f1" in text and "FRAME f2" not in text  # f2 adds a single new line only
    assert "[00:00] gradient descent." in text  # "Um," is gone
    assert "[00:10] (student) Why alpha?" in text
    assert "uh," not in text


def test_new_lines_only_are_listed_for_a_changed_board() -> None:
    readings = {
        "f1": reading("Gradient descent\ntheta_j := theta_j - alpha dJ/dtheta_j"),
        "f2": reading("Gradient descent\nRepeat until convergence\nfor i = 1 to m\nupdate theta"),
    }
    text = build_timeline("CS229", 30, TRANSCRIPT, SPANS, readings, [])
    block = text.split("FRAME f2")[1]
    assert "(new lines only)" in block and "Repeat until convergence" in block
    assert "Gradient descent" not in block.split("```")[1]
