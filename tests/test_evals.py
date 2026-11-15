from typing import Any

from pydantic import BaseModel

from engram.evals import board_states, edit_distance, grounding, normalise, report
from engram.speech import Word
from engram.timeline import Anchors, Span
from engram.vision.keyframes import Keyframe


def test_people_and_latex_spell_the_same_board_alike() -> None:
    typed = "θj := θj − α ∂/∂θj J(θ)\nx(i)²"
    read = (
        "$\\theta_j := \\theta_j - \\alpha \\frac{\\partial}{\\partial \\theta_j} J(\\theta)$"
        "\n$x^{(i)2}$"
    )
    assert normalise(typed).replace("/", "") == normalise(read)
    assert normalise("Say θ = 0⃗ [?]") == normalise("say $\\theta = \\vec{0}$")


def test_edit_distance() -> None:
    assert edit_distance("kitten", "sitting") == 3
    assert edit_distance("", "abc") == 3 and edit_distance("same", "same") == 0


def frame(i: str, t: float, *views: tuple[float, float]) -> Keyframe:
    return Keyframe(id=i, t=t, ink_share=0.1, has_text=True, views=list(views))


def test_a_state_is_found_directly_or_through_a_fuller_later_frame() -> None:
    keyframes = [
        frame("f1", 28, (0, 30)),  # state at 30: found directly
        frame("f2", 95, (40, 60), (80, 100)),  # covers the state at 55 (a revisit), and 95
        frame("f3", 150, (140, 160)),  # matches nothing labelled: extra
    ]
    score = board_states("x", [30, 55, 95, 130], keyframes, until=200)
    assert (score.states, score.direct, score.via_later) == (4, 2, 1)
    assert score.missed == [130] and score.extra == 1 and score.recall == 0.75
    # a board still up when the window ends is kept by a frame after it
    tail = board_states("x", [195], [*keyframes, frame("f4", 260, (190, 262))], until=200)
    assert tail.via_later == 1 and tail.missed == [] and tail.keyframes == 3


class Lenient:
    name = "lenient"

    def run[T: BaseModel](self, prompt: str, *, system: str, schema: type[T]) -> tuple[T, float]:
        ids = [b.split("\n", 1)[0].strip() for b in prompt.split("### Item ")[1:]]
        verdicts = [{"id": i, "verdict": "supported", "reason": "ok"} for i in ids]
        return schema.model_validate({"verdicts": verdicts}), 0.001


def test_the_grounding_eval_plants_one_fault_per_item() -> None:
    words = [Word(text=w, start=i, end=i + 0.5) for i, w in enumerate("abcdef")]
    anchors = Anchors(words, [Span(frame="f1", start=0, end=6, first_word=0, end_word=6, text="")])
    cards = [{"id": f"c1-card{i}", "front": f"Q{i}", "back": f"A{i}", "t": 1.0, "frame": "f1"}
             for i in (1, 2)]  # fmt: skip
    quiz = [{"id": "c1-q1", "question": "Q", "options": ["a", "b", "c", "d"], "answer": 0,
             "explanation": "e", "evidence": "a b", "t": 1.0, "frame": "f1"}]  # fmt: skip
    course: dict[str, Any] = {"chapters": [
        {"start": 0, "end": 6, "flashcards": cards, "quiz": quiz}]}  # fmt: skip
    scores, cost = grounding(course, anchors, {"f1": "board"}, Lenient())
    counts = {s.kind: (s.items, s.kept) for s in scores}
    assert counts == {"original": (3, 3), "wrong answer": (1, 1), "swapped answer": (2, 2)}
    assert scores[1].kept_ids == ["c1-q1-wrong"]  # a judge this lenient lets faults through
    assert "| wrong answer | 1 | 1 (100%) | removed |" in report([], [], scores)
    assert cost > 0
