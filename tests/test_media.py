from pathlib import Path

import pytest

from engram.errors import MediaError
from engram.media import extract_audio, probe
from tests.conftest import make_video


def test_probe_reads_the_streams(lecture_video: Path) -> None:
    info = probe(lecture_video)
    assert info.duration == pytest.approx(3, abs=0.2)
    assert (info.width, info.height) == (320, 240)
    assert info.fps == pytest.approx(10)
    assert info.has_audio


def test_probe_rejects_a_silent_video(tmp_path: Path) -> None:
    silent = make_video(tmp_path / "silent.mp4", audio=False)
    with pytest.raises(MediaError, match="no audio"):
        probe(silent)


def test_probe_rejects_audio_only(tmp_path: Path) -> None:
    sound = make_video(tmp_path / "sound.m4a", video=False)
    with pytest.raises(MediaError, match="no video"):
        probe(sound)


def test_probe_names_the_tool_when_the_file_is_not_media(tmp_path: Path) -> None:
    junk = tmp_path / "notes.mp4"
    junk.write_text("not a video")
    with pytest.raises(MediaError, match="^ffprobe failed: "):
        probe(junk)


def test_extract_audio_writes_mono_16k(lecture_video: Path, tmp_path: Path) -> None:
    out = tmp_path / "audio.mp3"
    extract_audio(lecture_video, out)
    info = probe_audio(out)
    assert info == ("1", "16000")
    assert not list(tmp_path.glob("*.part.mp3"))


def probe_audio(path: Path) -> tuple[str, str]:
    import json
    import subprocess

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
        check=True,
        capture_output=True,
    )
    stream = json.loads(out.stdout)["streams"][0]
    return str(stream["channels"]), str(stream["sample_rate"])


def test_audio_bytes_ignore_container_metadata(lecture_video: Path, tmp_path: Path) -> None:
    import subprocess

    from engram.pipeline.hashing import file_sha256

    tagged = tmp_path / "tagged.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(lecture_video),
            "-c",
            "copy",
            "-metadata",
            "comment=re-uploaded",
            "-metadata",
            "title=Other",
            str(tagged),
        ],
        check=True,
    )
    a, b = tmp_path / "a.mp3", tmp_path / "b.mp3"
    extract_audio(lecture_video, a)
    extract_audio(tagged, b)
    assert file_sha256(a) == file_sha256(b)
