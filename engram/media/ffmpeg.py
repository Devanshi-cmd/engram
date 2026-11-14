from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path

from pydantic import BaseModel

from engram.errors import MediaError


class MediaInfo(BaseModel):
    duration: float
    width: int
    height: int
    fps: float
    video_codec: str
    has_audio: bool
    size_bytes: int


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess[bytes]:
    """Runs a media tool. On failure, the error names the tool and quotes its last line."""
    try:
        return subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as e:
        raise MediaError(f"{cmd[0]} is not installed.") from e
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"{cmd[0]} took longer than {timeout:.0f} s and was stopped.") from e
    except subprocess.CalledProcessError as e:
        lines = e.stderr.decode(errors="replace").strip().splitlines()
        last = lines[-1] if lines else f"exit code {e.returncode}"
        raise MediaError(f"{cmd[0]} failed: {last[:300]}") from e


def probe(path: Path) -> MediaInfo:
    """Reads a video's streams. Rejects files without a video or an audio stream."""
    out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=60,
    )
    data = json.loads(out.stdout)
    streams = data.get("streams", [])
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    if video is None:
        raise MediaError("This file has no video stream. Engram needs to see the lecture.")
    if not has_audio:
        raise MediaError("This file has no audio stream. Engram needs to hear the lecture.")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise MediaError("Couldn't read the video's length. The file may be damaged.")
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    fps = float(Fraction(rate)) if rate != "0/0" else 0.0
    return MediaInfo(
        duration=round(duration, 3),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        fps=round(fps, 3),
        video_codec=str(video.get("codec_name", "unknown")),
        has_audio=has_audio,
        size_bytes=path.stat().st_size,
    )


def extract_audio(video: Path, out: Path) -> None:
    """Mono 16 kHz MP3 at 64 kbps: about 28 MB per hour, all speech recognition needs."""
    tmp = out.with_suffix(".part.mp3")
    _run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-vn",
            # No tags and a bit-exact encode: the same speech always gives the same bytes,
            # so a re-uploaded lecture hits the transcript cache instead of paying again.
            "-map_metadata",
            "-1",
            "-fflags",
            "+bitexact",
            "-flags:a",
            "+bitexact",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "64k",
            str(tmp),
        ],
        timeout=1800,
    )
    tmp.replace(out)


def web_playable(video: Path) -> bool:
    """True when every browser plays the file as it is: H.264 video and AAC audio in MP4."""
    out = _run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         str(video)],
        timeout=60,
    )  # fmt: skip
    data = json.loads(out.stdout)
    codecs = {s.get("codec_type"): s.get("codec_name") for s in data.get("streams", [])}
    container = str(data.get("format", {}).get("format_name", ""))
    return (
        codecs.get("video") == "h264"
        and codecs.get("audio") in (None, "aac")
        and ("mp4" in container)
    )


def make_web_copy(
    video: Path, out: Path, duration: float, progress: Callable[[float], None] | None = None
) -> None:
    """H.264 (at most 720p) and AAC in a fast-start MP4: plays in every browser, seeks at once."""
    tmp = out.with_name(f".{out.name}.part.mp4")
    cmd = [
        "ffmpeg", "-nostdin", "-y", "-v", "error", "-progress", "pipe:1", "-i", str(video),
        "-map", "0:v:0", "-map", "0:a:0?", "-vf", "scale=-2:'min(720,ih)'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-movflags", "+faststart", str(tmp),
    ]  # fmt: skip
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    reported = 0.0
    for line in proc.stdout:
        if line.startswith("out_time_us=") and progress and duration > 0:
            with contextlib.suppress(ValueError):
                done = min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration)
                if done - reported >= 0.05:  # every 5%, not every half second
                    progress(done)
                    reported = done
    error = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        raise MediaError(f"ffmpeg couldn't make a web copy: {error.strip()[-200:]}")
    tmp.replace(out)
