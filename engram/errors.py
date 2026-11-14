"""Errors that carry a message written for the person running the job, not a traceback."""

from __future__ import annotations


class EngramError(Exception):
    """A failure with a message that can be shown as-is on the job page."""


class MediaError(EngramError):
    """ffmpeg or ffprobe failed, or the file isn't a usable lecture video."""


class ProviderError(EngramError):
    """A paid API (ElevenLabs, Anthropic) failed after its retries."""


class CreditsExhausted(ProviderError):
    """The provider account is out of credits. The job can be retried after a top-up."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"{provider} is out of credits. Top up, then retry the job.")
