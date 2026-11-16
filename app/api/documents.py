"""User-facing documents surface (§A14.7, G2 A1) — org upload stays on
/api/admin/documents; this router serves private and group uploads plus the
audience-scoped meta listings.

Rules implemented here:
- 404-not-403: anything outside the caller's audience (AccessResolver
  can_read) is indistinguishable from nonexistent;
- management rights: private → owner, group → editor of THAT group, org →
  admin only; C3 keeps admin WRITE delete-any / org-only (an admin PATCH on
  a group document is 403 when they are a member, 404 when not);
- DECISION #3 (no moves in v1): PATCH refuses scope/group keys with 422;
- storage/ingest rules inherited unchanged from the B4 flow: F3 name
  hygiene, F6 content-vs-type gate, F1 server-generated stored names,
  §A9.1 vectors-first deletion;
- audit events carry ids and counters only — never the display name
  (DECISION #1: a filename is itself a potential leak).

Quota enforcement (§A14.6, DECISIONS #7/#8) lands with A2/A3 on top of
this route — the upload contract (201 + document_id) does not change.
"""
import logging
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_ as sa_and
from sqlalchemy import or_ as sa_or
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.models.document import Document
from app.models.group import Group, GroupMembership
from app.models.user import User
from app.rag.vectorstore import add_documents, delete_document_vectors
from app.services import documents as documents_service
from app.services.access import DbAccessResolver
from app.services.audit_events import record_event
from app.services.ingestion import (
    ContentRejected,
    chunk_documents,
    content_matches_suffix,
    load_file,
    make_text_validator,
    SUPPORTED,
)
from app.services.validation import display_name_problems

router = APIRouter(prefix="/api", tags=["documents"])
settings = get_settings()
logger = logging.getLogger(__name__)


