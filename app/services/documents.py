"""Document lifecycle: upload → index → (maybe) delete (§A7, §A9.1).

Upload: streamed to disk in chunks with a hard byte cap (MAX_UPLOAD_MB →
413) so an oversized or lying-Content-Length body can't exhaust memory.
Checksum (sha256) computed while streaming; files live under
uploads/<doc_id>/<display_name> — no more filename collisions.

Delete consistency (§A9.1): vectors first, DB rows last. TX1 marks the row
'deleting'; the (idempotent, retryable) Pinecone delete-by-doc_id runs
outside any transaction; TX2 removes the row + files. A Pinecone outage
leaves the row in 'deleting' for the sweep (manual CLI; startup hook when
B6 wires the delete endpoints).
"""
import hashlib
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.core.config import get_settings
from app.models.document import Document
from app.services.audit_events import record_event

logger = logging.getLogger(__name__)


class UploadTooLarge(Exception):
    """Raised mid-stream when the byte cap is exceeded (maps to 413)."""


def create_document(
    db: DBSession,
    *,
    scope: str,
    display_name: str,
    storage_path: str,
    checksum: str,
    size_bytes: int,
    mime_suffix: str | None,
    owner_id: UUID | None = None,
) -> Document:
    doc = Document(
        scope=scope,
        owner_id=owner_id,
        display_name=display_name,
        storage_path=storage_path,
        checksum=checksum,
        size_bytes=size_bytes,
        mime_suffix=mime_suffix,
        status="pending",
    )
    db.add(doc)
    db.flush()
    return doc


def get_document(db: DBSession, doc_id: UUID) -> Document | None:
    return db.get(Document, doc_id)


def attach_ingest_result(db: DBSession, doc: Document, chunk_count: int) -> None:
    doc.status = "indexed"
    doc.chunk_count = chunk_count
    doc.indexed_at = datetime.now(timezone.utc)
    db.flush()


def mark_failed(db: DBSession, doc: Document, error_class: str) -> None:
    """Failure CLASSES only — never chunk text or exception bodies (§A6)."""
    doc.status = "failed"
    doc.error = error_class
    db.flush()


def save_upload_stream(fileobj, *, dest_dir: Path, display_name: str) -> tuple[Path, int, str]:
    """Stream the upload to ``dest_dir/<display_name>`` under the byte cap.

    Enforces MAX_UPLOAD_MB while streaming (a lying Content-Length can't
    blow past it); removes the partial file on any failure. Returns
    (path, size_bytes, sha256 checksum).
    """
    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / display_name
    digest = hashlib.sha256()
    size = 0
    try:
        with dest.open("wb") as out:
            while chunk := fileobj.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(
                        f"upload exceeds MAX_UPLOAD_MB={get_settings().max_upload_mb}"
                    )
                digest.update(chunk)
                out.write(chunk)
    except Exception:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise
    return dest, size, digest.hexdigest()


def begin_delete(db: DBSession, doc: Document) -> None:
    """TX1: mark 'deleting' (row stays queryable, hidden from listings)."""
    doc.status = "deleting"
    db.flush()


def complete_delete(db: DBSession, doc_id: UUID, *, actor_user_id=None) -> None:
    """TX2: vectors are already gone — remove the row and the stored file."""
    doc = db.get(Document, doc_id)
    if doc is not None:
        storage = Path(doc.storage_path)
        db.delete(doc)
        record_event(
            db, "document_deleted", actor_user_id=actor_user_id,
            object_type="document", object_id=doc_id, outcome="ok",
            details={"scope": doc.scope, "checksum": doc.checksum},
        )
        db.flush()
        if doc.storage_path:
            # Defense-in-depth (2026-11-05 incident): storage.parent must be
            # a real uploads/<doc_id>/ directory. A malformed storage_path
            # (e.g. a bare relative name whose parent resolves to "." — the
            # process CWD) must never be rmtree'd: skip with an error and
            # let the DB row delete proceed.
            allowed_root = Path(get_settings().upload_dir).resolve()
            target = storage.parent.resolve()
            if target == allowed_root or allowed_root not in target.parents:
                logger.error(
                    "complete_delete refusing to rmtree %s — not under upload_dir %s",
                    target, allowed_root,
                )
            else:
                shutil.rmtree(target, ignore_errors=True)


def list_stuck_deletes(db: DBSession) -> list[Document]:
    return list(
        db.execute(select(Document).where(Document.status == "deleting")).scalars().all()
    )


def retry_stuck_deletes(db: DBSession, delete_vectors, *, actor_user_id=None) -> int:
    """Sweep (§A9.1): resume rows stuck in 'deleting' — the failure mode is
    a Pinecone outage and the retry is the same idempotent delete-by-doc_id.
    Returns the number of documents completed."""
    completed = 0
    for doc in list_stuck_deletes(db):
        delete_vectors(str(doc.id))  # idempotent; raises through on outage
        complete_delete(db, doc.id, actor_user_id=actor_user_id)
        completed += 1
    db.commit()
    return completed
