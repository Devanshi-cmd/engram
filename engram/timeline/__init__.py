"""Lines the lecture up in time: which words were spoken while which frame was on screen."""

from engram.timeline.align import Span, align_words
from engram.timeline.anchor import Anchor, Anchors, Moment

__all__ = ["Anchor", "Anchors", "Moment", "Span", "align_words"]
