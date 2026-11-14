"""Fuzzy text matching shared by the timeline and anchors: letter trigrams within words."""

from __future__ import annotations

import re


def grams(text: str) -> set[str]:
    """Letter trigrams that don't cross a word boundary. Robust to small spelling and
    punctuation differences ("θ_j" vs "theta j" aside), and cheap to intersect."""
    t = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return {t[i : i + 3] for i in range(len(t) - 2) if " " not in t[i : i + 3]}


def overlap(needle: set[str], haystack: set[str]) -> float:
    """How much of `needle` appears in `haystack`, 0..1."""
    return len(needle & haystack) / len(needle) if needle else 0.0
