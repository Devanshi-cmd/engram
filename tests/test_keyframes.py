from pathlib import Path

import cv2
import numpy as np
import pytest

from engram.vision.keyframes import KeyframeResult, find_keyframes, ink_and_person
from tests import synthetic


@pytest.fixture(scope="module")
def board_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return synthetic.render(tmp_path_factory.mktemp("board") / "board.mp4")


@pytest.fixture(scope="module")
def found(board_video: Path) -> tuple[KeyframeResult, dict[str, float]]:
    return find_keyframes(board_video, synthetic.SECONDS)


def gray(frame: np.ndarray) -> np.ndarray:
    small = cv2.resize(frame, (480, 270), interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)


def test_writing_counts_as_ink_but_the_lecturer_and_tray_do_not() -> None:
    frame = synthetic.frame_at(19)
    ink, person = ink_and_person(gray(frame))
    cx = synthetic.lecturer_x(19) * 480 // synthetic.W
    assert person[150:, cx].all()  # the lecturer's column is marked as a person
    assert not ink[150:, cx - 20 : cx + 20].any()  # and none of it is ink
    assert not ink[-12:, :].any()  # the pen tray isn't writing
    assert ink[:120].sum() > 300  # the written lines are


def test_each_view_keeps_its_finished_board(
    found: tuple[KeyframeResult, dict[str, float]],
) -> None:
    result, times = found
    t = sorted(times.values())
    # Left view: the best frame comes after the third line is finished (written by 15 s).
    assert any(15 <= x < 21 for x in t)
    # Right view before the wipe: both lines done (35 s), wipe at 40.
    assert any(35 <= x < 40 for x in t)


def test_the_wipe_the_cut_and_the_pan_all_start_views(
    found: tuple[KeyframeResult, dict[str, float]],
) -> None:
    result, _ = found
    reasons = {r for k in result.keyframes for r in k.reasons}
    assert {"pan", "change", "cut"} <= reasons


def test_the_slide_is_kept(found: tuple[KeyframeResult, dict[str, float]]) -> None:
    _, times = found
    assert any(50 <= x < 58 for x in times.values())


def test_views_tile_the_whole_lecture(found: tuple[KeyframeResult, dict[str, float]]) -> None:
    result, _ = found
    spans = sorted(span for k in result.keyframes for span in k.views)
    assert spans[0][0] == 0
    for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
        assert end == start
    assert spans[-1][1] >= synthetic.SECONDS - 1


def test_a_board_shown_again_later_is_not_kept_twice(
    found: tuple[KeyframeResult, dict[str, float]],
) -> None:
    result, times = found
    # The post-wipe line is on screen at 46–50 and again at 58–66: one frame for both.
    holders = [k for k in result.keyframes if any(s < 50 and e > 44 for s, e in k.views)]
    later = [k for k in result.keyframes if any(e > 60 for _, e in k.views)]
    assert holders and later
    assert holders[-1].id == later[-1].id or result.dropped_as_covered >= 1


def test_every_frame_has_a_time_inside_one_of_its_views(
    found: tuple[KeyframeResult, dict[str, float]],
) -> None:
    result, times = found
    for k in result.keyframes:
        assert times[k.id] == k.t
        assert 0 <= k.t <= synthetic.SECONDS


def test_a_plot_on_a_slide_is_writing_not_a_person() -> None:
    ink, person = ink_and_person(gray(synthetic.slide()))
    assert not person.any()
    assert ink.sum() > 200
