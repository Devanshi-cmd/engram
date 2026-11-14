"""Near-identical frames, so the same slide shown twice is read once."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

SAME_BITS = 6  # of 256: frames this close look the same to a person


def dhash(image: Path) -> int:
    """A 256-bit difference hash: brighter-than-its-right-neighbour, on a 17×16 thumbnail."""
    gray = cv2.imread(str(image), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise ValueError(f"not an image: {image}")
    small = cv2.resize(gray, (17, 16), interpolation=cv2.INTER_AREA).astype(np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    return int("".join("1" if b else "0" for b in bits), 2)


def distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def group_same(hashes: dict[str, int]) -> dict[str, str]:
    """Maps each frame id to the first frame (in id order) it looks the same as."""
    leaders: list[tuple[str, int]] = []
    owner: dict[str, str] = {}
    for frame in sorted(hashes):
        h = hashes[frame]
        match = next((lead for lead, lh in leaders if distance(h, lh) <= SAME_BITS), None)
        if match is None:
            leaders.append((frame, h))
            owner[frame] = frame
        else:
            owner[frame] = match
    return owner
