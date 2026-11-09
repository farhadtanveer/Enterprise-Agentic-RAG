"""Destructive-path review (review task 1): every file-removal path fed
from DB values or user input.

House rule after the 2026-11-05 working-tree incident: code may delete a
SINGLE FILE whose resolved path is inside the configured upload_dir — never
a directory, never a path that resolves outside it (symlinks included).

Findings encoded here:
- FAILING  test_complete_delete_must_not_follow_symlink_out_of_doc_dir —
  complete_delete resolves() a storage_path symlink and unlinks whatever it
  points at; a symlink that lands on ANOTHER file under upload_dir is
  accepted by the guard, deleting the wrong file.
- FAILING  test_ingest_nul_filename_is_client_error_not_500 — a filename
  with an embedded NUL escapes save_upload_stream's cleanup (the except
  clause only catches OSError) and surfaces as an unhandled 500.
Pins (expected-PASS) cover the behavior that is already correct.
"""
import logging
from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.services import documents as docs_service

PEER = ("203.0.113.5", 50000)
ADMIN_KEY = "test-admin-key"


@contextmanager
def make_client(db_session, *, raise_server_exceptions=True):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(
            fastapi_app, client=PEER, raise_server_exceptions=raise_server_exceptions
        )
    finally:
        fastapi_app.dependency_overrides.clear()


def upload(client, tmp_path, name, content=b"hello world"):
    p = tmp_path / "src.bin"
    p.write_bytes(content)
    with p.open("rb") as fh:
        return client.post(
            "/api/ingest",
            files={"file": (name, fh, "application/octet-stream")},
            headers={"X-Admin-Key": ADMIN_KEY},
        )


# --- complete_delete -------------------------------------------------------------


def test_complete_delete_must_not_follow_symlink_out_of_doc_dir(
    db_session, tmp_path, monkeypatch
):
    """HOLE (fails today): the resolved-path guard accepts any target under
    upload_dir — including one reached by following a symlink planted as
    storage_path. Deleting document A then removes document B's file when a
    symlink points across doc dirs. The unlink must refuse symlinked paths
    (e.g. check target.is_symlink() / use O_NOFOLLOW semantics)."""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    root = tmp_path / "uploads"

    victim_dir = root / "doc-b"
    victim_dir.mkdir(parents=True)
    victim_file = victim_dir / "victim.txt"
    victim_file.write_text("keep me")

    attacker_dir = root / "doc-a"
    attacker_dir.mkdir(parents=True)
    link = attacker_dir / "link.txt"
    link.symlink_to(victim_file)

    doc = docs_service.create_document(
        db_session, scope="org", owner_id=None, display_name="link.txt",
        storage_path=str(link), checksum="a" * 64, size_bytes=1, mime_suffix=".txt",
    )
    db_session.commit()

    docs_service.complete_delete(db_session, doc.id)
    db_session.commit()

    # DESIRED: the symlinked-to victim file must survive a delete that was
    # authorized for doc-a only.
    assert victim_file.exists(), (
        "complete_delete followed a symlink out of the document's own "
        "directory and deleted another document's file"
    )


def test_complete_delete_refuses_directory_and_root_paths(
    db_session, tmp_path, monkeypatch, caplog
):
    """Pin (correct today): storage_path pointing at a DIRECTORY (the
    per-doc dir, or upload_dir itself) never deletes the directory — the
    guard's is_file() check refuses it. Regression guard for the incident
    rule; complete_delete is not reachable from any API yet (B6), but the
    sweep CLI uses it."""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    root = tmp_path / "uploads"
    doc_dir = root / "doc-1"
    doc_dir.mkdir(parents=True)
    (doc_dir / "keep.txt").write_text("content")

    for dangerous in (str(doc_dir), str(root), "uploads", ""):
        doc = docs_service.create_document(
            db_session, scope="org", owner_id=None, display_name="d.txt",
            storage_path=dangerous, checksum="c" * 64, size_bytes=1, mime_suffix=".txt",
        )
        db_session.commit()
        with caplog.at_level(logging.ERROR):
            docs_service.complete_delete(db_session, doc.id)
            db_session.commit()
        assert doc_dir.exists(), f"directory deleted via storage_path={dangerous!r}"
        assert (doc_dir / "keep.txt").exists()


