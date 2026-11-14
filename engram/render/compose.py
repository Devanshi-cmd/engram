"""Composes the summary video frame by frame and pipes it to ffmpeg.

Each scene of the script becomes a shot, timed by the narrator's word timings: a scene
starts when its first word is spoken. Frames get a slow Ken Burns move towards the writing
(the centre of the board's ink), shots crossfade, and captions fill in word by word.

Drawing in Python (rather than ffmpeg's zoompan and an ASS file) gives sub-pixel smooth
motion (zoompan rounds to whole pixels, which judders on slow moves) and captions styled
exactly like the web app. The 720p frames are scaled up with bicubic interpolation only:
no AI upscaling, so nothing is invented on the board.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Literal

import cv2
import numpy as np
from numpy.typing import NDArray
from PIL import Image

from engram.course.schemas import SummaryScript
from engram.errors import MediaError
from engram.render import design
from engram.render.captions import Chunk, chunk
from engram.voice.models import Narration

FPS = 30
FADE = 0.45  # seconds of crossfade between shots
LEAD = 0.5  # silence before the narrator starts
TAIL = 0.7  # after the last word, before the end card
END_CARD = 2.8
ZOOM = 0.10  # Ken Burns: from the whole frame to 10% closer, towards the writing
URL = "engram.devanshitiwari.com"

Frame = NDArray[Any]  # BGR uint8 pixels (OpenCV's types are too loose to be precise)


@dataclass(frozen=True)
class Format:
    name: str
    width: int
    height: int

    @property
    def scale(self) -> float:
        return self.width / 1920 if self.width >= self.height else self.width / 1080


LANDSCAPE = Format("landscape", 1920, 1080)
PORTRAIT = Format("portrait", 1080, 1350)


@dataclass(frozen=True)
class Shot:
    start: float
    end: float
    kind: Literal["title", "frame", "end"]
    heading: str
    image: Path | None = None


def plan_shots(
    script: SummaryScript, narration: Narration, frames: Path
) -> tuple[list[Shot], float]:
    """Shots timed by the narration, and the video's total length."""
    counts = [len(s.narration.split()) for s in script.scenes]
    words = narration.words
    starts: list[float] = []
    index = 0
    for count in counts:
        if len(words) == sum(counts) and index < len(words):
            starts.append(words[index].start)
        else:  # the voice split words differently: fall back to proportional timing
            starts.append(narration.duration * index / max(1, sum(counts)))
        index += count
    speech_end = LEAD + narration.duration + TAIL
    shots: list[Shot] = []
    for i, scene in enumerate(script.scenes):
        start = 0.0 if i == 0 else max(0.0, LEAD + starts[i] - 0.15)
        end = LEAD + starts[i + 1] - 0.15 if i + 1 < len(starts) else speech_end
        if scene.frame:
            shots.append(Shot(start, end, "frame", scene.heading, frames / f"{scene.frame}.jpg"))
        else:
            shots.append(Shot(start, end, "title", scene.heading))
    shots.append(Shot(speech_end, speech_end + END_CARD, "end", ""))
    return shots, speech_end + END_CARD


@cache
def _load(path: Path) -> tuple[Frame, tuple[float, float], bool]:
    """(pixels without letterbox bars, where the writing is, whether bars were cut)."""
    image = cv2.imread(str(path))
    if image is None:
        raise MediaError(f"Can't read the frame {path.name}.")
    trimmed = trim_bars(image)
    return trimmed, _ink_centre(trimmed), trimmed.shape != image.shape


def trim_bars(image: Frame) -> Frame:
    """Cuts black letterbox or pillarbox bars (a 4:3 slide inside a 16:9 recording). A row
    or column is content if more than 8% of it is lit, so a watermark over a bar (the
    "Stanford" logo) doesn't count as content."""
    lit = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) > 30
    rows = np.where(lit.mean(axis=1) > 0.08)[0]
    cols = np.where(lit.mean(axis=0) > 0.08)[0]
    h, w = lit.shape
    if len(rows) < h * 0.5 or len(cols) < w * 0.5:
        return image  # mostly dark: a blackboard, not bars
    trimmed: Frame = image[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1]
    if trimmed.shape[0] > h * 0.97 and trimmed.shape[1] > w * 0.97:
        return image  # a sliver: noise at the edge, not bars
    return trimmed


def _ink_centre(image: Frame) -> tuple[float, float]:
    """Where the writing is: the centre of mass of strokes darker or lighter than the board."""
    gray = cv2.cvtColor(cv2.resize(image, None, fx=0.25, fy=0.25), cv2.COLOR_BGR2GRAY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    ink = np.maximum(cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, kernel),
                     cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel))  # fmt: skip
    m = cv2.moments((ink > 18).astype(np.uint8))
    h, w = image.shape[:2]
    if m["m00"] < 50:
        return w / 2, h / 2
    return m["m10"] / m["m00"] * 4, m["m01"] / m["m00"] * 4


