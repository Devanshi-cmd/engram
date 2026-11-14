"""ElevenLabs text-to-speech with character timestamps, so captions land on the word."""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable

import httpx

from engram.errors import CreditsExhausted, ProviderError
from engram.voice.models import Narration, words_from_characters

API = "https://api.elevenlabs.io/v1"
MODEL = "eleven_multilingual_v2"
OUTPUT = "mp3_44100_128"
_RETRY_STATUS = {429, 500, 502, 503, 504}


class ElevenLabsNarrator:
    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        client: httpx.Client | None = None,
        attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._key, self.voice_id = api_key, voice_id
        self._client = client or httpx.Client(timeout=httpx.Timeout(300, connect=30))
        self._attempts, self._sleep = attempts, sleep
        self.name = f"elevenlabs-{MODEL}-{voice_id}"

    def speak(self, text: str) -> tuple[bytes, Narration]:
        url = f"{API}/text-to-speech/{self.voice_id}/with-timestamps?output_format={OUTPUT}"
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._client.post(
                    url,
                    headers={"xi-api-key": self._key},
                    json={"text": text, "model_id": MODEL},
                )
            except httpx.TransportError as e:
                if attempt == self._attempts:
                    raise ProviderError(f"Couldn't reach ElevenLabs: {e}") from e
                self._sleep(10 * attempt)
                continue
            if response.status_code == 200:
                return parse(text, response.json())
            detail = response.text[:500]
            if response.status_code == 402 or re.search(r"quota_exceeded|insufficient", detail):
                raise CreditsExhausted("ElevenLabs")
            if response.status_code in _RETRY_STATUS and attempt < self._attempts:
                self._sleep(10 * attempt)
                continue
            raise ProviderError(
                f"ElevenLabs speech failed ({response.status_code}): {detail[:200]}"
            )
        raise ProviderError("ElevenLabs speech failed after retries.")  # pragma: no cover


def parse(text: str, body: dict[str, object]) -> tuple[bytes, Narration]:
    alignment = body.get("alignment")
    if not isinstance(alignment, dict) or "characters" not in alignment:
        raise ProviderError("ElevenLabs returned speech without timings.")
    chars = "".join(alignment["characters"])
    starts = [float(x) for x in alignment["character_start_times_seconds"]]
    ends = [float(x) for x in alignment["character_end_times_seconds"]]
    words = words_from_characters(chars, starts, ends)
    audio = base64.b64decode(str(body["audio_base64"]))
    duration = ends[-1] if ends else 0.0
    return audio, Narration(text=chars or text, duration=duration, words=words)
