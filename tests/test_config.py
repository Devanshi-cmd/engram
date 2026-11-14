from pathlib import Path

import pytest

from lectern.config import load_settings


def test_placeholders_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "your-elevenlabs-api-key")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    monkeypatch.setenv("LECTERN_ACCESS_CODES", "change-me")
    settings = load_settings(env_file=None)
    assert settings.elevenlabs_api_key is None
    assert settings.elevenlabs_voice_id is None
    assert settings.anthropic_api_key == "sk-real"
    assert not settings.uploads_enabled


def test_access_codes_are_split_and_trimmed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LECTERN_ACCESS_CODES", " alpha, beta ,,")
    monkeypatch.setenv("LECTERN_DATA_DIR", str(tmp_path))
    settings = load_settings(env_file=None)
    assert settings.access_codes == {"alpha", "beta"}
    assert settings.data_dir == tmp_path
