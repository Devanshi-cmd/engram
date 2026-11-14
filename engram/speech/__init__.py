"""Speech to text: a `Transcriber` turns lecture audio into timed words."""

from engram.speech.models import Segment, Transcriber, Transcript, Word
from engram.speech.segments import to_segments

__all__ = ["Segment", "Transcript", "Transcriber", "Word", "to_segments"]
