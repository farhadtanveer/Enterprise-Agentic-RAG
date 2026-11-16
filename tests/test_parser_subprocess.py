"""A3 (docs/tasks/g2.md, DECISION #8): parsing runs in a SUBPROCESS with a
hard timeout and a memory limit; pages, extracted characters and the docx
uncompressed size are capped (zip-bomb guard); a global ingest gate caps
simultaneous parse/embed runs so uploads cannot starve chat.

Failure contract (S2 at scale): ANY parse failure — timeout, killed child,
cap refusal, corrupt file — surfaces as ParseFailed and the routes map it
to the GENERIC 422 "File could not be parsed" (never 500, never parser
text). The app process outlives every child failure.

The parent/child machinery is exercised for real (spawned processes); the
route-level tests patch the seam the way the rest of the offline suite
does.
"""
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.api import auth as auth_api
from app.api import conversations as conv_api
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.main import app as fastapi_app
from app.services import users as users_svc
from app.services.ingestion import ParseFailed, _spawn_and_collect, load_file
from app.services.ratelimit import SlidingWindowLimiter

PEER = ("203.0.113.13", 50000)
PASSWORD = "a valid longer passphrase"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    monkeypatch.setattr(auth_api, "login_limiter", SlidingWindowLimiter(10**9))
    monkeypatch.setattr(conv_api, "chat_limiter", SlidingWindowLimiter(10**9))


# --- the parent/child machinery (real spawned processes) ------------------------
# Spawn targets MUST be module-level (pickled by module+qualname reference);
# settings that must reach the CHILD are set as ENV VARS (monkeypatching the
# parent's cached Settings object cannot cross the process boundary).


def _hang_target(conn):  # the poisoned-file stand-in: never answers
    time.sleep(60)


def _crash_target(conn):  # a parser segfault/abort
    import os

    os._exit(2)


def _bomb_target(conn):  # allocates far beyond the child's RLIMIT_AS
    _b = bytearray(600 * 1024 * 1024)
    conn.send(("ok", []))


def test_load_file_round_trips_a_real_file_through_a_subprocess(tmp_path):
    f = tmp_path / "real.txt"
    f.write_text("subprocess parse round trip", encoding="utf-8")
    docs = load_file(f)
    assert len(docs) == 1
    assert "subprocess parse round trip" in docs[0].page_content


def test_hanging_child_hits_the_hard_timeout():
    started = time.monotonic()
    with pytest.raises(ParseFailed) as ei:
        _spawn_and_collect(_hang_target, (), timeout_s=1)
    assert ei.value.reason == "timeout"
    assert time.monotonic() - started < 30  # the PARENT did not wait 60s


def test_crashing_child_maps_to_generic_parse_failed():
    with pytest.raises(ParseFailed) as ei:
        _spawn_and_collect(_crash_target, (), timeout_s=30)
    assert ei.value.reason == "crashed"


def test_memory_limit_kills_the_bomber_but_not_the_app():
    import importlib.util

    if importlib.util.find_spec("resource") is None:
        pytest.skip("memory limit needs the resource module (POSIX)")

    with pytest.raises(ParseFailed) as ei:
        _spawn_and_collect(_bomb_target, (), timeout_s=60, memory_limit_mb=64)
    assert ei.value.reason in ("crashed", "timeout")


# --- the in-child caps (real files, real subprocess, env-crossed settings) ------


def test_docx_zip_bomb_guard_refuses_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("PARSER_MAX_DOCX_UNCOMPRESSED_MB", "1")  # env crosses to the child
    f = tmp_path / "bomb.docx"
    with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b"\x00" * (3 * 1024 * 1024))  # 3MB uncompressed
    with pytest.raises(ParseFailed) as ei:
        load_file(f)
    assert ei.value.reason == "caps"


def test_extracted_characters_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("PARSER_MAX_CHARS", "1000")
    f = tmp_path / "big.txt"
    f.write_text("x" * 50_000, encoding="utf-8")
    docs = load_file(f)
    assert sum(len(d.page_content) for d in docs) <= 1000


def test_pdf_pages_are_capped(tmp_path, monkeypatch):
    from pypdf import PdfWriter

    monkeypatch.setenv("PARSER_MAX_PAGES", "5")
    f = tmp_path / "many.pdf"
    w = PdfWriter()
    for _ in range(20):
        w.add_blank_page(width=200, height=200)
    with f.open("wb") as fh:
        w.write(fh)
    docs = load_file(f)
    assert len(docs) <= 5


