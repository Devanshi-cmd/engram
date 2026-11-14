"""Everything drawn with Pillow: title cards, heading chips, caption lines.

Colours are DevSwarm's tokens (Apple's palette): page #f5f5f7, ink #1d1d1f, muted #6e6e73,
action #0071e3. The font is Inter, the closest open-licence match to Apple's SF Pro (which
can't be redistributed). Images are RGBA; the compositor converts them to BGR.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = Path(__file__).parent / "fonts"

PAGE = (245, 245, 247)
SURFACE = (255, 255, 255)
INK = (29, 29, 31)
MUTED = (110, 110, 115)
FAINT = (134, 134, 139)
ACTION = (0, 113, 227)
PANEL = (29, 29, 31)

RGBA = tuple[int, int, int, int]


@cache
def font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS / f"Inter-{weight}.ttf"), size)


def wrap(text: str, face: ImageFont.FreeTypeFont, width: float) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and face.getlength(f"{lines[-1]} {word}") <= width:
            lines[-1] = f"{lines[-1]} {word}"
        else:
            lines.append(word)
    return lines


def tracked(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    face: ImageFont.FreeTypeFont,
    fill: tuple[int, ...],
    tracking: float,
) -> float:
    """Draws letter-spaced text (Apple's small caps labels); returns the width."""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=face, fill=fill)
        x += face.getlength(ch) + tracking
    return x - xy[0] - tracking  # fmt: skip


def tracked_width(text: str, face: ImageFont.FreeTypeFont, tracking: float) -> float:
    return sum(face.getlength(ch) for ch in text) + tracking * max(0, len(text) - 1)


def title_card(size: tuple[int, int], label: str, title: str, subtitle: str) -> Image.Image:
    """A Keynote-style card: a blue small-caps label, a bold title, a muted subtitle."""
    w, h = size
    s = min(w / 1920, h / 1080) if w >= h else w / 1080 * 0.9  # portrait: scale by width
    image = Image.new("RGBA", size, (*PAGE, 255))
    draw = ImageDraw.Draw(image)
    title_face = font("Bold", round(92 * s))
    sub_face = font("Medium", round(40 * s))
    label_face = font("SemiBold", round(26 * s))
    margin = round(w * 0.1)
    lines = wrap(title, title_face, w - 2 * margin)
    subs = wrap(subtitle, sub_face, w - 2 * margin) if subtitle else []
    line_h, sub_h = round(title_face.size * 1.1), round(sub_face.size * 1.35)
    block = (round(label_face.size * 2.2) if label else 0) + line_h * len(lines)
    block += (round(sub_face.size * 0.8) + sub_h * len(subs)) if subs else 0
    y = (h - block) / 2
    if label:
        tracked(draw, (margin, y), label.upper(), label_face, ACTION, label_face.size * 0.12)
        y += round(label_face.size * 2.2)
    for line in lines:
        draw.text((margin, y), line, font=title_face, fill=INK)
        y += line_h
    if subs:
        y += round(sub_face.size * 0.8)
        for line in subs:
            draw.text((margin, y), line, font=sub_face, fill=MUTED)
            y += sub_h
    return image


def end_card(size: tuple[int, int], url: str) -> Image.Image:
    w, h = size
    s = min(w / 1920, h / 1080) if w >= h else w / 1080 * 0.9
    image = Image.new("RGBA", size, (*PAGE, 255))
    draw = ImageDraw.Draw(image)
    mark = font("Bold", round(120 * s))
    line = font("Medium", round(38 * s))
    link = font("SemiBold", round(38 * s))
    texts = [
        ("Engram", mark, INK),
        ("Notes, flashcards and a quiz from this lecture", line, MUTED),
        (url, link, ACTION),
    ]
    heights = [round(mark.size * 1.25), round(line.size * 1.6), round(link.size * 1.4)]
    y = (h - sum(heights)) / 2
    for (text, face, colour), step in zip(texts, heights, strict=True):
        for part in wrap(text, face, w * 0.86):
            draw.text(((w - face.getlength(part)) / 2, y), part, font=face, fill=colour)
            y += step
    return image


def chip(text: str, scale: float) -> Image.Image:
    """A frosted pill with the scene's heading, for the top-left of a landscape frame."""
    face = font("SemiBold", round(30 * scale))
    pad_x, pad_y = round(24 * scale), round(13 * scale)
    tw = face.getlength(text)
    w, h = round(tw + 2 * pad_x), round(face.size + 2 * pad_y)
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(255, 255, 255, 222))
    draw.text((pad_x, pad_y - round(face.size * 0.06)), text, font=face, fill=INK)
    return _with_shadow(image, round(16 * scale), 40)


