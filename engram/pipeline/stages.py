"""The pipeline's stages, in dependency order. Each one reads and writes files in the job folder."""

from __future__ import annotations

from engram.errors import MediaError
from engram.media import extract_audio, probe
from engram.pipeline.hashing import file_sha256, key_of
from engram.pipeline.runner import Stage, StageContext
from engram.speech import Transcript, to_segments


def _probe(ctx: StageContext) -> str:
    info = probe(ctx.job.source_path)
    limit = ctx.services.settings.max_minutes
    if info.duration > limit * 60:
        raise MediaError(
            f"This lecture is {info.duration / 60:.0f} minutes long. The limit is {limit} minutes."
        )
    ctx.write_json("media.json", info.model_dump())
    return f"{_clock(info.duration)} · {info.width}×{info.height} · {info.fps:g} fps"


def _audio(ctx: StageContext) -> str:
    extract_audio(ctx.job.source_path, ctx.path("audio.mp3"))
    return f"{ctx.path('audio.mp3').stat().st_size / 1e6:.1f} MB"


def _keyterms(ctx: StageContext) -> list[str]:
    """Spellings Scribe should prefer. The title is the best hint before frames are read."""
    title = ctx.job.title
    parts = [p.strip() for p in title.replace("–", "|").replace("—", "|").split("|")]
    return [title, *parts]


def _transcribe(ctx: StageContext) -> str:
    transcriber = ctx.services.transcriber
    audio = ctx.path("audio.mp3")
    keyterms = _keyterms(ctx)
    key = key_of({"audio": file_sha256(audio), "by": transcriber.name, "keyterms": keyterms})
    saved = ctx.services.cache.get("transcripts", key)
    if saved is not None:
        transcript = Transcript.model_validate(saved)
        ctx.progress("Reusing a transcript already paid for")
    else:
        transcript = transcriber.transcribe(audio, keyterms)
        ctx.services.cache.put("transcripts", key, transcript.model_dump())
        left = transcriber.minutes_left()
        if left is not None and left < LOW_BALANCE_MINUTES:
            ctx.progress(
                f"Low ElevenLabs balance: about {left:.0f} minutes of lecture left. "
                "Top up before the next upload."
            )
    transcript.segments = to_segments(transcript.words)
    ctx.write_json("transcript.json", transcript.model_dump())
    speakers = {w.speaker for w in transcript.words if w.speaker}
    who = f" · {len(speakers)} speakers" if len(speakers) > 1 else ""
    return f"{len(transcript.words):,} words · {len(transcript.segments):,} segments{who}"


LOW_BALANCE_MINUTES = 60


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


STAGES: list[Stage] = [
    Stage(
        "probe",
        "Checking the video",
        _probe,
        outputs=("media.json",),
        params=lambda s: {"max_minutes": s.settings.max_minutes},
    ),
    Stage(
        "audio", "Extracting the audio", _audio, deps=("probe",), outputs=("audio.mp3",), version=2
    ),
    Stage(
        "transcribe",
        "Transcribing the speech",
        _transcribe,
        deps=("audio",),
        outputs=("transcript.json",),
        params=lambda s: {"by": s.transcriber.name},
    ),
]