def test_corrupt_docx_is_a_generic_parse_failure(tmp_path):
    f = tmp_path / "junk.docx"
    f.write_bytes(b"PK\x03\x04 definitely not a real zip")  # passes the F6 magic gate
    with pytest.raises(ParseFailed):
        load_file(f)


# --- route level: ParseFailed is the generic 422, app process alive -------------


@pytest.fixture()
def client(db_session):
    def _override():
        yield db_session

    fastapi_app.dependency_overrides[get_db] = _override
    try:
        yield TestClient(fastapi_app, client=PEER)
    finally:
        fastapi_app.dependency_overrides.clear()


def _user(db, name):
    u = users_svc.create_user(db, name, password=PASSWORD)
    db.commit()
    return u


def _login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def test_route_maps_parse_failure_to_generic_422(client, db_session, monkeypatch, tmp_path):
    import app.api.documents as doc_api

    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(
        doc_api, "load_file", lambda p: (_ for _ in ()).throw(ParseFailed("timeout"))
    )
    _user(db_session, "parse-fail-user")
    csrf = _login(client, "parse-fail-user")
    r = client.post(
        "/api/documents",
        files={"file": ("doc.txt", b"hello", "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "File could not be parsed"


# --- the ingest gate: parallel uploads cannot oversubscribe ---------------------


def test_ingest_gate_serializes_slow_parses(client, db_session, monkeypatch, tmp_path):
    import app.api.documents as doc_api
    from app.services.ingestion import IngestGate
    from langchain_core.documents import Document as LCDoc
    from sqlalchemy.orm import Session as DBSession

    # parallel requests need per-request DB sessions — the shared fixture
    # session cannot run three commits concurrently
    def _fresh_session():
        yield DBSession(bind=db_session.get_bind(), expire_on_commit=False)

    fastapi_app.dependency_overrides[get_db] = _fresh_session
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path / "uploads"))
    monkeypatch.setattr(doc_api, "ingest_gate", IngestGate(1))
    monkeypatch.setattr(doc_api, "add_documents", lambda chunks, **kw: ["id"])

    state = {"cur": 0, "max": 0}
    lock = threading.Lock()

    def slow_parse(path):
        with lock:
            state["cur"] += 1
            state["max"] = max(state["max"], state["cur"])
        time.sleep(0.3)
        with lock:
            state["cur"] -= 1
        return [LCDoc(page_content="x", metadata={})]

    monkeypatch.setattr(doc_api, "load_file", slow_parse)

    _user(db_session, "gate-user")
    setup = TestClient(fastapi_app, client=PEER)
    r = setup.post("/api/auth/login",
                   json={"username": "gate-user", "password": PASSWORD})
    sid, csrf = r.cookies.get("sid"), r.json()["csrf_token"]

    def fire(i):
        c = TestClient(fastapi_app, client=PEER)
        c.cookies.set("sid", sid)
        c.cookies.set("csrf", csrf)
        return c.post(
            "/api/documents",
            files={"file": (f"g{i}.txt", b"body", "text/plain")},
            headers={"X-CSRF-Token": csrf},
        )

    with ThreadPoolExecutor(max_workers=3) as pool:
        rs = list(pool.map(fire, (0, 1, 2)))
    assert all(r.status_code == 201 for r in rs), [r.text for r in rs]
    assert state["max"] == 1, f"gate failed — {state['max']} parses ran at once"


def test_large_verdict_does_not_deadlock_the_pipe(tmp_path, monkeypatch):
    """FOUND LIVE by g2a.sh (2026-11-16): a verdict larger than the pipe
    buffer (~64KB) deadlocked the old join-first parent — the child blocked
    in send() forever while the parent waited in join(), surfacing as a
    60s timeout and a bogus 422 on every real-size upload. The parent must
    drain the pipe WHILE the child runs. Regression: a 200KB extraction
    must parse."""
    monkeypatch.setenv("PARSER_MAX_CHARS", "2000000")  # default — no truncation
    f = tmp_path / "large.txt"
    f.write_text("x" * 200_000, encoding="utf-8")
    docs = load_file(f)
    assert sum(len(d.page_content) for d in docs) == 200_000