def caption(
    words: list[str], spoken: int, *, max_width: int, scale: float, on_dark: bool
) -> Image.Image:
    """Caption text, up to two lines. The first `spoken` words are full strength, the
    rest faint, so the line fills in as the narrator speaks. `on_dark` puts it in a dark
    pill (over a frame); otherwise it's ink on the page (portrait)."""
    face = font("SemiBold", round(44 * scale))
    space = face.getlength(" ")
    lines: list[list[int]] = [[]]
    for i in range(len(words)):
        trial = [*lines[-1], i]
        if (
            lines[-1]
            and sum(face.getlength(words[j]) for j in trial) + space * (len(trial) - 1) > max_width
        ):
            lines.append([i])
        else:
            lines[-1] = trial
    line_h = round(face.size * 1.3)
    ascent, descent = face.getmetrics()
    widths = [sum(face.getlength(words[j]) for j in ln) + space * (len(ln) - 1) for ln in lines]
    pad_x, pad_y = (round(30 * scale), round(14 * scale)) if on_dark else (0, 0)
    w = round(max(widths) + 2 * pad_x)
    h = line_h * (len(lines) - 1) + ascent + descent + 2 * pad_y
    image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if on_dark:
        radius = min(h // 2, round(30 * scale))
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=(*PANEL, 196))
        strong, faint = (255, 255, 255, 255), (255, 255, 255, 140)
    else:
        strong, faint = (*INK, 255), (*FAINT, 150)
    y = pad_y
    for line, width in zip(lines, widths, strict=True):
        x = (w - width) / 2
        for j in line:
            draw.text((x, y), words[j], font=face, fill=strong if j < spoken else faint)
            x += face.getlength(words[j]) + space
        y += line_h
    return image


def heading_block(text: str, width: int, scale: float) -> Image.Image:
    """Portrait: the scene's heading above the frame, bold ink, up to two lines."""
    face = font("Bold", round(58 * scale))
    lines = wrap(text, face, width)[:2]
    line_h = round(face.size * 1.12)
    image = Image.new("RGBA", (width, max(1, line_h * len(lines))), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    for i, line in enumerate(lines):
        draw.text(((width - face.getlength(line)) / 2, i * line_h), line, font=face, fill=INK)
    return image


def panel_shadow(size: tuple[int, int], box: tuple[int, int, int, int], radius: int) -> Image.Image:
    """The soft card shadow (DevSwarm's --shadow) under the portrait frame panel."""
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    x0, y0, x1, y1 = box
    ImageDraw.Draw(layer).rounded_rectangle((x0, y0 + 10, x1, y1 + 10), radius=radius,
                                            fill=(0, 0, 0, 34))  # fmt: skip
    return layer.filter(ImageFilter.GaussianBlur(26))


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius,
                                           fill=255)  # fmt: skip
    return mask


def _with_shadow(image: Image.Image, blur: int, alpha: int) -> Image.Image:
    pad = blur * 2
    w, h = image.size
    out = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
    shadow = Image.new("RGBA", out.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((pad, pad + blur // 3, pad + w, pad + h + blur // 3),
                                             radius=h // 2, fill=(0, 0, 0, alpha))  # fmt: skip
    out.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(blur)))
    out.alpha_composite(image, (pad, pad))
    return out
