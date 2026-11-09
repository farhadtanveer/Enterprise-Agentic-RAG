"""Admin surface (§A8 Admin + docs/tasks/phase-b5-b6.md Part 4, B6).

Conventions (§A8): session + CSRF on every mutating call; a plain user
gets 403 (require_admin), an anonymous caller 401; a nonexistent target
is 404 (existence not leaked); the last-admin guard answers 409 on EVERY
path that could leave zero active admins — including the admin acting on
themselves. Audit events carry counts/ids only, never content.

C2/C3/C4 rules: deleting a user removes ONLY their private documents
(org/group documents are ownerless and survive, uploaded_by concept lands
with the groups phase); admin document WRITE access outside org is
delete-only; the admin document listing is metadata-only and ORG-only —
private documents never appear per-document (no filenames, DECISION #1).
"""
import logging
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.api.deps import require_admin
from app.core.config import get_settings
from app.core.db import get_db
from app.models.document import Document
from app.models.user import User
from app.rag.vectorstore import add_documents, delete_document_vectors
from app.services import documents as documents_service
from app.services import sessions as sessions_service
from app.services import users as users_service
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

router = APIRouter(prefix="/api/admin", tags=["admin"])
settings = get_settings()
logger = logging.getLogger(__name__)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    role: str = Field(default="user")
    display_name: str | None = Field(default=None, max_length=200)


class UpdateUserRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    display_name: str | None = Field(default=None, max_length=200)


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    username: str
    display_name: str | None
    role: str
    is_active: bool
    must_change_password: bool
    created_at: str


def _user_out(u: User) -> dict:
    return {
        "id": str(u.id),
        "username": u.username,
        "display_name": u.display_name,
        "role": u.role,
        "is_active": u.is_active,
        "must_change_password": u.must_change_password,
        "created_at": u.created_at.isoformat(),
    }


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _get_user_or_404(db: DBSession, user_id: UUID) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="Not found")
    return u


def _guard_last_admin(db: DBSession, target: User, *, role: str | None, is_active: bool | None) -> None:
    """409 when this change would leave zero ACTIVE admins (§A5) — applies
    to demotions, disables and deletes, including on the admin themselves."""
    new_role = role if role is not None else target.role
    new_active = is_active if is_active is not None else target.is_active
    if not (target.role == "admin" and target.is_active):
        return  # target isn't an active admin; nothing can be lost
    if new_role == "admin" and new_active:
        return  # still an active admin afterwards
    if users_service.active_admin_count(db) <= 1:
        raise HTTPException(
            status_code=409, detail={"code": "last_admin"}
        )


# --- users ----------------------------------------------------------------------


