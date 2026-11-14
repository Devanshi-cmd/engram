"""Finds the frames worth reading in a lecture, including filmed whiteboards.

Recall's method (keep a frame when its hash jumps) works for screen recordings. A filmed
lecture breaks it: the camera follows the lecturer, so every frame differs from the last,
and the lecturer usually stands in front of the board. This module works on *ink* instead
of pixels, and on *views* (what the camera points at) instead of single frames.

One pass over the video at 1 frame per second and 480 px wide:

1. **Ink map.** A black-hat filter keeps thin dark strokes (white-hat for chalk on a dark
   board). A tall shape that stands on the floor is a person: its tall columns become the
   person region, and nothing inside it counts as writing. Board edges and the tray are
   dropped too.
2. **Camera motion.** The board is moving when its pixels (the person excluded) change.
   Phase correlation on the *ink maps*, not the frames, then gives the pan in pixels: the
   camera follows the lecturer, so whole-frame tracking follows them too. A view ends once
   the camera has moved a quarter of the frame from where the view began.
3. **Cuts.** A switch to a slide or another camera changes the brightness histogram and
   most pixels at once. Both must change: a plain slide can move the histogram alone.
4. **Changes.** If most of the visible writing vanishes, at once or for several seconds,
   the board was wiped, or the lecturer or a zoom hid it. Either way the view ends there,
   so the board's finished state before the change is kept.
5. **Best frame per view.** The still, sharp frame with the most visible ink, less a
   penalty for how much of the frame the person covers. Later frames win near-ties. That's
   the board at its most complete, with the least of it hidden.

Afterwards, marks that never move on screen (a channel logo) are learned from the whole
video and removed. Then frames are consolidated (Recall's idea, measured on ink after
alignment instead of on OCR words, so it works on handwriting). Backwards: a frame is
dropped when the frames after it, within three minutes, show its writing between them.
Forwards: a board the camera returns to, showing nothing new, is a revisit.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from engram.errors import MediaError

Gray = NDArray[np.uint8]
Mask = NDArray[np.bool_]

ANALYSIS_WIDTH = 480
SAMPLE_FPS = 1
INK_THRESHOLD = 18  # black-hat response that counts as a stroke (faint blue marker passes)
PAN_FRACTION = 0.25  # the camera moving this much of the frame starts a new view
STILL_BOARD_CHANGE = 8.0  # mean grey change of the board (person excluded) that means motion
CUT_HIST_CORRELATION = 0.5  # a cut needs the histogram to change this much...
CUT_MEAN_DIFF = 45.0  # ...and the average pixel to change this much (pans peak near 33)
CHANGE_RATIO = 0.6  # visible ink below this share of the view's best...
CHANGE_SECONDS = 4  # ...for this many seconds means the writing was wiped or hidden
SUDDEN_CHANGE_RATIO = 0.3  # below this share in a single step: a change at once
MIN_VIEW_SECONDS = 3  # shorter views are camera moves; they join the next view
OVERLAY_SHARE = 0.7  # ink in this share of all frames is a logo, not writing
COVER_WINDOW_SECONDS = 180  # a frame is redundant if frames this close show its ink...
COVER_SHARE = 0.8  # ...covering at least this share of it
NEAR_TIE = 0.95  # a later frame scoring within 5% of the best replaces it
TRACK_MIN_INK = 150  # with less writing than this in view, the camera can't be tracked
TRACK_MIN_CONFIDENCE = 0.15  # phase-correlation peak needed to believe a camera move
TEXT_MIN_SHARE = 0.004  # frames with less ink than this are talking heads: not worth reading

_HAT = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
_GROW = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))


# ---- per-frame signals ------------------------------------------------------
def ink_and_person(gray: Gray) -> tuple[Mask, Mask]:
    """Pixels that look like writing, and the region a person covers.

    A person is a tall connected shape that stands on the floor: it reaches the bottom of
    the frame, or fills most of its height. Tall shapes floating on the board (a plot, a big
    matrix bracket) are writing. The person's bounding box, widened a little, becomes the
    person region, and ink inside it (hands, the notes they hold, shirt folds) is dropped.
    """
    height, width = gray.shape
    dark_board = float(np.median(gray)) < 100  # chalk on a blackboard: strokes are light
    op = cv2.MORPH_TOPHAT if dark_board else cv2.MORPH_BLACKHAT
    ink = cv2.morphologyEx(gray, op, _HAT) > INK_THRESHOLD
    grown = cv2.dilate(ink.astype(np.uint8), _GROW)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grown, connectivity=8)
    x, y = stats[:, cv2.CC_STAT_LEFT], stats[:, cv2.CC_STAT_TOP]
    w, h, area = stats[:, cv2.CC_STAT_WIDTH], stats[:, cv2.CC_STAT_HEIGHT], stats[:, 2]
    band = (w >= 0.6 * width) & (h < 0.6 * height)  # the board's lower edge and the tray
    standing = (y + h >= 0.97 * height) | (h >= 0.75 * height)
    people = (h >= 0.25 * height) & standing & ~band
    keep = (
        ~people
        & ~band
        & (area > 12)  # specks
        & ~((h <= 4) & (w > 40))  # board edges
        & ~((w <= 4) & (h > 40))
    )
    keep[0] = people[0] = False
    person = np.zeros(gray.shape, dtype=bool)
    for i in np.flatnonzero(people):
        person |= _silhouette(labels[y[i] : y[i] + h[i], x[i] : x[i] + w[i]] == i, x[i], y[i],
                              gray.shape)  # fmt: skip
    if person.any():
        pad = int(0.04 * width)
        person = cv2.dilate(person.astype(np.uint8), np.ones((1, 2 * pad + 1), np.uint8)) > 0
    mask: Mask = ink & keep[labels] & ~person
    return mask, person


def _silhouette(shape: Mask, left: int, top: int, size: tuple[int, ...]) -> Mask:
    """The body inside a person-shaped component: its tall columns, from the top down.

    The component can include writing the person's outline happens to touch. Writing is
    short in every column it crosses, while a body runs from head (or shoulders) to the
    floor, so only columns spanning more than a third of the frame count.
    """
    height = size[0]
    region = np.zeros(size[:2], dtype=bool)
    rows = np.arange(shape.shape[0])[:, None]
    present = shape.any(axis=0)
    first = np.where(present, np.argmax(shape, axis=0), 0)
    last = np.where(present, shape.shape[0] - 1 - np.argmax(shape[::-1], axis=0), 0)
    tall = present & (last - first >= 0.35 * height)
    for col in np.flatnonzero(tall):
        region[top + first[col] :, left + col] = True
    del rows
    return region


def ink_mask(gray: Gray) -> Mask:
    """Pixels that look like writing on the board (see `ink_and_person`)."""
    return ink_and_person(gray)[0]


def sharpness(gray: Gray) -> float:
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _histogram(gray: Gray) -> NDArray[np.float32]:
    hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
    cv2.normalize(hist, hist)
    return np.asarray(hist, dtype=np.float32)


# ---- one pass over the video ----------------------------------------------
@dataclass
class _Sample:
    t: float
    ink: int
    sharp: float
    still: bool
    person_share: float = 0.0
    gray: Gray | None = None  # kept only while the sample is a view's best candidate
    mask: Mask | None = None
    person: Mask | None = None


@dataclass
class View:
    start: float
    end: float
    reason: str  # why it began: start, pan, cut, change (writing wiped or hidden)
    best: _Sample | None = None
    peak_ink: int = 0
    low_run: int = 0


@dataclass
class Keyframe:
    id: str
    t: float
    ink_share: float
    has_text: bool
    views: list[tuple[float, float]] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


@dataclass
class KeyframeResult:
    keyframes: list[Keyframe]
    samples: int
    views: int
    dropped_as_covered: int


def sample_frames(video: Path, duration: float) -> Iterator[tuple[float, Gray]]:
    """Grey 480 px frames at 1 fps, decoded by one ffmpeg process into a pipe."""
    height = int(round(ANALYSIS_WIDTH * 9 / 16 / 2)) * 2
    size = ANALYSIS_WIDTH * height
    cmd = [
        "ffmpeg", "-nostdin", "-v", "error", "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={ANALYSIS_WIDTH}:{height},format=gray",
        "-f", "rawvideo", "-",
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdout is not None
    index = 0
    try:
        while (buf := proc.stdout.read(size)) and len(buf) == size:
            yield index / SAMPLE_FPS, np.frombuffer(buf, np.uint8).reshape(height, ANALYSIS_WIDTH)
            index += 1
    finally:
        proc.stdout.close()
        err = proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        if proc.wait() != 0 and index == 0:
            raise MediaError(f"ffmpeg failed: {err.splitlines()[-1] if err else 'no frames'}")


def segment_views(
    frames: Iterator[tuple[float, Gray]],
    progress: Callable[[float], None] | None = None,
) -> tuple[list[View], Mask | None, int]:
    """Splits the lecture into views and remembers each view's best frame."""
    views: list[View] = []
    window = None
    prev: Gray | None = None
    prev_track: NDArray[np.float32] | None = None
    prev_ink = 0
    prev_person: Mask | None = None
    prev_hist: NDArray[np.float32] | None = None
    pan_x = pan_y = 0.0
    overlay_sum: NDArray[np.uint32] | None = None
    count = 0
    recent_ink: list[int] = []

    for t, gray in frames:
        count += 1
        if progress and count % 60 == 0:
            progress(t)
        mask, person = ink_and_person(gray)
        overlay_sum = mask.astype(np.uint32) if overlay_sum is None else overlay_sum + mask
        ink = int(mask.sum())
        hist = _histogram(gray)
        track = _for_tracking(mask)
        reason = None
        moving = False
        dx = dy = 0.0
        if prev is None:
            window = cv2.createHanningWindow(gray.shape[::-1], cv2.CV_32F)
            reason = "start"
        else:
            assert prev_hist is not None and window is not None and prev_track is not None
            assert prev_person is not None
            corr = cv2.compareHist(prev_hist, hist, cv2.HISTCMP_CORREL)
            change = cv2.absdiff(prev, gray)
            diff = float(np.mean(change))
            board = ~(person | prev_person)
            board_change = float(change[board].mean()) if board.any() else diff
            moving = board_change >= STILL_BOARD_CHANGE
            if corr < CUT_HIST_CORRELATION and diff > CUT_MEAN_DIFF:
                reason = "cut"
            else:
                # Only a moving board can mean a pan: new writing changes the board a little
                # and would otherwise read as drift.
                if moving and ink >= TRACK_MIN_INK and prev_ink >= TRACK_MIN_INK:
                    (sx, sy), confidence = cv2.phaseCorrelate(prev_track, track, window)
                    # A real pan moves all the writing together and matches strongly. New
                    # strokes appearing match weakly, and aren't camera motion.
                    if confidence >= TRACK_MIN_CONFIDENCE:
                        dx, dy = sx, sy
                pan_x += dx
                pan_y += dy
                h, w = gray.shape
                if abs(pan_x) > PAN_FRACTION * w or abs(pan_y) > PAN_FRACTION * h:
                    reason = "pan"
        still = reason is None and (prev is None or not moving)

        current = views[-1] if views else None
        recent_ink = (recent_ink + [ink])[-3:] if reason is None else [ink]
        smooth = int(np.median(recent_ink))
        # An erase is counted even while the camera seems to move: after a wipe there's little
        # left to track, and a collapse in ink means new content either way.
        if reason is None and current is not None and current.peak_ink > 0:
            if prev_ink >= TRACK_MIN_INK and ink < SUDDEN_CHANGE_RATIO * current.peak_ink:
                reason = "change"  # most of the board vanished at once
            elif smooth < CHANGE_RATIO * current.peak_ink:
                current.low_run += 1
                if current.low_run >= CHANGE_SECONDS:
                    reason = "change"
            else:
                current.low_run = 0

        if reason is not None:
            if current is not None:
                current.end = t
            views.append(View(start=t, end=t, reason=reason))
            pan_x = pan_y = 0.0
            current = views[-1]
            still = reason == "start"

        assert current is not None
        current.end = t + 1 / SAMPLE_FPS
        if still:
            current.peak_ink = max(current.peak_ink, smooth)
            sample = _Sample(t, ink, sharpness(gray), True, float(person.mean()))
            # Later frames win near-ties: they show the finished version of the board.
            if current.best is None or _score(sample) >= NEAR_TIE * _score(current.best):
                sample.gray, sample.mask, sample.person = gray.copy(), mask, person
                current.best = sample
        prev, prev_hist, prev_track, prev_ink, prev_person = gray, hist, track, ink, person

    overlay = None
    if overlay_sum is not None and count >= 30:
        overlay = overlay_sum >= OVERLAY_SHARE * count
    return _merge_short(views), overlay, count


