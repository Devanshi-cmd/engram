from engram.speech import Word
from engram.timeline import align_words


def words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for t, s, e in spec]


SPOKEN = words(
    ("Start", 0.5, 1.0), ("with", 1.1, 1.3), ("theta.", 1.4, 2.0),
    ("Now", 10.2, 10.5), ("update", 10.6, 11.0), ("theta_j.", 11.1, 12.0),
    ("Done.", 25.0, 25.5),
)  # fmt: skip


def test_words_go_to_the_frame_on_screen_when_they_were_said() -> None:
    spans = align_words([("f1", 0, 10), ("f2", 10, 20), ("f3", 20, 30)], SPOKEN, 30)
    assert [(s.frame, s.text) for s in spans] == [
        ("f1", "Start with theta."),
        ("f2", "Now update theta_j."),
        ("f3", "Done."),
    ]


def test_a_word_across_a_boundary_goes_by_its_midpoint() -> None:
    spans = align_words([("f1", 0, 10.7), ("f2", 10.7, 30)], SPOKEN, 30)
    assert spans[0].text.endswith("Now")  # 10.2–10.5: midpoint before 10.7
    assert spans[1].text.startswith("update")  # 10.6–11.0: midpoint 10.8


def test_consecutive_views_of_one_frame_become_one_span() -> None:
    spans = align_words([("f1", 0, 5), ("f1", 5, 12), ("f2", 12, 30)], SPOKEN, 30)
    assert [(s.frame, s.start, s.end) for s in spans] == [("f1", 0, 12), ("f2", 12, 30)]
    assert spans[0].text == "Start with theta. Now update theta_j."


def test_a_frame_revisited_later_gets_a_second_span() -> None:
    spans = align_words([("f1", 0, 10), ("f2", 10, 20), ("f1", 20, 30)], SPOKEN, 30)
    assert [s.frame for s in spans] == ["f1", "f2", "f1"]


def test_spans_tile_the_lecture_even_with_gaps_and_silence() -> None:
    spans = align_words([("f2", 14, 20), ("f1", 2, 9)], SPOKEN, 40)
    assert spans[0].start == 0 and spans[-1].end == 40
    assert spans[0].end == spans[1].start
    silent = align_words([("f1", 0, 5), ("f2", 5, 9), ("f3", 9, 40)], SPOKEN, 40)
    assert silent[1].text == "" and silent[1].first_word == silent[1].end_word


def test_no_views_means_no_spans() -> None:
    assert align_words([], SPOKEN, 30) == []
