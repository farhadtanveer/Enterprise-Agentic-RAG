"""A4 (docs/tasks/g2.md): the F1/F3/F6 storage rules apply UNCHANGED on the
new user-facing upload surface — server-generated storage names (F1), name
hygiene on every stored string (F3), content-vs-type gate incl. streaming
text validation (F6), and the hardened single-file removal refusing
symlinks and anything outside upload_dir.

Every file operation in this file lives under a per-test tmp_path
upload_dir (working-tree safety rule). The behaviors under test EXIST
(inherited from the B4 flow through app/services/documents.py) — this file
proves the NEW route inherits them; nothing here weakens an existing test.
"""
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.models.document import Document
from app.services import users as users_svc
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.17", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    import app.api.documents as doc_api
    from langchain_core.documents import Document as LCDoc

    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(doc_api, "load_file",
                        lambda p: [LCDoc(page_content="x", metadata={"source": str(p)})])
    monkeypatch.setattr(doc_api, "add_documents", lambda chunks, **kw: ["id"])
    monkeypatch.setattr(doc_api, "upload_limiter", SlidingWindowLimiter(10**9, window_seconds=3600.0))
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    return tmp_path / "uploads"


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER)
    finally:
        fastapi_app.dependency_overrides.clear()


def _user_and_csrf(client, db, name):
    users_svc.create_user(db, name, password=PASSWORD)
    db.commit()
    r = client.post("/api/auth/login", json={"username": name, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def _upload(client, csrf, name="innocent-note.txt", content=b"body text"):
    return client.post(
        "/api/documents",
        files={"file": (name, content, "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )


def test_f1_stored_name_is_server_generated_and_client_name_never_appears(client, db_session, tmp_path):
    upload_root = tmp_path / "uploads"
    csrf = _user_and_csrf(client, db_session, "f1-user")
    r = _upload(client, csrf, name="my secret resume FINAL(v2).txt")
    assert r.status_code == 201, r.text
    doc = db_session.execute(select(Document)).scalar_one()
    stored = Path(doc.storage_path)
    rel = stored.relative_to(upload_root)
    # exactly upload_dir/<doc_id>/<doc_id><suffix> — nothing client-supplied
    assert len(rel.parts) == 2
    assert rel.parts[0] == str(doc.id)
    assert rel.parts[1] == f"{doc.id}.txt"
    assert "resume" not in rel.as_posix().lower()
    assert stored.is_file()


def test_f6_rejected_content_leaves_no_file_on_disk(client, db_session, tmp_path):
    upload_root = tmp_path / "uploads"
    csrf = _user_and_csrf(client, db_session, "f6-user")

    r = _upload(client, csrf, name="binary.txt", content=b"\x00\x01binary")
    assert r.status_code == 422, r.text
    # the staging area and the whole upload root hold NOTHING from the rejected upload
    assert not (upload_root / ".incoming").exists() or \
        list((upload_root / ".incoming").iterdir()) == []

    r = _upload(client, csrf, name="fake.pdf", content=b"not really a pdf at all")
    assert r.status_code == 415, r.text
    files_left = [p for p in (upload_root.rglob("*") if upload_root.exists() else [])
                  if p.is_file()]
    # an EMPTY staging directory may remain (the working-tree rule leaves
    # empty-dir pruning to the operator) — but no FILE may survive
    assert files_left == [], f"rejected uploads must not leave files: {files_left}"


def test_f3_patch_display_name_hygiene(client, db_session):
    csrf = _user_and_csrf(client, db_session, "f3-user")
    r = _upload(client, csrf, name="ok.txt")
    doc_id = r.json()["document_id"]
    for bad in ("bad\x00name.txt", "bad|name.txt", "CON.txt"):
        r = client.patch(f"/api/documents/{doc_id}", json={"display_name": bad},
                         headers={"X-CSRF-Token": csrf})
        assert r.status_code == 422, (bad, r.status_code)


def test_delete_refuses_a_symlinked_storage_path_but_removes_the_row(client, db_session, tmp_path, monkeypatch):
    """F1 deletion rule on the new route: remove_stored_file refuses to
    unlink a SYMLINK (it could point anywhere) — the row is still deleted
    (the DB is authoritative) and the refusal is logged, never crashed."""
    import app.api.documents as doc_api

    monkeypatch.setattr(doc_api, "delete_document_vectors", lambda i: None)
    upload_root = tmp_path / "uploads"
    csrf = _user_and_csrf(client, db_session, "sym-del-user")
    r = _upload(client, csrf)
    doc_id = r.json()["document_id"]
    doc = db_session.execute(select(Document)).scalar_one()
    stored = Path(doc.storage_path)
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("do not touch", encoding="utf-8")

    stored.unlink()  # replace the real file with a symlink to OUTSIDE upload_dir
    os.symlink(outside, stored)

    r = client.delete(f"/api/documents/{doc_id}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert outside.exists()  # the symlink TARGET was never touched
    assert stored.is_symlink()  # the refusal left the symlink itself in place
    db_session.expire_all()
    assert db_session.execute(select(Document)).scalars().all() == []


def test_delete_refuses_a_symlinked_intermediate_directory(client, db_session, tmp_path, monkeypatch):
    import app.api.documents as doc_api

    monkeypatch.setattr(doc_api, "delete_document_vectors", lambda i: None)
    upload_root = tmp_path / "uploads"
    csrf = _user_and_csrf(client, db_session, "sym-dir-user")
    r = _upload(client, csrf)
    doc_id = r.json()["document_id"]
    doc = db_session.execute(select(Document)).scalar_one()
    stored = Path(doc.storage_path)
    doc_dir = stored.parent
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    outside_file = outside_dir / stored.name
    outside_file.write_text("trap", encoding="utf-8")

    stored.unlink()
    doc_dir.rmdir()
    os.symlink(outside_dir, doc_dir)  # symlinked INTERMEDIATE component

    r = client.delete(f"/api/documents/{doc_id}", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    assert outside_file.exists()  # nothing behind the symlinked dir was touched
    db_session.expire_all()
    assert db_session.execute(select(Document)).scalars().all() == []