def _for_tracking(ink: Mask) -> NDArray[np.float32]:
    """What the camera tracker sees: the writing only, softened.

    Tracking whole frames follows the lecturer (the camera usually does too), and painting
    them out leaves edges that move with them. Writing is fixed to the board, so its shift
    is the camera's.
    """
    return np.asarray(cv2.GaussianBlur(ink.astype(np.float32), (0, 0), 2.0), dtype=np.float32)


def _score(s: _Sample) -> float:
    # Most visible ink wins. The person's share of the frame costs up to half the score, so
    # of two frames showing the same writing, the one where they block less wins. Sharpness
    # pushes out motion blur.
    return s.ink * (1.0 - 0.5 * s.person_share) * min(1.0, s.sharp / 300.0) + s.t * 1e-6


def _merge_short(views: list[View]) -> list[View]:
    """Camera moves produce views of a second or two. They join the view that follows."""
    merged: list[View] = []
    carry_start: float | None = None
    for view in views:
        if view.end - view.start < MIN_VIEW_SECONDS or view.best is None:
            carry_start = view.start if carry_start is None else carry_start
            continue
        if carry_start is not None:
            view.start = carry_start
            carry_start = None
        merged.append(view)
    if carry_start is not None and merged:
        merged[-1].end = views[-1].end
    for a, b in zip(merged, merged[1:], strict=False):  # views tile the timeline
        a.end = b.start
    if merged:
        merged[0].start = 0.0
    return merged