class DocumentPatch(BaseModel):
    """DECISION #3: NO moves — scope/group keys are refused (extra=forbid
    makes any attempt a 422 before the handler even runs)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=200)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _meta(d: Document) -> dict:
    return {
        "id": str(d.id),
        "display_name": d.display_name,
        "scope": d.scope,
        "group_id": str(d.group_id) if d.group_id is not None else None,
        "uploaded_by": str(d.uploaded_by) if d.uploaded_by is not None else None,
        "status": d.status,
        "size_bytes": d.size_bytes,
        "chunk_count": d.chunk_count,
        "created_at": d.created_at.isoformat(),
    }


def _can_manage(user: User, doc: Document, resolver: DbAccessResolver) -> bool:
    if doc.scope == "private":
        return doc.owner_id == user.id
    if doc.scope == "group":
        return doc.group_id is not None and resolver.is_editor(user, doc.group_id)
    if doc.scope == "org":
        return user.role == "admin"
    return False  # unknown scope — fail closed


def _get_visible_or_404(db: DBSession, user: User, doc_id: UUID) -> Document:
    doc = documents_service.get_document(db, doc_id)
    if doc is None or not DbAccessResolver(db).can_read(user, doc):
        raise HTTPException(status_code=404, detail="Not found")
    return doc


@router.get("/documents")
def list_documents(
    response: Response,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    """Meta list: org + the caller's own private + their ACTIVE groups'
    documents. Rows outside the audience never leave the database here —
    the filter is server-side, not a post-filter."""
    group_ids = list(
        db.execute(
            select(GroupMembership.group_id)
            .join(Group, Group.id == GroupMembership.group_id)
            .where(GroupMembership.user_id == user.id, Group.is_active.is_(True))
        ).scalars().all()
    )
    q = (
        select(Document)
        .where(
            sa_or(
                Document.scope == "org",
                sa_and(Document.scope == "private", Document.owner_id == user.id),
                sa_and(Document.scope == "group", Document.group_id.in_(group_ids)),
            )
        )
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(q).scalars().all()
    _no_store(response)
    return {"items": [_meta(d) for d in rows], "total": len(rows)}


@router.post("/documents", status_code=201)
async def upload_document(
    response: Response,
    file: UploadFile = File(...),
    group_id: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    """Upload: default PRIVATE (owned by the caller); ``group_id`` makes it
    a GROUP document — editors of that group only (a member gets 403, a
    non-member 404, an admin without membership 404 — no implicit access,
    DECISION #4). Org upload stays admin-only on /api/admin/documents."""
    display_name = Path(file.filename or "").name
    name_problems = display_name_problems(display_name)
    if name_problems:
        raise HTTPException(status_code=422, detail="; ".join(name_problems))
    suffix = Path(display_name).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(
            status_code=400, detail=f"Supported: {', '.join(sorted(SUPPORTED))}"
        )
    header = file.file.read(8)
    file.file.seek(0)
    if not content_matches_suffix(header, suffix):
        raise HTTPException(
            status_code=415, detail="File content does not match the claimed type"
        )
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=413, detail="File exceeds MAX_UPLOAD_MB")

    scope, owner_id, group_uuid = "private", user.id, None
    if group_id is not None:
        try:
            group_uuid = UUID(group_id)
        except ValueError:
            raise HTTPException(status_code=404, detail="Not found") from None
        group = db.get(Group, group_uuid)
        resolver = DbAccessResolver(db)
        if group is None or not group.is_active:
            raise HTTPException(status_code=404, detail="Not found")
        membership = db.execute(
            select(GroupMembership.user_id).where(
                GroupMembership.group_id == group_uuid,
                GroupMembership.user_id == user.id,
            ).limit(1)
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(status_code=404, detail="Not found")
        if not resolver.is_editor(user, group_uuid):
            raise HTTPException(status_code=403, detail="Editor role required")
        scope, owner_id = "group", None  # C2: group documents are OWNERLESS

    doc = documents_service.create_document(
        db, scope=scope, owner_id=owner_id,
        group_id=group_uuid, uploaded_by=user.id,
        display_name=display_name, storage_path="", checksum="", size_bytes=0,
        mime_suffix=suffix or None,
    )
    try:
        dest, size, checksum = documents_service.save_upload_stream(
            file.file, dest_dir=Path(settings.upload_dir) / str(doc.id),
            storage_name=f"{doc.id}{suffix}",  # F1: server-generated
            chunk_validator=make_text_validator() if suffix in {".txt", ".md"} else None,
        )
    except documents_service.UploadTooLarge:
        db.rollback()
        raise HTTPException(status_code=413, detail="File exceeds MAX_UPLOAD_MB") from None
    except documents_service.UploadRejected:
        db.rollback()
        raise HTTPException(status_code=422, detail="Invalid storage name") from None
    except ContentRejected:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="Text upload must be valid UTF-8 without NUL bytes",
        ) from None
    doc.storage_path = str(dest)
    doc.size_bytes = size
    doc.checksum = checksum
    db.flush()
    try:
        loaded = load_file(dest)
        chunks = chunk_documents(loaded)
    except Exception as exc:
        documents_service.mark_failed(db, doc, "unparseable")
        record_event(
            db, "document_uploaded", actor_user_id=user.id,
            object_type="document", object_id=doc.id,
            outcome="error", route="/api/documents",
            details={"scope": scope, "error_class": "unparseable"},
        )
        db.commit()
        logger.error("document ingest failed: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail="File could not be parsed") from None
    try:
        add_documents(chunks, doc_id=doc.id, scope=scope, owner_id=str(owner_id) if owner_id else None,
                      source_name=display_name, group_id=str(group_uuid) if group_uuid else None)
    except Exception as exc:
        documents_service.mark_failed(db, doc, "ingest_failed")
        record_event(
            db, "document_uploaded", actor_user_id=user.id,
            object_type="document", object_id=doc.id,
            outcome="error", route="/api/documents",
            details={"scope": scope, "error_class": "ingest_failed"},
        )
        db.commit()
        logger.error("document ingest failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Ingest failed") from None
    documents_service.attach_ingest_result(db, doc, len(chunks))
    record_event(
        db, "document_uploaded", actor_user_id=user.id,
        object_type="document", object_id=doc.id,
        outcome="ok", route="/api/documents",
        details={"scope": scope, "chunks": len(chunks)},  # ids + counters only
    )
    db.commit()
    _no_store(response)
    return {
        "message": "Document indexed",
        "document_id": str(doc.id),
        "file": display_name,
        "chunks": len(chunks),
        "scope": scope,
        "group_id": str(group_uuid) if group_uuid else None,
    }


@router.get("/documents/{document_id}")
def get_document(
    document_id: UUID,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    doc = _get_visible_or_404(db, user, document_id)
    _no_store(response)
    return _meta(doc)


@router.patch("/documents/{document_id}")
def patch_document(
    document_id: UUID,
    payload: DocumentPatch,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    doc = _get_visible_or_404(db, user, document_id)
    if not _can_manage(user, doc, DbAccessResolver(db)):
        # readable but not manageable (e.g. org doc vs plain user, group doc
        # vs member, group doc vs C3-limited admin member) — or, for an
        # admin on a foreign private doc, can_read already failed above.
        raise HTTPException(status_code=403, detail="Forbidden")
    changed = {}
    if payload.display_name is not None and payload.display_name != doc.display_name:
        problems = display_name_problems(payload.display_name)
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))
        doc.display_name = payload.display_name
        changed["display_name_set"] = True
    if not changed:
        raise HTTPException(status_code=422, detail="No changes supplied")
    record_event(
        db, "document_updated", actor_user_id=user.id,
        object_type="document", object_id=doc.id, outcome="ok",
        details={"fields": sorted(changed)},  # ids/flags only
    )
    db.commit()
    _no_store(response)
    return _meta(doc)


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: UUID,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    """§A9.1 deletion: owner (private), editors of the group, admin ANY
    (C3 — audited, no content access). Outside the audience → 404."""
    doc = documents_service.get_document(db, document_id)
    resolver = DbAccessResolver(db)
    if doc is None:
        raise HTTPException(status_code=404, detail="Not found")
    if user.role != "admin":  # C3: admins delete any document
        if not resolver.can_read(user, doc):
            raise HTTPException(status_code=404, detail="Not found")
        if not _can_manage(user, doc, resolver):
            raise HTTPException(status_code=403, detail="Forbidden")
    documents_service.begin_delete(db, doc)
    db.commit()
    delete_document_vectors(str(doc.id))  # idempotent, outside TX
    documents_service.complete_delete(db, doc.id, actor_user_id=user.id)
    db.commit()
    _no_store(response)
    return {"status": "deleted", "id": str(document_id)}
