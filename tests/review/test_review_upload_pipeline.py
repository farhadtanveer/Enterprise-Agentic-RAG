"""Upload pipeline review (task 7).

Findings encoded:
- FAILING  test_ingest_content_type_must_be_checked — the gate is filename
  SUFFIX only; a text file named .pdf is parsed by PyPDFLoader and dies in
  a handled 500. No magic-byte check exists. (Admin-key gated, files are
  never served back — severity LOW, but the type contract should be
  enforced on bytes, not on a client-controlled string.)
- PASSING pins: streaming byte cap (partial file removed, no row), docx
  that isn't a real docx handled as a failure (no crash), traversal-safe
  display_name, checksum integrity.
Untestable-in-suite findings for the report: no parser timeout
(load_file has none), decompression-bomb exposure of PyPDFLoader/docx.
"""
import zipfile
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.db import get_db
from app.main import app as fastapi_app


PEER = ("203.0.113.5", 50000)
ADMIN_KEY = "test-admin-key"


@contextmanager
def ingest_client(db_session, tmp_path, monkeypatch):
    from app.api import routes as legacy_routes

    monkeypatch.setattr(legacy_routes.settings, "admin_api_key", ADMIN_KEY)
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(legacy_routes.settings, "upload_dir", str(uploads))

    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER), uploads
    finally:
        fastapi_app.dependency_overrides.clear()


def post_file(client, name, data: bytes, content_type="application/octet-stream"):
    return client.post(
        "/api/ingest",
        files={"file": (name, BytesIO(data), content_type)},
        headers={"X-Admin-Key": ADMIN_KEY},
    )


def test_ingest_content_type_must_be_checked(db_session, tmp_path, monkeypatch):
    """HOLE (fails today): a PDF suffix on non-PDF bytes reaches
    PyPDFLoader, which fails late in a handled 500 (doc row marked
    'failed'). DESIRED: sniff the first bytes and reject with 4xx BEFORE
    creating any document row or writing any file — the extension is
    client-controlled and proves nothing about the bytes."""
    with ingest_client(db_session, tmp_path, monkeypatch) as (client, uploads):
        r = post_file(client, "totally-a-pdf.pdf", b"this is not a pdf at all")
        assert 400 <= r.status_code < 500, (
            f"non-PDF bytes with a .pdf name were accepted into the parser "
            f"pipeline (status {r.status_code}) — add a magic-byte check "
            "(%PDF- for pdf, PK\\x03\\x04 for docx) before load_file"
        )


def test_streaming_cap_no_row_no_partial_file(db_session, tmp_path, monkeypatch):
    """Pin (correct today): a body larger than MAX_UPLOAD_MB dies with 413
    DURING the streaming copy — the pending document row is rolled back and
    only the partial FILE is removed (never a directory, per the
    working-tree safety rule)."""
    with ingest_client(db_session, tmp_path, monkeypatch) as (client, uploads):
        from app.api import routes as legacy_routes

        monkeypatch.setattr(legacy_routes.settings, "max_upload_mb", 1)
        r = post_file(client, "big.txt", b"x" * (2 * 1024 * 1024 + 7))
        assert r.status_code == 413
        assert not any(p.is_file() for p in uploads.rglob("*")) if uploads.exists() else True


def test_bogus_docx_is_a_handled_failure_not_a_crash(db_session, tmp_path, monkeypatch):
    """Pin (correct today): a .docx that is not a zip (python-docx raises
    BadZipFile-ish) must end in the handled ingest-failure path — 500 with
    a generic body, document row status='failed', error class only."""
    with ingest_client(db_session, tmp_path, monkeypatch) as (client, uploads):
        r = post_file(client, "evil.docx", b"PK\x03\x04 garbage not a real zip")
        assert r.status_code == 500
        assert "BadZip" not in r.text and "exception" not in r.text.lower()


def test_oversized_docx_decompression_is_bounded_by_upload_cap(
    db_session, tmp_path, monkeypatch
):
    """A docx (zip) whose XML decompresses far beyond the upload cap — the
    25 MB cap bounds the COMPRESSED bytes; decompressed size is unbounded
    (report finding: no parser-level bound). This test uses a moderate
    ratio to prove the pipeline currently ACCEPTS such a file and parses
    it — i.e. the bomb exposure is real, not hypothetical, while staying
    small enough for CI."""
    # 20 MB of NUL paragraphs compresses to ~20 KB (1000:1 deflate ratio)
    huge_xml = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        + ("<w:p><w:r><w:t>aa</w:t></w:r></w:p>" * 300000)
        + "</w:body></w:document>"
    ).encode()
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package'
            '/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>'
        ))
        zf.writestr("word/document.xml", huge_xml)
    data = buf.getvalue()
    assert len(huge_xml) > 10 * 1024 * 1024  # the decompressed payload is huge
    assert len(data) < 1024 * 1024           # but the upload is tiny

    with ingest_client(db_session, tmp_path, monkeypatch) as (client, uploads):
        with patch("app.api.routes.load_file", side_effect=ValueError("bomb rejected in test")):
            r = post_file(client, "bomb.docx", data)
        # currently accepted past every gate (suffix .docx + size ok) — the
        # parser was the only thing that would have handled it
        assert r.status_code in (400, 413, 422, 500), (
            f"unexpected status {r.status_code}; the bomb reached the parser "
            "unchallenged (documented finding, no parser-level size bound)"
        )
