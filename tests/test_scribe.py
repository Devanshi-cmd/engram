from pathlib import Path

import httpx
import pytest

from engram.errors import CreditsExhausted, ProviderError
from engram.speech import Word, to_segments
from engram.speech.scribe import ScribeTranscriber, clean_keyterms, parse_response

RESPONSE = {
    "language_code": "eng",
    "words": [
        {
            "text": "Hello",
            "start": 0.0,
            "end": 0.4,
            "type": "word",
            "speaker_id": "speaker_0",
            "logprob": -0.1,
        },
        {"text": " ", "start": 0.4, "end": 0.5, "type": "spacing"},
        {"text": "(laughs)", "start": 0.5, "end": 0.9, "type": "audio_event"},
        {
            "text": "world.",
            "start": 1.0,
            "end": 1.4,
            "type": "word",
            "speaker_id": "speaker_0",
            "logprob": -0.3,
        },
    ],
}


def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"ID3fake")
    return path


def scribe(handler: object, sleeps: list[float] | None = None) -> ScribeTranscriber:
    client = httpx.Client(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return ScribeTranscriber(
        "key", client=client, sleep=(sleeps if sleeps is not None else []).append
    )


def test_parse_keeps_only_spoken_words() -> None:
    transcript = parse_response(RESPONSE)
    assert [w.text for w in transcript.words] == ["Hello", "world."]
    assert transcript.words[0].speaker == "speaker_0"
    assert transcript.language == "eng"


def test_request_sends_word_timestamps_and_keyterms(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RESPONSE)

    scribe(handler).transcribe(audio_file(tmp_path), ["Eigenvalues", "Gilbert Strang"])
    body = seen[0].read().decode(errors="replace")
    assert seen[0].headers["xi-api-key"] == "key"
    assert 'name="timestamps_granularity"\r\n\r\nword' in body
    assert body.count('name="keyterms"') == 2
    assert "Gilbert Strang" in body


def test_rate_limits_are_retried(tmp_path: Path) -> None:
    replies = iter([httpx.Response(429, text="slow down"), httpx.Response(200, json=RESPONSE)])
    sleeps: list[float] = []
    transcript = scribe(lambda _: next(replies), sleeps).transcribe(audio_file(tmp_path), [])
    assert len(transcript.words) == 2
    assert sleeps == [20]


def test_out_of_credits_is_reported_plainly(tmp_path: Path) -> None:
    with pytest.raises(CreditsExhausted, match="ElevenLabs is out of credits"):
        scribe(lambda _: httpx.Response(402, text="quota")).transcribe(audio_file(tmp_path), [])


def test_other_errors_give_up_with_the_status(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match=r"\(400\)"):
        scribe(lambda _: httpx.Response(400, text="bad")).transcribe(audio_file(tmp_path), [])


def test_keyterms_are_cleaned() -> None:
    terms = ["  Linear   Algebra ", "Linear Algebra", "x" * 80, "a b c d e f", "f[x]", ""]
    assert clean_keyterms(terms) == ["Linear Algebra", "x" * 49]


def words(*spec: tuple[str, float, float]) -> list[Word]:
    return [Word(text=t, start=s, end=e) for t, s, e in spec]


def test_segments_split_on_sentence_ends_and_pauses() -> None:
    segs = to_segments(
        words(
            ("So", 0, 0.3),
            ("today", 0.4, 0.8),
            ("we", 0.9, 1.0),
            ("start.", 1.1, 2.2),
            ("Matrices", 2.4, 3.0),
            ("multiply", 3.1, 3.6),
            ("rows", 3.7, 4.0),
            ("by", 4.1, 4.2),
            ("columns", 6.0, 6.5),
        )
    )
    assert [s.text for s in segs] == [
        "So today we start.",
        "Matrices multiply rows by",
        "columns",
    ]


def test_short_fragments_stay_with_the_next_words() -> None:
    segs = to_segments(words(("so", 0, 0.2), ("then", 1.6, 1.9), ("we", 2.0, 2.1)))
    assert [s.text for s in segs] == ["so then we"]


def test_minutes_left_from_the_subscription() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/user/subscription"
        return httpx.Response(200, json={"character_limit": 10_000, "character_count": 7_300})

    assert scribe(handler).minutes_left() == pytest.approx(120)


def test_minutes_left_never_raises() -> None:
    assert scribe(lambda _: httpx.Response(500)).minutes_left() is None
    assert scribe(lambda _: httpx.Response(200, json={})).minutes_left() is None