def _ease(p: float) -> float:
    p = min(1.0, max(0.0, p))
    return p * p * (3 - 2 * p)


def ken_burns(
    source: Frame, focus: tuple[float, float], size: tuple[int, int], p: float, slide: bool = False
) -> Frame:
    """The slow move. A board is cropped to fill the space; a slide that was letterboxed
    (`slide`) is shown whole on the page colour when its shape differs from the space."""
    sh, sw = source.shape[:2]
    out_w, out_h = size
    cover = max(out_w / sw, out_h / sh)
    contain = min(out_w / sw, out_h / sh)
    if slide and cover / contain > 1.15:
        # A slide of another shape: show all of it on the page colour, zoom gently.
        k = contain * (1 + ZOOM * 0.5 * p)
        m = np.array([[k, 0, out_w / 2 - k * sw / 2], [0, k, out_h / 2 - k * sh / 2]])
        fitted: Frame = cv2.warpAffine(source, m, size, flags=cv2.INTER_CUBIC,
                                       borderMode=cv2.BORDER_CONSTANT,
                                       borderValue=design.PAGE[::-1])  # fmt: skip
        return fitted
    k = cover * (1 + ZOOM * p)
    # Start part-way towards the writing (a 4:3 crop of a 16:9 frame loses its edges), end on it.
    q = 0.5 + 0.5 * p
    cx = sw / 2 + (focus[0] - sw / 2) * q
    cy = sh / 2 + (focus[1] - sh / 2) * q
    half_w, half_h = out_w / (2 * k), out_h / (2 * k)
    cx = min(max(cx, half_w), sw - half_w)
    cy = min(max(cy, half_h), sh - half_h)
    m = np.array([[k, 0, out_w / 2 - k * cx], [0, k, out_h / 2 - k * cy]], dtype=np.float64)
    out: Frame = cv2.warpAffine(source, m, size, flags=cv2.INTER_CUBIC,
                                borderMode=cv2.BORDER_REPLICATE)  # fmt: skip
    return out


def _bgr(image: Image.Image) -> tuple[Frame, Frame]:
    """RGBA → (BGR pixels, alpha 0..1 with a channel axis)."""
    rgba = np.asarray(image.convert("RGBA"))
    bgr = np.ascontiguousarray(rgba[..., 2::-1])
    return bgr, (rgba[..., 3:4].astype(np.float32) / 255)


def _paste(base: Frame, layer: tuple[Frame, Frame], x: int, y: int) -> None:
    pixels, alpha = layer
    h, w = pixels.shape[:2]
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, base.shape[1]), min(y + h, base.shape[0])
    if x1 <= x0 or y1 <= y0:
        return
    region = base[y0:y1, x0:x1].astype(np.float32)
    px = pixels[y0 - y : y1 - y, x0 - x : x1 - x]
    a = alpha[y0 - y : y1 - y, x0 - x : x1 - x]
    base[y0:y1, x0:x1] = (region * (1 - a) + px * a).astype(np.uint8)


