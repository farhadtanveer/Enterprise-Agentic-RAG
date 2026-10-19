"""M4: default admin key must warn in dev, refuse to start elsewhere;
key comparison must be constant-time."""
import pytest
from fastapi.testclient import TestClient

import app.api.routes as routes
from app.core.config import Settings, get_settings, validate_admin_key
from app.main import app

client = TestClient(app)


def _settings(**kwargs):
    # _env_file=None keeps real .env values out of the assertion
    return Settings(_env_file=None, **kwargs)


def test_default_key_refused_outside_development():
    s = _settings(app_env="production", admin_api_key="change-me")
    with pytest.raises(RuntimeError, match="change-me"):
        validate_admin_key(s)


def test_default_key_warns_in_development(caplog):
    s = _settings(app_env="development", admin_api_key="change-me")
    validate_admin_key(s)  # must not raise
    assert "effectively unprotected" in caplog.text


def test_real_key_never_raises_even_in_production():
    validate_admin_key(_settings(app_env="production", admin_api_key="a-real-key"))


def test_ingest_key_check(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(s, "admin_api_key", "test-admin-key")
    monkeypatch.setattr(s, "upload_dir", str(tmp_path))
    monkeypatch.setattr(routes, "load_file", lambda p: [])
    monkeypatch.setattr(routes, "chunk_documents", lambda docs: [])
    monkeypatch.setattr(routes, "add_documents", lambda chunks: [])

    files = {"file": ("notes.txt", b"hello")}
    ok = client.post("/api/ingest", headers={"X-Admin-Key": "test-admin-key"}, files=files)
    bad = client.post("/api/ingest", headers={"X-Admin-Key": "wrong-key"}, files=files)

    assert ok.status_code == 200
    assert ok.json()["file"] == "notes.txt"
    assert bad.status_code == 401
