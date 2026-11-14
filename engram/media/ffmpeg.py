from __future__ import annotations

import json
import subprocess
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
