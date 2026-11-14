"""The pipeline's stages, in dependency order. Each one reads and writes files in the job folder."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from typing import Any

import cv2
from pydantic import BaseModel

from engram.course.assemble import build_course, item_ids, to_markdown
from engram.course.chapters import Chapter, plan_chapters, split_timeline
from engram.course.generate import check_diagrams, write_chapter
from engram.course.grounding import Item, Sources, card_item, ground, quiz_item
from engram.course.prompts import PROMPT_VERSION
from engram.course.schemas import ChapterContent, ItemVerdict, SummaryScript
from engram.course.script import words as script_words
from engram.course.script import write_script
from engram.errors import EngramError, MediaError, ProviderError
from engram.llm.frames import ClaudeCodeFrameReader, FrameContext, FrameReader, FrameReading, Read
from engram.llm.judge import Judge
from engram.media import MediaInfo, extract_audio, make_web_copy, probe, web_playable
from engram.pipeline.hashing import file_sha256, key_of
from engram.pipeline.runner import PaidCache, Stage, StageContext
from engram.pipeline.thinking import Thinker
from engram.render import compose
from engram.render.captions import webvtt
from engram.speech import Transcript, to_segments
from engram.timeline import Anchors, Span, align_words
from engram.timeline.compact import build_flags, build_timeline, clock
from engram.vision.keyframes import extract_frame, find_keyframes
from engram.vision.similar import dhash, group_same
from engram.voice.models import Narration


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


def _playback(ctx: StageContext) -> str:
    """What the course page plays: the upload itself when every browser can, else a copy."""
    source = ctx.job.source_path
    copy = ctx.path("playback.mp4")
    if web_playable(source):
        copy.unlink(missing_ok=True)
        ctx.write_json("playback.json", {"file": None})
        return "the upload plays as it is"
    info = MediaInfo.model_validate(ctx.read_json("media.json"))
    total = _clock(info.duration)

    def report(done: float) -> None:
        ctx.progress(f"Web copy: {_clock(done * info.duration)} of {total}")

    make_web_copy(source, copy, info.duration, report)
    ctx.write_json("playback.json", {"file": "playback.mp4", "bytes": copy.stat().st_size})
    return f"{info.video_codec} → H.264 · {copy.stat().st_size / 1e6:.0f} MB"


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


# --- The course: Opus plans and writes, Haiku checks, code assembles ---------------------

CHAPTER_WORKERS = 4
THUMB_WIDTH = 480


def _thinker(ctx: StageContext) -> Thinker:
    return Thinker(ctx.services.get_reasoner(), ctx.services.cache, ctx.job_dir)


def _usage(thinker: Thinker) -> str:
    report = thinker.report()
    fresh = report["fresh_calls"]
    if not fresh:
        return "reused"
    calls = f"{fresh} Opus call{'s' if fresh > 1 else ''}"
    return f"{calls} · {_clock(report['seconds'])} of thinking"


def _chapters(ctx: StageContext) -> str:
    info = MediaInfo.model_validate(ctx.read_json("media.json"))
    timeline = split_timeline(ctx.path("timeline.md").read_text())
    thinker = _thinker(ctx)
    ctx.progress("Opus is reading the whole timeline")
    title, overview, chapters = plan_chapters(thinker, timeline, info.duration)
    ctx.write_json(
        "chapters.json",
        {"title": title, "overview": overview, "chapters": [asdict(c) for c in chapters],
         "reasoning": thinker.report()},
    )  # fmt: skip
    return f"{len(chapters)} chapters · {_usage(thinker)}"


def _load_chapters(ctx: StageContext) -> tuple[dict[str, Any], list[Chapter]]:
    plan = ctx.read_json("chapters.json")
    return plan, [Chapter(**c) for c in plan["chapters"]]


def _write(ctx: StageContext) -> str:
    plan, chapters = _load_chapters(ctx)
    timeline = split_timeline(ctx.path("timeline.md").read_text())
    thinker = _thinker(ctx)
    course = plan["title"] or ctx.job.title

    def one(chapter: Chapter) -> tuple[int, ChapterContent, dict[str, Any]]:
        try:
            content = write_chapter(thinker, course, chapters, chapter, timeline)
        except ProviderError:  # one retry: a long Opus call can time out or hit a rate limit
            content = write_chapter(thinker, course, chapters, chapter, timeline)
        checked = check_diagrams(thinker, f"chapter {chapter.number}", content.diagrams)
        content = content.model_copy(update={"diagrams": checked.kept})
        return chapter.number, content, {"repaired": checked.repaired, "dropped": checked.dropped}

    ctx.progress(f"Opus is writing {len(chapters)} chapters, {CHAPTER_WORKERS} at a time")
    out: dict[str, Any] = {}
    with ThreadPoolExecutor(CHAPTER_WORKERS) as pool:
        futures = [pool.submit(one, c) for c in chapters]
        for done, future in enumerate(as_completed(futures), 1):
            number, content, diagrams = future.result()
            out[str(number)] = {"content": content.model_dump(), "diagrams": diagrams}
            ctx.progress(f"Wrote {done} of {len(chapters)} chapters")
    ctx.write_json("content.json", {"chapters": out, "reasoning": thinker.report()})
    contents = [ChapterContent.model_validate(v["content"]) for v in out.values()]
    cards = sum(len(c.flashcards) for c in contents)
    questions = sum(len(c.quiz) for c in contents)
    dropped = sum(len(v["diagrams"]["dropped"]) for v in out.values())
    parts = [f"{cards} cards", f"{questions} questions",
             f"{sum(len(c.diagrams) for c in contents)} diagrams"]  # fmt: skip
    if dropped:
        parts.append(f"{dropped} broken diagram{'s' if dropped > 1 else ''} dropped")
    return " · ".join([*parts, _usage(thinker)])


class _CachedJudge:
    """The same check of the same items is never paid for twice."""

    def __init__(self, judge: Judge, cache: PaidCache) -> None:
        self.judge, self.cache, self.name = judge, cache, judge.name
        self.reused = 0

    def run[T: BaseModel](self, prompt: str, *, system: str, schema: type[T]) -> tuple[T, float]:
        key = key_of({"by": self.judge.name, "system": system, "prompt": prompt})
        saved = self.cache.get("grounding", key)
        if saved is not None:
            self.reused += 1
            return schema.model_validate(saved), 0.0
        value, cost = self.judge.run(prompt, system=system, schema=schema)
        self.cache.put("grounding", key, value.model_dump())
        return value, cost


def _anchors(ctx: StageContext) -> Anchors:
    transcript = Transcript.model_validate(ctx.read_json("transcript.json"))
    spans = [Span.model_validate(s) for s in ctx.read_json("alignment.json")]
    return Anchors(transcript.words, spans)


def _contents(ctx: StageContext) -> dict[int, ChapterContent]:
    raw = ctx.read_json("content.json")["chapters"]
    return {int(n): ChapterContent.model_validate(v["content"]) for n, v in raw.items()}


def _check(ctx: StageContext) -> str:
    _, chapters = _load_chapters(ctx)
    contents = _contents(ctx)
    anchors = _anchors(ctx)
    frames = ctx.read_json("readings.json")["frames"]
    sources = Sources(anchors, {f: r["key_text"] for f, r in frames.items()})
    judge = _CachedJudge(ctx.services.get_judge(), ctx.services.cache)

    def one(chapter: Chapter) -> tuple[dict[str, ItemVerdict], float]:
        content = contents[chapter.number]
        within = (chapter.start, chapter.end)
        card_ids, quiz_ids = item_ids(chapter.number, content)
        items: list[Item] = []
        for item_id, card in zip(card_ids, content.flashcards, strict=True):
            m = anchors.locate(card.anchor, within)
            items.append(card_item(item_id, card, m.t if m else None, m.frame if m else None))
        for item_id, q in zip(quiz_ids, content.quiz, strict=True):
            m = anchors.locate(q.anchor, within)
            items.append(quiz_item(item_id, q, m.t if m else None, m.frame if m else None))
        return ground(judge, sources, items, within)

    ctx.progress(f"{judge.name} is checking every card and question against the lecture")
    verdicts: dict[str, ItemVerdict] = {}
    cost = 0.0
    with ThreadPoolExecutor(CHAPTER_WORKERS) as pool:
        for found, spent in pool.map(one, chapters):
            verdicts.update(found)
            cost += spent
    ctx.add_cost(cost)
    ctx.write_json(
        "grounding.json",
        {"judge": judge.name, "cost_usd": round(cost, 5),
         "verdicts": {k: v.model_dump() for k, v in verdicts.items()}},
    )  # fmt: skip
    bad = [v for v in verdicts.values() if v.verdict != "supported"]
    parts = [f"{len(verdicts)} checked", f"{len(bad)} removed", f"${cost:.3f}"]
    if judge.reused:
        parts.append("reused")
    return " · ".join(parts)


def _course(ctx: StageContext) -> str:
    info = MediaInfo.model_validate(ctx.read_json("media.json"))
    plan, chapters = _load_chapters(ctx)
    content = ctx.read_json("content.json")
    grounding = ctx.read_json("grounding.json")
    course = build_course(
        title=plan["title"], lecture_title=ctx.job.title, overview=plan["overview"],
        duration=info.duration, chapters=chapters, contents=_contents(ctx),
        diagram_notes={int(n): v["diagrams"] for n, v in content["chapters"].items()},
        verdicts={k: ItemVerdict.model_validate(v) for k, v in grounding["verdicts"].items()},
        anchors=_anchors(ctx),
    )  # fmt: skip
    calls = plan["reasoning"]["calls"] + content["reasoning"]["calls"]
    opened = sorted({f for c in calls for f in c["files_read"] if f.startswith("frames/")})
    course["stats"] = {
        "opus_calls": len(plan["reasoning"]["calls"]) + len(content["reasoning"]["calls"]),
        "opus_seconds": plan["reasoning"]["seconds"] + content["reasoning"]["seconds"],
        "frames_opened_by_opus": opened,
        "grounding_judge": grounding["judge"],
        "grounding_cost_usd": grounding["cost_usd"],
    }
    thumbs = ctx.path("thumbs")
    thumbs.mkdir(exist_ok=True)
    for frame in course["frames"]:
        _thumbnail(ctx.path(f"frames/{frame}.jpg"), thumbs / f"{frame}.jpg")
    ctx.write_json("course.json", course)
    ctx.path("course.md").write_text(to_markdown(course))
    chs = course["chapters"]
    points = sum(len(s["points"]) for c in chs for s in c["sections"])
    cards = sum(len(c["flashcards"]) for c in chs)
    questions = sum(len(c["quiz"]) for c in chs)
    diagrams = sum(len(c["diagrams"]) for c in chs)
    parts = [f"{len(chs)} chapters", f"{points} notes", f"{diagrams} diagrams",
             f"{cards} cards", f"{questions} questions"]  # fmt: skip
    if course["unplaced"]:
        parts.append(f"{len(course['unplaced'])} without a timestamp")
    return " · ".join(parts)


def _thumbnail(source: Any, target: Any) -> None:
    image = cv2.imread(str(source))
    if image is None:
        return
    h, w = image.shape[:2]
    if w > THUMB_WIDTH:
        image = cv2.resize(
            image, (THUMB_WIDTH, round(h * THUMB_WIDTH / w)), interpolation=cv2.INTER_AREA
        )
    cv2.imwrite(str(target), image, [cv2.IMWRITE_JPEG_QUALITY, 80])


# --- The narrated summary ----------------------------------------------------------------


def _script(ctx: StageContext) -> str:
    course = ctx.read_json("course.json")
    readings = ctx.read_json("readings.json")["frames"]
    thinker = _thinker(ctx)
    ctx.progress("Opus is writing the narration")
    script = write_script(thinker, course, readings)
    if len(script.scenes) < 2:
        raise ProviderError("Opus wrote a summary script with fewer than two scenes.")
    ctx.write_json("script.json", {**script.model_dump(), "reasoning": thinker.report()})
    return f"{len(script.scenes)} scenes · {script_words(script)} words · {_usage(thinker)}"


def _load_script(ctx: StageContext) -> SummaryScript:
    raw = ctx.read_json("script.json")
    return SummaryScript.model_validate({k: v for k, v in raw.items() if k != "reasoning"})


def _narrate(ctx: StageContext) -> str:
    narrator = ctx.services.narrator
    if narrator is None:
        raise EngramError("No narrator voice: set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID.")
    text = " ".join(s.narration for s in _load_script(ctx).scenes)
    key = key_of({"by": narrator.name, "text": text})
    saved = ctx.services.cache.get("narrations", key)
    audio_cache = ctx.services.cache.root / "narrations" / f"{key}.mp3"
    if saved is not None and audio_cache.exists():
        narration = Narration.model_validate(saved)
        audio = audio_cache.read_bytes()
        reused = " · reused"
    else:
        ctx.progress(f"Recording {len(text):,} characters of narration")
        audio, narration = narrator.speak(text)
        audio_cache.parent.mkdir(parents=True, exist_ok=True)
        audio_cache.write_bytes(audio)
        ctx.services.cache.put("narrations", key, narration.model_dump())
        reused = ""
    ctx.path("narration.mp3").write_bytes(audio)
    ctx.write_json("narration.json", narration.model_dump())
    words = len(narration.words)
    return f"{narration.duration:.1f} s · {words} words · {len(text):,} characters{reused}"


def _video(ctx: StageContext) -> str:
    script = _load_script(ctx)
    narration = Narration.model_validate(ctx.read_json("narration.json"))
    shots, total = compose.plan_shots(script, narration, ctx.path("frames"))
    formats = [compose.LANDSCAPE, compose.PORTRAIT]
    ctx.progress(f"Rendering {total:.0f} s in landscape and portrait")

    def one(fmt: compose.Format) -> None:
        name = "summary.mp4" if fmt is compose.LANDSCAPE else "summary-portrait.mp4"
        compose.render(fmt, script, shots, narration, ctx.path("narration.mp3"), total,
                       ctx.path(name))  # fmt: skip

    with ThreadPoolExecutor(len(formats)) as pool:
        list(pool.map(one, formats))
    compose.poster(compose.LANDSCAPE, script, shots, narration, ctx.path("summary.jpg"))
    chunks = compose.Renderer(compose.LANDSCAPE, script, shots, narration).chunks
    ctx.path("summary.vtt").write_text(webvtt(chunks))
    size = sum(ctx.path(n).stat().st_size for n in ("summary.mp4", "summary-portrait.mp4"))
    return f"{total:.0f} s · {len(shots)} shots · 1920×1080 and 1080×1350 · {size / 1e6:.1f} MB"


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
        "playback",
        "Making a copy every browser plays",
        _playback,
        deps=("probe",),
        outputs=("playback.json",),
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
    Stage(
        "chapters",
        "Finding the chapters",
        _chapters,
        deps=("probe", "timeline"),
        outputs=("chapters.json",),
        params=lambda s: {"by": s.get_reasoner().name, "prompts": PROMPT_VERSION},
    ),
    Stage(
        "write",
        "Writing notes, diagrams, cards and the quiz",
        _write,
        deps=("timeline", "chapters"),
        outputs=("content.json",),
        params=lambda s: {"by": s.get_reasoner().name, "prompts": PROMPT_VERSION},
    ),
    Stage(
        "check",
        "Checking cards and questions against the lecture",
        _check,
        deps=("transcribe", "align", "read_frames", "chapters", "write"),
        outputs=("grounding.json",),
        params=lambda s: {"by": s.get_judge().name, "prompts": PROMPT_VERSION},
    ),
    Stage(
        "course",
        "Putting the course together",
        _course,
        deps=("probe", "transcribe", "align", "read_frames", "chapters", "write", "check"),
        outputs=("course.json", "course.md", "thumbs"),
    ),
    Stage(
        "script",
        "Writing the summary narration",
        _script,
        deps=("read_frames", "course"),
        outputs=("script.json",),
        version=2,  # v2: 150-190 words, one shortening pass past 200
        params=lambda s: {"by": s.get_reasoner().name, "prompts": PROMPT_VERSION},
    ),
    Stage(
        "narrate",
        "Recording the narration",
        _narrate,
        deps=("script",),
        outputs=("narration.mp3", "narration.json"),
        params=lambda s: {"by": s.narrator.name if s.narrator else None},
    ),
    Stage(
        "video",
        "Rendering the summary video",
        _video,
        deps=("keyframes", "script", "narrate"),
        outputs=("summary.mp4", "summary-portrait.mp4", "summary.jpg", "summary.vtt"),
        version=3,  # v3: letterboxed slides shown whole; captions split by sentence
    ),
]
