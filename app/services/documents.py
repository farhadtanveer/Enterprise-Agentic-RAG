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
import os
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


class UploadRejected(Exception):
    """Raised when the stored name or an existing path is unsafe (maps to 422)."""


def save_upload_stream(
    fileobj, *, dest_dir: Path, storage_name: str, chunk_validator=None
) -> tuple[Path, int, str]:
    """Stream the upload to ``dest_dir/<storage_name>`` under the byte cap.

    F1: the stored name is SERVER-GENERATED (the route/CLI pass
    ``f"{doc_id}{suffix}"``) — a client-supplied filename never becomes a
    filesystem path component. ``storage_name`` must be a pure basename;
    the destination must not already exist (exclusive create, so a
    pre-planted symlink or file can never be written through).

    F6: ``chunk_validator`` (optional) inspects every chunk as it streams —
    e.g. the text NUL/UTF-8 contract — so rejected content never lands.

    Enforces MAX_UPLOAD_MB while streaming (a lying Content-Length can't
    blow past it); removes the partial file on any failure. Returns
    (path, size_bytes, sha256 checksum).
    """
    if (
        not storage_name
        or storage_name in (".", "..")
        or "/" in storage_name
        or "\\" in storage_name  # reject Windows separators on every OS
        or "\x00" in storage_name
        or os.sep in storage_name
        or (os.altsep and os.altsep in storage_name)
        or Path(storage_name).name != storage_name
    ):
        raise UploadRejected(f"storage_name must be a plain basename, got {storage_name!r}")
    max_bytes = get_settings().max_upload_mb * 1024 * 1024
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / storage_name
    if dest.is_symlink() or dest.exists():
        # exclusive-create refuses a pre-planted file/symlink outright
        raise UploadRejected(f"destination already exists: {dest.name}")
    digest = hashlib.sha256()
    size = 0
    try:
        with dest.open("xb") as out:
            while chunk := fileobj.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise UploadTooLarge(
                        f"upload exceeds MAX_UPLOAD_MB={get_settings().max_upload_mb}"
                    )
                if chunk_validator is not None:
                    chunk_validator(chunk)
                digest.update(chunk)
                out.write(chunk)
            if chunk_validator is not None:
                chunk_validator(b"")  # flush any pending incremental decoder state
    except Exception:
        # working-tree safety rule: on failure remove only the PARTIAL FILE
        # this call created — never a directory. The (now empty) per-document
        # directory stays; pruning empty dirs is an operator action.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove partial upload %s", dest)
        raise
    return dest, size, digest.hexdigest()


def begin_delete(db: DBSession, doc: Document) -> None:
    """TX1: mark 'deleting' (row stays queryable, hidden from listings)."""
    doc.status = "deleting"
    db.flush()


def remove_stored_file(storage_path: str | None) -> bool:
    """F1-hardened single-file removal (the ONLY way any code deletes a
    stored document file). Returns True when the file was unlinked.

    Working-tree safety rule (2026-11-05 incident), hardened by the
    security review (F1): code that deletes files may delete a SINGLE
    REGULAR FILE whose REAL path is inside the configured upload_dir —
    never a directory derived from DB values or settings defaults — and
    never a SYMLINK: if any component of the stored path (final name or an
    intermediate directory) is a symlink, the resolved real path differs
    from the plain absolute path and the unlink is refused. The
    per-document directory may remain (empty); pruning empty dirs is an
    operator action, not something DB-driven code does.
    """
    if not storage_path:
        return False
    try:
        storage = Path(storage_path)
        real = Path(os.path.realpath(storage))
        plain = Path(os.path.abspath(storage))
        allowed_root = Path(get_settings().upload_dir).resolve()
        if real != plain:
            logger.error(
                "remove_stored_file refusing symlinked storage path — "
                "resolves to %s",
                real.name,
            )
        elif real.is_symlink() or storage.is_symlink():
            logger.error("remove_stored_file refusing symlink final component")
        elif real.is_file() and allowed_root in real.parents:
            real.unlink(missing_ok=True)
            return True
        else:
            logger.error(
                "remove_stored_file refusing to unlink — not a regular "
                "file under upload_dir %s",
                allowed_root,
            )
    except OSError as exc:
        # F4 hygiene: class only — the row's storage_path is in the DB if
        # an operator needs the full picture
        logger.error("remove_stored_file could not remove the stored file: %s",
                     type(exc).__name__)
    return False


def private_documents_of(db: DBSession, user_id) -> list[Document]:
    """All private documents owned by the user (the §A9.1 cleanup set)."""
    return list(
        db.execute(
            select(Document).where(
                Document.scope == "private", Document.owner_id == user_id
            )
        ).scalars().all()
    )


def complete_delete(db: DBSession, doc_id: UUID, *, actor_user_id=None) -> None:
    """TX2: vectors are already gone — remove the row and the stored file."""
    doc = db.get(Document, doc_id)
    if doc is not None:
        db.delete(doc)
        record_event(
            db, "document_deleted", actor_user_id=actor_user_id,
            object_type="document", object_id=doc_id, outcome="ok",
            details={"scope": doc.scope, "checksum": doc.checksum},
        )
        db.flush()
        remove_stored_file(doc.storage_path)


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
