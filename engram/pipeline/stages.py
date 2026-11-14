"""The pipeline's stages, in dependency order. Each one reads and writes files in the job folder."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

from engram.errors import MediaError, ProviderError
from engram.llm.frames import ClaudeCodeFrameReader, FrameContext, FrameReader, FrameReading, Read
from engram.media import MediaInfo, extract_audio, probe
from engram.pipeline.hashing import file_sha256, key_of
from engram.pipeline.runner import Stage, StageContext
from engram.speech import Transcript, to_segments
from engram.timeline import Span, align_words
from engram.timeline.compact import build_flags, build_timeline, clock
from engram.vision.keyframes import extract_frame, find_keyframes
from engram.vision.similar import dhash, group_same


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


def _keyframes(ctx: StageContext) -> str:
    info = MediaInfo.model_validate(ctx.read_json("media.json"))
    video = ctx.job.source_path
    total = _clock(info.duration)
    result, times = find_keyframes(
        video, info.duration, progress=lambda t: ctx.progress(f"Scanning {_clock(t)} of {total}")
    )
    folder = ctx.path("frames")
    folder.mkdir(exist_ok=True)
    for stale in folder.glob("*.jpg"):
        stale.unlink()
    ctx.progress(f"Saving {len(result.keyframes)} frames at full size")
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda k: extract_frame(video, times[k.id], folder / f"{k.id}.jpg"),
                      result.keyframes))  # fmt: skip
    ctx.write_json(
        "keyframes.json",
        {
            "frames": [{**asdict(k), "file": f"frames/{k.id}.jpg"} for k in result.keyframes],
            "stats": {
                "sampled": result.samples,
                "views": result.views,
                "dropped_as_covered": result.dropped_as_covered,
            },
        },
    )
    readable = sum(k.has_text for k in result.keyframes)
    return (
        f"{len(result.keyframes)} frames from {result.views} views · "
        f"{readable} with writing · {result.samples:,} sampled"
    )


def _align(ctx: StageContext) -> str:
    info = MediaInfo.model_validate(ctx.read_json("media.json"))
    transcript = Transcript.model_validate(ctx.read_json("transcript.json"))
    frames = ctx.read_json("keyframes.json")["frames"]
    views = [(f["id"], start, end) for f in frames for start, end in f["views"]]
    spans = align_words(views, transcript.words, info.duration)
    ctx.write_json("alignment.json", [s.model_dump() for s in spans])
    silent = sum(1 for s in spans if s.first_word == s.end_word)
    return f"{len(spans)} spans · {silent} without speech"


READ_WORKERS = 8
SPEECH_CONTEXT_CHARS = 3000  # what the lecturer said while a frame was up, for the reader


def _read_frames(ctx: StageContext) -> str:
    frames = ctx.read_json("keyframes.json")["frames"]
    spans = [Span.model_validate(s) for s in ctx.read_json("alignment.json")]
    reader: FrameReader = ctx.services.frame_reader or ClaudeCodeFrameReader(ctx.job_dir)
    by_frame: dict[str, list[Span]] = {}
    for span in spans:
        by_frame.setdefault(span.frame, []).append(span)

    # The same slide shown twice is read once.
    owner = group_same({f["id"]: dhash(ctx.path(f["file"])) for f in frames})
    leaders = [f for f in frames if owner[f["id"]] == f["id"]]

    def context(frame_id: str) -> FrameContext:
        mine = [s for f, o in owner.items() if o == frame_id for s in by_frame.get(f, [])]
        mine.sort(key=lambda s: s.start)
        speech = " … ".join(s.text for s in mine if s.text)
        if len(speech) > SPEECH_CONTEXT_CHARS:
            speech = speech[:SPEECH_CONTEXT_CHARS].rsplit(" ", 1)[0] + " …"
        on_screen = ", ".join(f"{clock(s.start)}–{clock(s.end)}" for s in mine) or "unknown"
        return FrameContext(ctx.job.title, on_screen, speech)

    def read_one(frame: dict[str, str]) -> tuple[str, Read, bool]:
        image = ctx.path(frame["file"])
        c = context(frame["id"])
        key = key_of({"image": file_sha256(image), "by": reader.name, "context": c.render()})
        saved = ctx.services.cache.get("frame-readings", key)
        if saved is not None:
            return frame["id"], Read(FrameReading.model_validate(saved)), True
        result = reader.read(image, c)
        if result.error is None:
            ctx.services.cache.put("frame-readings", key, result.reading.model_dump())
        return frame["id"], result, False

    ctx.progress(f"Reading {len(leaders)} frames with {reader.name}")
    readings: dict[str, FrameReading] = {}
    errors: list[str] = []
    cost = 0.0
    reused = 0
    with ThreadPoolExecutor(READ_WORKERS) as pool:
        for done, (frame_id, result, cached) in enumerate(pool.map(read_one, leaders), 1):
            readings[frame_id] = result.reading
            cost += result.cost_usd
            reused += cached
            if result.error:
                errors.append(f"{frame_id}: {result.error[:120]}")
            if done % 10 == 0:
                ctx.progress(f"Read {done} of {len(leaders)} frames")
    if leaders and len(errors) > len(leaders) / 2:
        raise ProviderError(f"Most frames couldn't be read. First error: {errors[0]}")
    ctx.add_cost(cost)
    out = {
        f["id"]: {**readings[owner[f["id"]]].model_dump(), "same_as": None
                  if owner[f["id"]] == f["id"] else owner[f["id"]]}
        for f in frames
    }  # fmt: skip
    ctx.write_json("readings.json", {"reader": reader.name, "cost_usd": round(cost, 5),
                                     "frames": out, "errors": errors})  # fmt: skip
    low = sum(1 for r in readings.values() if r.confidence < 0.6)
    same = len(frames) - len(leaders)
    parts = [f"{len(leaders)} read", f"${cost:.3f}"]
    if same:
        parts.append(f"{same} repeats")
    if reused:
        parts.append(f"{reused} reused")
    parts.append(f"{low} low-confidence")
    if errors:
        parts.append(f"{len(errors)} failed")
    return " · ".join(parts)


def _timeline(ctx: StageContext) -> str:
    info = MediaInfo.model_validate(ctx.read_json("media.json"))
    transcript = Transcript.model_validate(ctx.read_json("transcript.json"))
    spans = [Span.model_validate(s) for s in ctx.read_json("alignment.json")]
    raw = ctx.read_json("readings.json")["frames"]
    readings = {k: FrameReading.model_validate(
        {f: v for f, v in r.items() if f != "same_as"}) for k, r in raw.items()}  # fmt: skip
    flags = build_flags(transcript, spans, readings)
    ctx.write_json("flags.json", [f.model_dump() for f in flags])
    text = build_timeline(ctx.job.title, info.duration, transcript, spans, readings, flags)
    ctx.path("timeline.md").write_text(text)
    return f"{len(text) / 1000:.0f} KB · {len(flags)} flags"


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
        version=2,  # v2: filler fragments wait up to 5 s for the rest of their sentence
        params=lambda s: {"by": s.transcriber.name},
    ),
    Stage(
        "keyframes",
        "Finding the key frames",
        _keyframes,
        deps=("probe",),
        outputs=("keyframes.json", "frames"),
    ),
    Stage(
        "align",
        "Lining up speech and frames",
        _align,
        deps=("probe", "transcribe", "keyframes"),
        outputs=("alignment.json",),
    ),
    Stage(
        "read_frames",
        "Reading the board and slides",
        _read_frames,
        deps=("keyframes", "align"),
        outputs=("readings.json",),
        params=lambda s: {"reader": s.frame_reader_name},
    ),
    Stage(
        "timeline",
        "Writing the timeline",
        _timeline,
        deps=("probe", "transcribe", "align", "read_frames"),
        outputs=("timeline.md", "flags.json"),
        version=2,  # v2: fuzzy repeat-line matching, fillers dropped, fragments not flagged
    ),
]