# ---- consolidation ----------------------------------------------------------
_ORB = cv2.ORB.create(nfeatures=1500)
_MATCHER = cv2.BFMatcher(cv2.NORM_HAMMING)


def _feature_mask(ink: Mask) -> NDArray[np.uint8]:
    return np.asarray(cv2.dilate(ink.astype(np.uint8) * 255, np.ones((15, 15), np.uint8)))


def _align(a: _Sample, b: _Sample, a_ink: Mask, b_ink: Mask) -> NDArray[np.float64] | None:
    """A similarity transform taking `b`'s pixels onto `a`'s, matched on writing only."""
    assert a.gray is not None and b.gray is not None
    # Features only from the writing: the lecturer moves and would line up the wrong things.
    ka, da = _ORB.detectAndCompute(a.gray, _feature_mask(a_ink))
    kb, db = _ORB.detectAndCompute(b.gray, _feature_mask(b_ink))
    if da is None or db is None or len(ka) < 12 or len(kb) < 12:
        return _shift_only(a_ink, b_ink)
    pairs = [m for m, n in (p for p in _MATCHER.knnMatch(db, da, k=2) if len(p) == 2)
             if m.distance < 0.75 * n.distance]  # fmt: skip
    if len(pairs) < 12:
        return _shift_only(a_ink, b_ink)
    src = np.array([kb[m.queryIdx].pt for m in pairs], dtype=np.float32)
    dst = np.array([ka[m.trainIdx].pt for m in pairs], dtype=np.float32)
    transform, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC)
    if transform is None or inliers is None or int(inliers.sum()) < 10:
        return None
    scale = float(np.hypot(transform[0, 0], transform[0, 1]))
    if not 0.7 < scale < 1.4:  # a camera zooms a little, never this much between views
        return None
    return np.asarray(transform, dtype=np.float64)


