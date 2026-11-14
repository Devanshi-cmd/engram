"""ElevenLabs Scribe: one request per lecture, with word timestamps and speaker labels."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from engram.errors import CreditsExhausted, ProviderError
from engram.speech.models import Transcript, Word

API = "https://api.elevenlabs.io/v1"
MODEL = "scribe_v2"
MAX_UPLOAD_BYTES = 1_000_000_000  # Scribe's file limit; a 2-hour lecture at 64 kbps is ~56 MB
_RETRY_STATUS = {429, 500, 502, 503, 504}
_KEYTERM_MAX_CHARS = 49  # longer key terms are rejected by the API
_KEYTERM_MAX_COUNT = 100
CREDITS_PER_HOUR = 1350  # measured by Recall: 837 credits for 37 minutes with key terms


def clean_keyterms(terms: list[str]) -> list[str]:
    """Short, plain phrases only. Scribe rejects long or bracketed key terms."""
    seen: set[str] = set()
    out: list[str] = []
    for term in terms:
        t = re.sub(r"\s+", " ", term).strip()[:_KEYTERM_MAX_CHARS].strip()
        if t and len(t.split()) <= 5 and not re.search(r"[<>{}\[\]\\]", t) and t not in seen:
            seen.add(t)
            out.append(t)
    return out[:_KEYTERM_MAX_COUNT]


class ScribeTranscriber:
    name = f"elevenlabs-{MODEL}"

    def __init__(
        self,
        api_key: str,
        *,
        client: httpx.Client | None = None,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key = api_key
        self._client = client or httpx.Client(timeout=httpx.Timeout(1800, connect=30))
        self._attempts = attempts
        self._sleep = sleep

    def transcribe(self, audio: Path, keyterms: list[str]) -> Transcript:
        if audio.stat().st_size > MAX_UPLOAD_BYTES:
            raise ProviderError("The audio is over Scribe's 1 GB limit.")
        return parse_response(self.raw(audio, keyterms))

    def minutes_left(self) -> float | None:
        """Roughly how many minutes of lecture the account can still transcribe.

        None if the balance can't be read: a balance check must never fail a job.
        """
        try:
            response = self._client.get(
                f"{API}/user/subscription", headers={"xi-api-key": self._key}, timeout=30
            )
            response.raise_for_status()
            sub = response.json()
            left = int(sub["character_limit"]) - int(sub["character_count"])
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return None
        return max(left, 0) * 60 / CREDITS_PER_HOUR

    def raw(self, audio: Path, keyterms: list[str]) -> dict[str, Any]:
        """The untouched API response, so the caller can cache exactly what was paid for."""
        data: dict[str, str | list[str]] = {
            "model_id": MODEL,
            "timestamps_granularity": "word",
            "diarize": "true",
            "tag_audio_events": "false",
            "keyterms": clean_keyterms(keyterms),  # a list is sent as repeated fields
        }
        for attempt in range(1, self._attempts + 1):
            try:
                with audio.open("rb") as f:
                    response = self._client.post(
                        f"{API}/speech-to-text",
                        headers={"xi-api-key": self._key},
                        data=data,
                        files={"file": (audio.name, f, "audio/mpeg")},
                    )
            except httpx.TransportError as e:
                if attempt == self._attempts:
                    raise ProviderError(f"Couldn't reach ElevenLabs: {e}") from e
                self._sleep(20 * attempt)
                continue
            if response.status_code == 200:
                body: dict[str, Any] = response.json()
                return body
            detail = response.text[:500]
            if response.status_code == 402 or re.search(
                r"quota_exceeded|insufficient_credits", detail
            ):
                raise CreditsExhausted("ElevenLabs")
            if response.status_code in _RETRY_STATUS and attempt < self._attempts:
                self._sleep(20 * attempt)
                continue
            raise ProviderError(
                f"ElevenLabs transcription failed ({response.status_code}): {detail[:200]}"
            )
        raise ProviderError("ElevenLabs transcription failed after retries.")  # pragma: no cover


def parse_response(body: dict[str, Any]) -> Transcript:
    """Keeps spoken words only (not spacing or audio events), in time order."""
    words = [
        Word(
            text=str(w["text"]).strip(),
            start=float(w["start"]),
            end=float(w["end"]),
            speaker=w.get("speaker_id"),
            logprob=w.get("logprob"),
        )
        for w in body.get("words", [])
        if w.get("type", "word") == "word" and w.get("start") is not None and str(w["text"]).strip()
    ]
    words.sort(key=lambda w: w.start)
    return Transcript(
        language=body.get("language_code"), words=words, source=ScribeTranscriber.name
    )