class Renderer:
    def __init__(
        self, fmt: Format, script: SummaryScript, shots: list[Shot], narration: Narration
    ) -> None:
        self.fmt, self.script, self.shots = fmt, script, shots
        self.size = (fmt.width, fmt.height)
        s = fmt.scale
        portrait = fmt.height > fmt.width
        self.chunks: list[Chunk] = chunk(narration.words, 40 if portrait else 52, LEAD)
        self._captions: dict[tuple[int, int], tuple[Frame, Frame]] = {}
        self._static: dict[int, Frame] = {}
        self._chips: dict[str, tuple[Frame, Frame]] = {}
        if portrait:
            margin = round(48 * s)
            pw = fmt.width - 2 * margin
            ph = round(pw * 3 / 4)  # 4:3: a taller window on the board than 16:9
            top = round(fmt.height * 0.2)
            self.panel = (margin, top, margin + pw, top + ph)
            self.radius = round(28 * s)
            mask = np.asarray(design.rounded_mask((pw, ph), self.radius), dtype=np.float32)
            self.panel_mask = (mask / 255)[..., None]
            self.caption_y = top + ph + round(48 * s)
            self.caption_width = pw - round(40 * s)
        else:
            self.caption_width = round(1500 * s)

    # --- static layers, drawn once per shot -------------------------------------------------

    def _static_layer(self, i: int) -> Frame:
        if i in self._static:
            return self._static[i]
        shot, fmt, s = self.shots[i], self.fmt, self.fmt.scale
        if shot.kind == "end":
            out = _bgr(design.end_card(self.size, URL))[0]
        elif shot.kind == "title":
            first = i == 0
            title = self.script.title if first else shot.heading
            subtitle = self.script.subtitle if first else ""
            label = "Lecture summary" if first else ""
            out = _bgr(design.title_card(self.size, label, title, subtitle))[0]
        elif fmt.height > fmt.width:
            canvas = Image.new("RGBA", self.size, (*design.PAGE, 255))
            canvas.alpha_composite(design.panel_shadow(self.size, self.panel, self.radius))
            block = design.heading_block(shot.heading, self.panel[2] - self.panel[0], s)
            y = self.panel[1] - round(40 * s) - block.height
            canvas.alpha_composite(block, (self.panel[0], max(round(60 * s), y)))
            out = _bgr(canvas)[0]
        else:
            out = np.zeros((fmt.height, fmt.width, 3), np.uint8)  # the frame covers it all
        self._static[i] = out
        return out

    # --- one frame --------------------------------------------------------------------------

    def _shot(self, i: int, t: float) -> Frame:
        shot = self.shots[i]
        base = self._static_layer(i).copy()
        if shot.kind != "frame" or shot.image is None:
            return base
        source, focus, slide = _load(shot.image)
        p = _ease((t - shot.start) / max(0.1, shot.end + FADE - shot.start))
        if self.fmt.height > self.fmt.width:
            x0, y0, x1, y1 = self.panel
            moving = ken_burns(source, focus, (x1 - x0, y1 - y0), p, slide)
            region = base[y0:y1, x0:x1].astype(np.float32)
            a = self.panel_mask
            base[y0:y1, x0:x1] = (region * (1 - a) + moving * a).astype(np.uint8)
        else:
            base = ken_burns(source, focus, self.size, p, slide)
            if shot.heading:
                s = self.fmt.scale
                _paste(base, self._chip(shot.heading), round(40 * s), round(32 * s))
        return base

    def _chip(self, heading: str) -> tuple[Frame, Frame]:
        if heading not in self._chips:
            self._chips[heading] = _bgr(design.chip(heading, self.fmt.scale))
        return self._chips[heading]

    def frame(self, t: float) -> Frame:
        i = max(j for j, s in enumerate(self.shots) if s.start <= t or j == 0)
        out = self._shot(i, t)
        start = self.shots[i].start
        if i > 0 and t < start + FADE:
            a = _ease((t - start) / FADE)
            out = cv2.addWeighted(self._shot(i - 1, t), 1 - a, out, a, 0)
        if self.shots[i].kind != "end":
            self._caption(out, t)
        return out

    def _caption(self, out: Frame, t: float) -> None:
        current = next(((k, c) for k, c in enumerate(self.chunks) if c.start <= t < c.end), None)
        if current is None:
            return
        k, c = current
        spoken = sum(1 for w in c.words if w.start + LEAD <= t)
        key = (k, spoken)
        if key not in self._captions:
            image = design.caption(
                [w.text for w in c.words], spoken, max_width=self.caption_width,
                scale=self.fmt.scale, on_dark=self.fmt.width >= self.fmt.height,
            )  # fmt: skip
            self._captions[key] = _bgr(image)
        layer = self._captions[key]
        h, w = layer[0].shape[:2]
        x = (self.fmt.width - w) // 2
        y = (self.caption_y if self.fmt.height > self.fmt.width
             else self.fmt.height - round(64 * self.fmt.scale) - h)  # fmt: skip
        _paste(out, layer, x, y)


def render(
    fmt: Format,
    script: SummaryScript,
    shots: list[Shot],
    narration: Narration,
    audio: Path,
    total: float,
    out: Path,
    fps: int | None = None,
) -> None:
    fps = fps or FPS
    renderer = Renderer(fmt, script, shots, narration)
    tmp = out.with_name(f".{out.name}.tmp.mp4")
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{fmt.width}x{fmt.height}",
        "-r", str(fps), "-i", "-",
        "-i", str(audio),
        "-filter_complex", f"[1:a]adelay={round(LEAD * 1000)}:all=1,apad[a]",
        "-map", "0:v", "-map", "[a]", "-t", f"{total:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(tmp),
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for n in range(round(total * fps)):
            proc.stdin.write(renderer.frame(n / fps).tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    error = proc.stderr.read().decode() if proc.stderr else ""
    if proc.wait() != 0:
        raise MediaError(f"ffmpeg couldn't encode the summary: {error[-300:]}")
    tmp.replace(out)


def poster(fmt: Format, script: SummaryScript, shots: list[Shot], narration: Narration,
           out: Path) -> None:  # fmt: skip
    """A still for the player before it starts: the middle of the first frame shot."""
    renderer = Renderer(fmt, script, shots, narration)
    shot = next((s for s in shots if s.kind == "frame"), shots[0])
    t = (shot.start + shot.end) / 2
    cv2.imwrite(str(out), renderer._shot(shots.index(shot), t), [cv2.IMWRITE_JPEG_QUALITY, 86])