def _shift_only(a_ink: Mask, b_ink: Mask) -> NDArray[np.float64] | None:
    """For boards with too little writing for feature matching: a pure camera shift."""
    if a_ink.sum() < 30 or b_ink.sum() < 30:
        return None
    a, b = _for_tracking(a_ink), _for_tracking(b_ink)
    window = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    (dx, dy), confidence = cv2.phaseCorrelate(b, a, window)
    if confidence < 0.3:
        return None
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy]], dtype=np.float64)


def covered_share(earlier: _Sample, later: list[_Sample], overlay: Mask | None) -> float:
    """How much of `earlier`'s writing the `later` frames show between them.

    Each later frame is lined up with the earlier one, and its (slightly grown) ink is
    painted onto the earlier frame's canvas. The share of the earlier frame's ink that
    gets painted over is what the later frames already show.
    """
    assert earlier.mask is not None
    a_ink = earlier.mask & ~overlay if overlay is not None else earlier.mask
    total = int(a_ink.sum())
    if total == 0:
        return 1.0
    h, w = a_ink.shape
    shown = np.zeros((h, w), dtype=np.uint8)
    for other in later:
        assert other.mask is not None
        b_ink = other.mask & ~overlay if overlay is not None else other.mask
        transform = _align(earlier, other, a_ink, b_ink)
        if transform is None:
            continue
        grown = cv2.dilate(b_ink.astype(np.uint8), _GROW)
        shown |= np.asarray(
            cv2.warpAffine(grown, transform, (w, h), flags=cv2.INTER_NEAREST), dtype=np.uint8
        )
    return int((shown.astype(bool) & a_ink).sum()) / total