def test_complete_delete_refuses_traversal_and_unc_paths(
    db_session, tmp_path, monkeypatch
):
    """Pin (correct today): traversal (..), absolute, drive-letter and UNC
    storage paths all resolve OUTSIDE upload_dir and are refused — the row
    goes, no file outside uploads is touched."""
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    outside = tmp_path / "outside.txt"
    outside.write_text("do not delete")

    candidates = [
        str(tmp_path / "outside.txt"),                 # absolute, outside
        str(tmp_path / "uploads" / ".." / "outside.txt"),  # traversal out
        "C:\\Windows\\system32\\evil.txt",             # drive letter (parsed on POSIX as relative name)
        "\\\\server\\share\\evil.txt",                 # UNC
        "/etc/passwd",                                 # absolute POSIX
    ]
    for dangerous in candidates:
        doc = docs_service.create_document(
            db_session, scope="org", owner_id=None, display_name="x.txt",
            storage_path=dangerous, checksum="b" * 64, size_bytes=1, mime_suffix=".txt",
        )
        db_session.commit()
        docs_service.complete_delete(db_session, doc.id)
        db_session.commit()
        assert docs_service.get_document(db_session, doc.id) is None  # row went
    assert outside.exists()


# --- upload filename sanitation ---------------------------------------------------


def test_ingest_traversal_filename_is_sanitized(db_session, tmp_path, monkeypatch):
    """Pin (correct today): Path(...).name strips directory components, so
    "../../evil.txt" lands as evil.txt inside uploads/<doc_id>/ — nothing
    is written outside the configured upload_dir."""
    from app.api import routes as legacy_routes

    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", ADMIN_KEY)
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(uploads))

    from unittest.mock import patch

    class C:
        page_content = "c"
        metadata = {"start_index": "0"}

    with make_client(db_session) as client, \
         patch.object(legacy_routes, "load_file", return_value=[C()]), \
         patch.object(legacy_routes, "chunk_documents", return_value=[C()]), \
         patch.object(legacy_routes, "add_documents", return_value=["id1"]):
        r = upload(client, tmp_path, "../../evil.txt")

    assert r.status_code == 200, r.text
    doc_id = r.json()["document_id"]
    stored = uploads / doc_id / "evil.txt"
    assert stored.is_file()
    # nothing landed outside upload_dir
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "evil.txt").exists()


def test_ingest_raw_nul_filename_is_client_error_not_500(db_session, tmp_path, monkeypatch):
    """HOLE (fails today): a multipart filename with a RAW NUL byte (what a
    raw-socket/curl --form client can send; httpx percent-encodes it, so the
    multipart body is built manually here) passes the suffix check and then
    crashes the documents INSERT — psycopg raises DataError ('text fields
    cannot contain NUL') straight out of create_document's flush, an
    unhandled exception → raw framework 500. A client-controlled string
    must map to a 4xx (reject control characters in filenames), never an
    unhandled 500."""
    from app.api import routes as legacy_routes

    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", ADMIN_KEY)
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(tmp_path / "uploads"))

    boundary = "review-boundary-123"
    part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="bad\x00name.txt"\r\n'
        "Content-Type: text/plain\r\n\r\n"
        "content\r\n"
        f"--{boundary}--\r\n"
    ).encode("latin-1", errors="ignore")
    headers = {
        "X-Admin-Key": ADMIN_KEY,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }

    with make_client(db_session, raise_server_exceptions=False) as client:
        r = client.post("/api/ingest", content=part, headers=headers)
    assert 400 <= r.status_code < 500, (
        f"raw-NUL filename produced status {r.status_code} (unhandled server "
        "error) — validate/sanitize the filename before the DB insert and "
        "the open(), mapping control characters to a 4xx"
    )