@router.get("/users")
def list_users(
    response: Response,
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    rows = db.execute(
        select(User).order_by(User.created_at).limit(500)
    ).scalars().all()
    _no_store(response)
    return {"items": [_user_out(u) for u in rows], "total": len(rows)}


@router.post("/users", status_code=201)
def create_user(
    payload: CreateUserRequest,
    response: Response,
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    if payload.role not in ("user", "admin"):
        raise HTTPException(status_code=422, detail="role must be 'user' or 'admin'")
    try:
        u = users_service.create_user(
            db, payload.username, password=payload.password,
            role=payload.role, display_name=payload.display_name,
            must_change_password=True,  # admin-issued credentials are temp
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Username already exists") from None
    record_event(
        db, "user_created", actor_user_id=admin.id,
        object_type="user", object_id=u.id, outcome="ok",
        details={"role": u.role, "must_change_password": True},
    )
    db.commit()
    _no_store(response)
    return _user_out(u)


@router.patch("/users/{user_id}")
def update_user(
    user_id: UUID,
    payload: UpdateUserRequest,
    response: Response,
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    u = _get_user_or_404(db, user_id)
    if payload.role is not None and payload.role not in ("user", "admin"):
        raise HTTPException(status_code=422, detail="role must be 'user' or 'admin'")
    _guard_last_admin(db, u, role=payload.role, is_active=payload.is_active)
    changed = {}
    if payload.role is not None and payload.role != u.role:
        u.role = payload.role
        changed["role"] = payload.role
    if payload.is_active is not None and payload.is_active != u.is_active:
        u.is_active = payload.is_active
        changed["is_active"] = payload.is_active
        if not payload.is_active:
            revoked = sessions_service.revoke_user_sessions(db, u.id)
            changed["revoked_sessions"] = revoked
    if payload.display_name is not None:
        u.display_name = payload.display_name
        changed["display_name_set"] = True
    if not changed:
        raise HTTPException(status_code=422, detail="No changes supplied")
    record_event(
        db, "user_updated", actor_user_id=admin.id,
        object_type="user", object_id=u.id, outcome="ok",
        details=changed,
    )
    db.commit()
    _no_store(response)
    return _user_out(u)


@router.post("/users/{user_id}/reset-password")
def reset_password(
    user_id: UUID,
    payload: ResetPasswordRequest,
    response: Response,
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    u = _get_user_or_404(db, user_id)
    try:
        users_service.admin_reset_password(db, u, payload.new_password)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.commit()
    _no_store(response)
    return {"status": "password_reset", "must_change_password": True}


@router.delete("/users/{user_id}")
def delete_user(
    user_id: UUID,
    response: Response,
    confirm: str = Query(default=""),
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    if confirm != "true":
        raise HTTPException(
            status_code=422, detail="Delete requires confirm=true (double confirmation)"
        )
    u = _get_user_or_404(db, user_id)
    _guard_last_admin(db, u, role="user", is_active=False)
    # §A9.1: the user's PRIVATE documents (vectors + stored files) go first,
    # while the doc rows and ids are still resolvable; the user-row cascade
    # then removes sessions, conversations -> messages -> feedback and the
    # private document rows. Org documents are ownerless (C2) and survive.
    docs = documents_service.private_documents_of(db, u.id)
    for d in docs:
        delete_document_vectors(str(d.id))
        documents_service.remove_stored_file(d.storage_path)
    db.execute(sa_delete(User).where(User.id == u.id))
    record_event(
        db, "user_deleted", actor_user_id=admin.id,
        object_type="user", object_id=u.id, outcome="ok",
        details={"private_documents": len(docs)},
    )
    db.commit()
    _no_store(response)
    return {"status": "deleted", "private_documents": len(docs)}


@router.post("/users/{user_id}/purge-private-documents")
def purge_private_documents(
    user_id: UUID,
    response: Response,
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    """C4 bulk delete: all private documents of user X (+ vectors + files).
    The user keeps their account and their chats; org documents (C2) are
    never touched by this endpoint."""
    u = _get_user_or_404(db, user_id)
    docs = documents_service.private_documents_of(db, u.id)
    for d in docs:
        delete_document_vectors(str(d.id))
        documents_service.remove_stored_file(d.storage_path)
        db.delete(d)
    record_event(
        db, "private_documents_purged", actor_user_id=admin.id,
        object_type="user", object_id=u.id, outcome="ok",
        details={"purged": len(docs)},
    )
    db.commit()
    _no_store(response)
    return {"status": "purged", "purged": len(docs)}


# --- documents ------------------------------------------------------------------


@router.get("/documents")
def list_documents(
    response: Response,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    """C4/DECISION #1: ORG documents per-document (meta only). Private
    documents NEVER appear here — not their rows, not their filenames.
    Private volume is served by aggregates + the near-quota list (groups
    phase); this endpoint counts private docs only as a total."""
    q = (
        select(Document)
        .where(Document.scope == "org")
        .order_by(Document.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(q).scalars().all()
    total_org = db.execute(
        select(func.count()).select_from(Document).where(Document.scope == "org")
    ).scalar_one()
    total_private = db.execute(
        select(func.count()).select_from(Document).where(Document.scope == "private")
    ).scalar_one()
    items = [
        {
            "id": str(d.id),
            "display_name": d.display_name,
            "status": d.status,
            "size_bytes": d.size_bytes,
            "chunk_count": d.chunk_count,
            "created_at": d.created_at.isoformat(),
        }
        for d in rows
    ]
    _no_store(response)
    return {
        "items": items,
        "total_org": total_org,
        "private_documents_total": total_private,  # count only — never rows
    }


@router.post("/documents", status_code=201)
async def upload_org_document(
    response: Response,
    file: UploadFile = File(...),
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    """Org upload over the admin SESSION (no static key). Same gates and
    error contract as the legacy ingest (F1/F3/F6/S2)."""
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

    doc = documents_service.create_document(
        db, scope="org", owner_id=None, display_name=display_name,
        storage_path="", checksum="", size_bytes=0, mime_suffix=suffix or None,
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
            db, "document_uploaded", actor_user_id=admin.id,
            object_type="document", object_id=doc.id,
            outcome="error", route="/api/admin/documents",
            details={"scope": "org", "error_class": "unparseable"},
        )
        db.commit()
        logger.error("admin ingest failed: %s", type(exc).__name__)
        raise HTTPException(status_code=422, detail="File could not be parsed") from None
    try:
        add_documents(chunks, doc_id=doc.id, scope="org", owner_id=None,
                      source_name=display_name)
    except Exception as exc:
        documents_service.mark_failed(db, doc, "ingest_failed")
        record_event(
            db, "document_uploaded", actor_user_id=admin.id,
            object_type="document", object_id=doc.id,
            outcome="error", route="/api/admin/documents",
            details={"scope": "org", "error_class": "ingest_failed"},
        )
        db.commit()
        logger.error("admin ingest failed: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Ingest failed") from None
    documents_service.attach_ingest_result(db, doc, len(chunks))
    record_event(
        db, "document_uploaded", actor_user_id=admin.id,
        object_type="document", object_id=doc.id,
        outcome="ok", route="/api/admin/documents",
        details={"scope": "org", "chunks": len(chunks)},
    )
    db.commit()
    _no_store(response)
    return {
        "message": "Document indexed",
        "document_id": str(doc.id),
        "file": display_name,
        "chunks": len(chunks),
    }


@router.delete("/documents/{document_id}")
def delete_document(
    document_id: UUID,
    response: Response,
    admin: User = Depends(require_admin),
    db: DBSession = Depends(get_db),
) -> dict:
    """C3: admin may delete ANY document (org, group, or private) — audited,
    with no content access. 404 for unknown ids; §A9.1 vectors-first."""
    doc = documents_service.get_document(db, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Not found")
    documents_service.begin_delete(db, doc)
    db.commit()
    delete_document_vectors(str(doc.id))  # idempotent, outside TX
    documents_service.complete_delete(db, doc.id, actor_user_id=admin.id)
    db.commit()
    _no_store(response)
    return {"status": "deleted", "id": str(document_id)}
