"""Flags and the compact timeline: what Opus reads instead of 120 images.

The timeline interleaves what was on screen with what was said, in time order. It's kept
small, following Recall (174 KB → 84 KB on a one-hour video):

- a frame whose writing adds at most one new line, and isn't flagged, is skipped;
- writing shown earlier isn't repeated, only the new lines are printed;
- descriptions are capped, and shorter when the writing itself is shown;
- speech is grouped into paragraphs of about 30 seconds, students marked as such.

Flags tell Opus what to double-check by opening the image itself.
"""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel

from engram.llm.frames import FrameReading
from engram.speech.models import Transcript
from engram.timeline.align import Span

SPEECH_LOGPROB_FLAG = -0.6  # mean word log-probability of a segment below this is doubtful
FRAME_CONFIDENCE_FLAG = 0.6
PARAGRAPH_SECONDS = 30
FLAG_MIN_TEXT = 40  # a doubtful frame with less writing than this isn't worth opening
SAME_LINE = 0.6  # letter-trigram overlap at which two transcribed lines are the same line
FILLERS = re.compile(r"(?<!\w)(?:um+|uh+|erm|hmm)[,.]?\s+", re.I)
# The lecturer points at something; if the frame on screen says little, Opus should look.
DEICTIC = re.compile(
    r"\b(as you can see|look at this|over here|this one here|this diagram|this picture|"
    r"this curve|this figure|right here)\b",
    re.I,
)


class Flag(BaseModel):
    t: float
    kind: str  # speech | frame
    reason: str
    frame: str | None = None
    text: str = ""


def clock(seconds: float) -> str:
    s = int(seconds)
    h, m, s = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def build_flags(
    transcript: Transcript, spans: list[Span], readings: dict[str, FrameReading]
) -> list[Flag]:
    flags: list[Flag] = []
    for seg in transcript.segments:
        why = []
        if seg.logprob_mean is not None and seg.logprob_mean < SPEECH_LOGPROB_FLAG:
            why.append(f"low speech-recognition confidence ({seg.logprob_mean})")
        words = seg.text.split()
        if len(words) > 12 and len(set(words)) / len(words) < 0.3:
            why.append("repetitive text (a recognition loop?)")
        if why:
            flags.append(Flag(t=seg.start, kind="speech", reason="; ".join(why), text=seg.text))

    first_seen: dict[str, float] = {}
    for span in spans:
        first_seen.setdefault(span.frame, span.start)
    for frame, reading in readings.items():
        # Low confidence on a fragment (a cut-off heading, a blank patch) isn't worth a look.
        substantive = len(reading.key_text.strip()) >= FLAG_MIN_TEXT
        if reading.confidence < FRAME_CONFIDENCE_FLAG and substantive:
            reason = reading.review_reason or "low confidence"
            flags.append(
                Flag(
                    t=first_seen.get(frame, 0),
                    kind="frame",
                    frame=frame,
                    reason=f"confidence {reading.confidence:.2f}: {reason}",
                )  # fmt: skip
            )
    for span in spans:
        shown = readings.get(span.frame)
        if shown and DEICTIC.search(span.text) and len(shown.description) < 60:
            flags.append(
                Flag(
                    t=span.start,
                    kind="frame",
                    frame=span.frame,
                    reason="the lecturer points at the screen, but the reading says little",
                )  # fmt: skip
            )
    return sorted(flags, key=lambda f: f.t)


def main_speaker(transcript: Transcript) -> str | None:
    counts = Counter(w.speaker for w in transcript.words if w.speaker)
    return counts.most_common(1)[0][0] if counts else None


def build_timeline(
    title: str,
    duration: float,
    transcript: Transcript,
    spans: list[Span],
    readings: dict[str, FrameReading],
    flags: list[Flag],
) -> str:
    flagged = {f.frame for f in flags if f.frame}
    lecturer = main_speaker(transcript)
    lines = [
        f"# {title}",
        f"Duration {clock(duration)} · {len(readings)} frames · {len(flags)} flags · "
        f"{len(transcript.words):,} words",
        "",
        "Frames are written as [mm:ss] FRAME <id> (kind, confidence). Writing under a frame is",
        "verbatim; only lines not shown before are listed. ⚠ marks a frame or passage to check:",
        "open frames/<id>.jpg to see it yourself.",
        "",
    ]
    seen_lines = _Seen()
    for span in spans:
        reading = readings.get(span.frame)
        if reading is not None:
            block = _frame_block(span, reading, seen_lines, span.frame in flagged, flags)
            lines += block
        lines += _speech(transcript, span, lecturer)
    return "\n".join(lines).rstrip() + "\n"


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"(\.\.\.|…|\[\?\])\s*$", "", line)).strip()


def _grams(line: str) -> set[str]:
    t = re.sub(r"[^a-z0-9]+", " ", line.lower())
    return {t[i : i + 3] for i in range(len(t) - 2) if " " not in t[i : i + 3]}


class _Seen:
    """Lines already shown. Haiku transcribes the same writing a little differently each
    time ("(learning rate $\\alpha$)" vs "learning rate"), so matching is on letter
    trigrams, not exact text."""

    def __init__(self) -> None:
        self.grams: list[set[str]] = []

    def is_new(self, line: str) -> bool:
        g = _grams(line)
        if not g:
            return False
        return all(len(g & other) / len(g) < SAME_LINE for other in self.grams)

    def add(self, line: str) -> None:
        if g := _grams(line):
            self.grams.append(g)


def _frame_block(
    span: Span, reading: FrameReading, seen: _Seen, flagged: bool, flags: list[Flag]
) -> list[str]:
    text_lines = [_norm(line) for line in reading.key_text.splitlines() if _norm(line)]
    new = [line for line in text_lines if seen.is_new(line)]
    for line in text_lines:
        seen.add(line)
    if not flagged and text_lines and len(new) <= 1 and sum(len(x) for x in new) < 25:
        return []  # nothing new on screen
    if not flagged and not text_lines and reading.kind in ("talking_head", "other"):
        return []
    mark = " ⚠" if flagged else ""
    head = (
        f"[{clock(span.start)}] FRAME {span.frame}{mark} "
        f"({reading.kind}, conf {reading.confidence:.2f})"
    )
    desc = reading.description
    cap = 200 if new else 360
    if len(desc) > cap:
        desc = desc[:cap].rsplit(" ", 1)[0] + "…"
    out = ["", f"{head}: {desc}"]
    if reading.diagram:
        out.append(f"Diagram: {reading.diagram[:300]}")
    if flagged:
        why = next((f.reason for f in flags if f.frame == span.frame), "")
        out.append(f"⚠ CHECK: {why[:200]}")
    if new:
        out += ["```" if len(new) == len(text_lines) else "```  (new lines only)", *new, "```"]
    out.append("")
    return out


def _speech(transcript: Transcript, span: Span, lecturer: str | None) -> list[str]:
    words = transcript.words[span.first_word : span.end_word]
    out: list[str] = []
    para: list[str] = []
    start = None
    speaker = None

    def flush() -> None:
        nonlocal para, start
        if para and start is not None:
            who = "" if speaker in (None, lecturer) else "(student) "
            text = FILLERS.sub("", " ".join(para))
            out.append(f"[{clock(start)}] {who}{text}")
        para, start = [], None

    for word in words:
        if para and (word.speaker != speaker or word.start - (start or 0) > PARAGRAPH_SECONDS):
            flush()
        if start is None:
            start, speaker = word.start, word.speaker
        para.append(word.text)
    flush()
    return out
