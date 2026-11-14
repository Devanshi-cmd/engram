"""A filmed-whiteboard lecture drawn in code, so keyframe detection can be tested exactly.

Timeline (seconds), camera 640×360 over a 1100×360 board:
   0–20  camera on the left; three lines are written one by one; the lecturer walks about
  20–23  the camera pans right
  23–40  two lines are written on the right
  40     the right side is wiped
  41–50  one new line is written there
  50–58  cut to a slide (a plot)
  58–66  back on the right side of the board
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

W, H, BOARD_W, FPS, SECONDS = 640, 360, 1100, 5, 66
BOARD = (226, 226, 222)
INK = (150, 60, 30)  # BGR: dark blue marker

LEFT_LINES = [(1, "h(x) = sum theta_j x_j"), (6, "theta := theta - a dJ"), (11, "m = # examples")]
RIGHT_LINES = [(24, "J(theta) = 1/2 sum"), (31, "repeat until conv.")]
AFTER_WIPE = [(42, "x_0 = 1")]


def camera_x(t: float) -> int:
    if t < 20:
        return 0
    if t < 23:
        return int((t - 20) / 3 * (BOARD_W - W))
    return BOARD_W - W


def board_at(t: float) -> NDArray[np.uint8]:
    board = np.full((H, BOARD_W, 3), BOARD, np.uint8)
    cv2.rectangle(board, (0, H - 24), (BOARD_W, H), (120, 120, 120), -1)  # the pen tray
    for i, (start, text) in enumerate(LEFT_LINES):
        _write(board, text, 30, 60 + 70 * i, t - start)
    wiped = t >= 40
    lines = AFTER_WIPE if wiped else RIGHT_LINES
    for i, (start, text) in enumerate(lines):
        _write(board, text, 640, 60 + 70 * i, t - start)
    return board


def _write(board: NDArray[np.uint8], text: str, x: int, y: int, elapsed: float) -> None:
    if elapsed <= 0:
        return
    shown = text[: int(len(text) * min(1.0, elapsed / 4))]  # four seconds to write a line
    cv2.putText(board, shown, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 1.0, INK, 2, cv2.LINE_AA)


def lecturer_x(t: float) -> int:
    return int(W / 2 + (W / 3) * np.sin(t / 3))


def lecturer(frame: NDArray[np.uint8], t: float) -> None:
    """A person-like shape wandering left and right: a shirt with an outline and folds,
    and a head with hair, standing on the floor (it runs off the bottom of the frame)."""
    cx = lecturer_x(t)
    cv2.rectangle(frame, (cx - 55, 120), (cx + 55, H), (200, 170, 140), -1)
    cv2.rectangle(frame, (cx - 55, 120), (cx + 55, H), (90, 80, 70), 3)
    for fold in (-30, -8, 15, 36):
        cv2.line(frame, (cx + fold, 140), (cx + fold + 6, H), (120, 110, 100), 2)
    cv2.circle(frame, (cx, 95), 30, (150, 175, 205), -1)
    cv2.ellipse(frame, (cx, 82), (32, 20), 0, 180, 360, (40, 40, 50), 6)


def slide() -> NDArray[np.uint8]:
    frame = np.full((H, W, 3), 250, np.uint8)
    frame[:, :60] = 0
    frame[:, -60:] = 0
    cv2.line(frame, (120, 300), (520, 300), (0, 0, 0), 2)
    cv2.line(frame, (120, 300), (120, 60), (0, 0, 0), 2)
    for k in range(12):
        cv2.circle(frame, (140 + 30 * k, 280 - 16 * k), 4, (0, 0, 0), -1)
    return frame


def frame_at(t: float) -> NDArray[np.uint8]:
    if 50 <= t < 58:
        return slide()
    x = camera_x(t)
    frame = board_at(t)[:, x : x + W].copy()
    lecturer(frame, t)
    return frame


def render(path: Path) -> Path:
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
        "-f", "lavfi", "-i", f"sine=frequency=300:duration={SECONDS}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-shortest", str(path),
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin is not None
    for i in range(SECONDS * FPS):
        proc.stdin.write(frame_at(i / FPS).tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path
