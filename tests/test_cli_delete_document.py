"""H10 (docs/tasks/g2.md): the operator CLI ``delete-document <id>``.

Contract (§A9.1, mirrored from the admin delete route):
- vectors FIRST, then the row + stored file;
- IDEMPOTENT: an unknown/already-deleted id is a successful no-op, a
  Pinecone outage leaves the row 'deleting' for the sweep;
- AUDITED: document_deleted with actor NULL (CLI actor).

Files: every path in these tests lives under a per-test ``tmp_path``
upload_dir (never settings defaults) — the working-tree safety rule.
"""
import pytest
from click.testing import CliRunner
from pathlib import Path

from sqlalchemy import select

from app.cli import cli
from app.models.document import Document
from app.services import documents as docs_service


def _row_count(db, doc_id) -> int:
    """Fresh query — the caller's identity map may still hold the deleted
    instance (expire+get would raise ObjectDeletedError)."""
    return len(
        db.execute(select(Document).where(Document.id == doc_id)).scalars().all()
    )


@pytest.fixture()
def cli_env(joined_session_factory, monkeypatch, tmp_path):
    """CLI session factory + a tmp_path upload_dir + a vector-delete spy."""
    from app.core.config import get_settings

    monkeypatch.setattr("app.cli._session", joined_session_factory)
    monkeypatch.setattr(get_settings(), "upload_dir", str(tmp_path))
    calls = {"vectors": [], "order": []}

    def fake_delete_vectors(doc_id: str) -> None:
        # called with the LIVE db session of the CLI command
        calls["vectors"].append(doc_id)
        calls["order"].append("vectors")

    monkeypatch.setattr("app.rag.vectorstore.delete_document_vectors", fake_delete_vectors)
    return {"tmp_path": tmp_path, "calls": calls, "factory": joined_session_factory}


def _make_doc(db, upload_dir: Path, *, scope="org", owner_id=None) -> Document:
    doc = docs_service.create_document(
        db, scope=scope, owner_id=owner_id, display_name="h10-note.txt",
        storage_path="", checksum="deadbeef", size_bytes=10, mime_suffix=".txt",
    )
    dest_dir = upload_dir / str(doc.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = dest_dir / f"{doc.id}.txt"
    stored.write_text("h10 note", encoding="utf-8")
    doc.storage_path = str(stored)
    db.commit()
    return doc


def run_cli(document_id: str):
    return CliRunner().invoke(cli, ["delete-document", document_id])


def test_delete_document_removes_vectors_then_row_then_file(cli_env, db_session):
    doc = _make_doc(db_session, cli_env["tmp_path"])
    doc_id = str(doc.id)

    result = run_cli(doc_id)
    assert result.exit_code == 0, result.output
    assert "Deleted" in result.output

    # the vector delete ran for EXACTLY this doc id, while the row still existed
    assert cli_env["calls"]["vectors"] == [doc_id]

    db_session.expire_all()  # doc.id is now unusable — use the captured doc_id
    assert _row_count(db_session, doc_id) == 0  # row gone (fresh read)
    assert not (cli_env["tmp_path"] / doc_id / f"{doc_id}.txt").exists()  # file gone


def test_delete_document_vectors_are_deleted_BEFORE_the_row(cli_env, db_session, monkeypatch):
    """§A9.1 ordering: at vector-delete time the row must still exist
    (already marked 'deleting' by TX1, committed)."""
    doc = _make_doc(db_session, cli_env["tmp_path"])

    real_fake = cli_env["calls"]

    def spying_delete(doc_id: str) -> None:
        db_session.expire_all()
        row = db_session.get(Document, doc.id)
        assert row is not None, "row vanished before the vector delete (§A9.1 violation)"
        assert row.status == "deleting", f"row status {row.status!r} at vector-delete time"
        real_fake["order"].append("vectors")

    monkeypatch.setattr("app.rag.vectorstore.delete_document_vectors", spying_delete)
    result = run_cli(str(doc.id))
    assert result.exit_code == 0, result.output


def test_delete_document_is_idempotent(cli_env, db_session):
    doc = _make_doc(db_session, cli_env["tmp_path"])
    first = run_cli(str(doc.id))
    assert first.exit_code == 0, first.output

    second = run_cli(str(doc.id))  # unknown NOW — still exit 0, "nothing to do"
    assert second.exit_code == 0, second.output
    assert "nothing to do" in second.output
    assert cli_env["calls"]["vectors"] == [str(doc.id)]  # vectors touched ONCE


def test_delete_document_unknown_id_is_a_clean_noop(cli_env):
    result = run_cli("00000000-0000-0000-0000-000000000000")
    assert result.exit_code == 0
    assert "nothing to do" in result.output
    assert cli_env["calls"]["vectors"] == []


def test_delete_document_rejects_non_uuid_input(cli_env):
    result = run_cli("not-a-uuid")
    assert result.exit_code != 0
    assert "Not a document id" in result.output
    assert cli_env["calls"]["vectors"] == []


def test_delete_document_writes_document_deleted_audit(cli_env, db_session):
    from sqlalchemy import select
    from app.models.audit_event import AuditEvent

    doc = _make_doc(db_session, cli_env["tmp_path"])
    result = run_cli(str(doc.id))
    assert result.exit_code == 0, result.output

    events = list(
        db_session.execute(
            select(AuditEvent).where(AuditEvent.event_type == "document_deleted")
        ).scalars().all()
    )
    assert len(events) == 1
    ev = events[0]
    assert str(ev.object_id) == str(doc.id)
    assert ev.actor_user_id is None  # CLI actor
    assert ev.outcome == "ok"
    assert ev.details.get("scope") == "org"
    # no filenames, no content in the audit details
    assert "h10-note" not in str(ev.details)


def test_delete_document_on_a_row_stuck_in_deleting_completes(cli_env, db_session):
    """The Pinecone-outage shape: TX1 committed, sweep never ran — the CLI
    completes the deletion (idempotent vector delete + TX2)."""
    doc = _make_doc(db_session, cli_env["tmp_path"])
    doc_id = str(doc.id)
    docs_service.begin_delete(db_session, doc)
    db_session.commit()

    result = run_cli(doc_id)
    assert result.exit_code == 0, result.output
    db_session.expire_all()
    assert _row_count(db_session, doc_id) == 0
