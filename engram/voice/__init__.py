"""The narrator: text in, speech audio and the time of every word out."""

from engram.voice.models import Narration, Narrator, SpokenWord, words_from_characters

__all__ = ["Narration", "Narrator", "SpokenWord", "words_from_characters"]
