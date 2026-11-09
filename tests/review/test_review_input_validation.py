"""Input hygiene review (F3, review finding 3): client-supplied filenames
and titles reject NUL/control characters, Windows reserved names, reserved
filesystem characters, and over-length values with 422 — before any row,
file, or DB round-trip. display_name never reaches a filesystem path since
F1; these rules are defense in depth for the driver, the UI, and portable
exports."""
import io
from contextlib import contextmanager
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app as fastapi_app
from app.services.validation import display_name_problems, title_problems

PEER = ("203.0.113.5", 50000)
ADMIN_KEY = "test-admin-key"


# --- unit level -------------------------------------------------------------------


def test_display_name_problems_rejects_the_f3_zoo():
    for bad in (
        "bad\x00name.txt",           # NUL (the acceptance scenario)
        "line\nbreak.txt",           # C0 control
        "esc\x7f.txt",               # DEL
        "\x85next-line.txt",         # C1 control
        "CON.txt",                   # Windows reserved, with extension
        "con",                       # Windows reserved, bare, lowercase
        "LPT9.md",                   # reserved printer port
        "a|b.txt",                   # reserved filesystem character
        "x" * 256 + ".txt",          # over-length
        "trailing dot.",             # Windows strips trailing dots
        "trailing space ",           # Windows strips trailing spaces
    ):
        assert display_name_problems(bad), f"expected problems for {bad!r}"


def test_display_name_problems_allows_reasonable_names():
    for good in ("policy-notes.txt", "VPN handbook 2026.DOCX", "a.b.c.md", "备注.md"):
        assert display_name_problems(good) == [], f"{good!r} should be fine"


def test_title_problems_rejects_nul_and_controls_but_allows_none():
    assert title_problems("bad\x00title")
    assert title_problems("tab\tseparated")     # \t is a C0 control
    assert title_problems("x" * 201)
    assert title_problems(None) == []
    assert title_problems("a fine title") == []


# --- API level --------------------------------------------------------------------

MODULE = "app.api.routes"


@contextmanager
def _ingest_client(db_session, tmp_path, monkeypatch):
    from app.api import routes as legacy_routes

    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", ADMIN_KEY)
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(tmp_path / "uploads"))

    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER)
    finally:
        fastapi_app.dependency_overrides.clear()


def _post(client, name, data=b"hello"):
    return client.post(
        "/api/ingest",
        files={"file": (name, io.BytesIO(data), "application/octet-stream")},
        headers={"X-Admin-Key": ADMIN_KEY},
    )


def test_ingest_rejects_reserved_and_control_names_with_422(db_session, tmp_path, monkeypatch):
    # note: httpx percent-encodes NUL inside a filename, so the RAW-NUL case
    # is covered by the manual-multipart acceptance test
    # (test_review_destructive_paths.py::test_ingest_raw_nul_filename_is_client_error_not_500);
    # this test exercises the other F3 rejections through the API.
    with _ingest_client(db_session, tmp_path, monkeypatch) as client, \
         patch(f"{MODULE}.load_file"), \
         patch(f"{MODULE}.chunk_documents"), \
         patch(f"{MODULE}.add_documents", return_value=["id1"]):
        for name in ("CON.txt", "con.md", "a|b.txt", f"{'x' * 256}.txt"):
            r = _post(client, name)
            assert r.status_code == 422, f"{name!r} -> {r.status_code}: {r.text}"
        # and the parser was never reached for any of them
        assert _post(client, "trailing.txt ").status_code == 422


def test_conversation_title_rejects_control_characters_422(client, db_session):
    from app.services import users

    users.create_user(db_session, "title-hygiene", password="a valid longer passphrase")
    db_session.commit()
    r = client.post(
        "/api/auth/login",
        json={"username": "title-hygiene", "password": "a valid longer passphrase"},
    )
    csrf = r.json()["csrf_token"]

    r = client.post(
        "/api/conversations",
        json={"title": "bad\x00title"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422, r.text

    r = client.post(
        "/api/conversations",
        json={"title": "x" * 201},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422, r.text

    # control characters cannot sneak in via rename either
    conv = client.post(
        "/api/conversations", json={}, headers={"X-CSRF-Token": csrf}
    ).json()
    r = client.patch(
        f"/api/conversations/{conv['id']}",
        json={"title": "nul\x00again"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422, r.text
