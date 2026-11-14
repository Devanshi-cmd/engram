from pathlib import Path

from fastapi.testclient import TestClient

from lectern.config import Settings
from lectern.web.app import OUTPUTS, SECURITY_HEADERS, create_app


def client(
    tmp_path: Path, *, codes: frozenset[str] = frozenset(), uploads: bool = False
) -> TestClient:
    settings = Settings(data_dir=tmp_path, access_codes=codes)
    return TestClient(create_app(settings, allowed_hosts=["testserver"], accept_uploads=uploads))


def test_home_shows_every_output_and_the_theme(tmp_path: Path) -> None:
    response = client(tmp_path).get("/")
    assert response.status_code == 200
    for _, label, _ in OUTPUTS:
        assert label in response.text
    assert "/static/theme.css" in response.text
    assert 'class="theme-switch"' in response.text


def test_upload_form_needs_both_the_pipeline_and_access_codes(tmp_path: Path) -> None:
    assert "dropzone" not in client(tmp_path, codes=frozenset({"x"})).get("/").text
    assert "dropzone" not in client(tmp_path, uploads=True).get("/").text
    page = client(tmp_path, codes=frozenset({"x"}), uploads=True).get("/").text
    assert 'name="video"' in page and 'name="access_code"' in page


def test_every_response_carries_the_security_headers(tmp_path: Path) -> None:
    c = client(tmp_path)
    for path in ("/", "/healthz", "/static/theme.css", "/missing"):
        response = c.get(path)
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value, path


def test_unknown_page_uses_the_themed_error_page(tmp_path: Path) -> None:
    response = client(tmp_path).get("/no-such-page")
    assert response.status_code == 404
    assert "Not Found." in response.text and "Back to Lectern" in response.text


def test_unknown_host_is_rejected(tmp_path: Path) -> None:
    response = client(tmp_path).get("/", headers={"host": "evil.example"})
    assert response.status_code == 400


def test_static_assets_exist(tmp_path: Path) -> None:
    c = client(tmp_path)
    for asset in ("theme.css", "lectern.css", "site.js", "htmx.min.js", "favicon.svg"):
        assert c.get(f"/static/{asset}").status_code == 200, asset