def find_keyframes(
    video: Path,
    duration: float,
    progress: Callable[[float], None] | None = None,
) -> tuple[KeyframeResult, dict[str, float]]:
    """Views, consolidated keyframes, and each keyframe's time (for full-size extraction)."""
    views, overlay, count = segment_views(sample_frames(video, duration), progress)
    pixels = None
    entries: list[tuple[View, _Sample]] = [(v, v.best) for v in views if v.best is not None]

    # Walk backwards: the finished version of each board wins. A frame is dropped when the
    # frames kept after it, within a few minutes, already show its writing between them.
    kept: list[tuple[list[View], _Sample]] = []
    dropped = 0
    for view, best in reversed(entries):
        nearby = [g for g in kept if g[1].t - best.t <= COVER_WINDOW_SECONDS]
        if nearby and covered_share(best, [g[1] for g in nearby], overlay) >= COVER_SHARE:
            # Its time goes to the nearest later frame that shows the same board.
            nearby[-1][0].append(view)
            dropped += 1
        else:
            kept.append(([view], best))
    kept.reverse()

    # Then forwards: a board the camera comes back to, showing nothing new, is a revisit.
    # Its time goes to the earlier frame that already shows that board.
    revisited: list[tuple[list[View], _Sample]] = []
    for group in kept:
        nearby = [g for g in revisited if group[1].t - g[1].t <= COVER_WINDOW_SECONDS]
        if nearby and covered_share(group[1], [g[1] for g in nearby], overlay) >= COVER_SHARE:
            nearby[-1][0].extend(group[0])
            dropped += 1
        else:
            revisited.append(group)
    kept = revisited

    keyframes: list[Keyframe] = []
    times: dict[str, float] = {}
    for index, (owned, best) in enumerate(kept, start=1):
        assert best.mask is not None
        pixels = pixels or best.mask.size
        ink = best.mask & ~overlay if overlay is not None else best.mask
        share = float(ink.sum()) / pixels
        owned.sort(key=lambda v: v.start)
        frame = Keyframe(
            id=f"f{index:04d}",
            t=best.t,
            ink_share=round(share, 5),
            has_text=share >= TEXT_MIN_SHARE,
            views=[(round(v.start, 2), round(v.end, 2)) for v in owned],
            reasons=[v.reason for v in owned],
        )
        keyframes.append(frame)
        times[frame.id] = best.t
    return KeyframeResult(keyframes, count, len(views), dropped), times


def extract_frame(video: Path, t: float, out: Path) -> None:
    """One full-resolution frame as a high-quality JPEG."""
    tmp = out.with_name(f".{out.name}")
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-q:v", "2", str(tmp)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    tmp.replace(out)
