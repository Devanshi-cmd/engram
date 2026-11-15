"""YouTube links: read a video's details, then download it for the pipeline.

The web app only reads the details (title, length, whether it's public), so a bad link is
refused at once. The worker downloads the video when the job runs, at up to 720p in H.264
and AAC, which every browser plays as it is.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel

from engram.errors import EngramError

MAX_HEIGHT = 720  # board text reads fine at 720p, and a 2-hour lecture stays near 1 GB

_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
          "youtube-nocookie.com", "www.youtube-nocookie.com"}  # fmt: skip


NOT_FOUND = "YouTube didn't return this video. Check the link and try again."
NOT_PUBLIC = "This video isn't public, so Engram can't fetch it."
DOWNLOAD_FAILED = "The download from YouTube failed. Try again later, or upload the file."


class YouTubeError(EngramError):
    """The link can't be used: not public, a live stream, too long, or YouTube refused."""


class VideoInfo(BaseModel):
    id: str
    title: str
    channel: str
    duration: float
    url: str  # the canonical watch link

    @property
    def source_sha(self) -> str:
        """Stands in for the file's hash: the same video is the same job."""
        return f"youtube:{self.id}"


class YouTube(Protocol):
    def info(self, video_id: str) -> VideoInfo: ...
    def download(self, video_id: str, out: Path) -> None: ...


def video_id(link: str) -> str | None:
    """The 11-character id from any common YouTube link, or None if it isn't one.

    Takes watch?v=, youtu.be/, /shorts/, /live/ and /embed/ links, with or without https."""
    link = link.strip()
    if not link:
        return None
    if "://" not in link:
        link = "https://" + link
    try:
        url = urlparse(link)
    except ValueError:
        return None
    host = (url.hostname or "").lower()
    parts = [p for p in url.path.split("/") if p]
    found: str | None = None
    if host in ("youtu.be", "www.youtu.be"):
        found = parts[0] if parts else None
    elif host in _HOSTS:
        if parts[:1] == ["watch"]:
            found = next(iter(parse_qs(url.query).get("v", [])), None)
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            found = parts[1]
    return found if found and _ID.match(found) else None


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


class _Quiet:
    """yt-dlp's logger: keep its errors for the message, drop the chatter."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        self.errors.append(msg)


def _readable(errors: list[str], fallback: str) -> str:
    """yt-dlp's error, rewritten for the person who pasted the link."""
    text = " ".join(errors).lower()
    if "private" in text:
        return "This video is private. Use a public or unlisted video."
    if "sign in to confirm your age" in text or "age-restricted" in text:
        return "This video is age-restricted, so Engram can't fetch it. Upload the file instead."
    if "members-only" in text or "join this channel" in text:
        return "This video is for channel members only. Upload the file instead."
    if "not a bot" in text or "http error 429" in text:
        return "YouTube is refusing downloads from this server right now. Upload the file instead."
    if "unavailable" in text or "removed" in text or "does not exist" in text:
        return "This video isn't available on YouTube."
    return fallback


class YtDlp:
    """The real thing, through yt-dlp. Node.js solves YouTube's player challenges."""

    def _options(self, logger: _Quiet, **extra: Any) -> dict[str, Any]:
        base = {"quiet": True, "no_warnings": True, "logger": logger, "noplaylist": True,
                "js_runtimes": {"node": {}}, "socket_timeout": 30, "retries": 3}  # fmt: skip
        return {**base, **extra}

    def info(self, vid: str) -> VideoInfo:
        import yt_dlp

        logger = _Quiet()
        try:
            with yt_dlp.YoutubeDL(self._options(logger)) as ydl:
                data = ydl.extract_info(watch_url(vid), download=False)
        except yt_dlp.utils.DownloadError:
            raise YouTubeError(_readable(logger.errors, NOT_FOUND)) from None
        if not data:
            raise YouTubeError(NOT_FOUND)
        if data.get("is_live") or data.get("live_status") in ("is_live", "is_upcoming"):
            raise YouTubeError("Live streams and premieres can't be used. Try once it has ended.")
        if data.get("availability") in ("private", "premium_only", "subscriber_only", "needs_auth"):
            raise YouTubeError(_readable([str(data.get("availability"))], NOT_PUBLIC))
        return VideoInfo(id=vid, title=str(data.get("title") or "Lecture"),
                         channel=str(data.get("channel") or data.get("uploader") or ""),
                         duration=float(data.get("duration") or 0), url=watch_url(vid))  # fmt: skip

    def download(self, vid: str, out: Path) -> None:
        import yt_dlp

        out.parent.mkdir(parents=True, exist_ok=True)
        part = out.with_name(f".{out.stem}.download")
        logger = _Quiet()
        options = self._options(
            logger,
            format=f"bv*[height<={MAX_HEIGHT}]+ba/b[height<={MAX_HEIGHT}]/b",
            format_sort=[f"res:{MAX_HEIGHT}", "vcodec:h264", "acodec:m4a"],
            merge_output_format="mp4",
            outtmpl=str(part) + ".%(ext)s",
        )
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                ydl.download([watch_url(vid)])
        except yt_dlp.utils.DownloadError:
            raise YouTubeError(_readable(logger.errors, DOWNLOAD_FAILED)) from None
        done = part.with_name(part.name + ".mp4")
        if not done.exists():
            raise YouTubeError(DOWNLOAD_FAILED)
        done.replace(out)
