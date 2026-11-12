"""Operator CLIs (docs/backend-design.md §A5) — run inside the compose network:

    docker compose run --rm app python -m app.cli bootstrap-admin
    docker compose run --rm app python -m app.cli reset-password <username>
    docker compose run --rm app python -m app.cli unlock <username>

Interactive getpass prompts (no echo, confirmed); the operator has shell
access — the same trust level as the design assumes. Every command writes
an audit event. There is no default password anywhere (§A5).
"""
from pathlib import Path

import click

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.services import users
from app.services.audit_events import record_event
from app.services.passwords import validate_password


def _session():
    """Fresh session from settings (DATABASE_URL) — db:5432 in compose."""
    return get_sessionmaker()()


def _prompt_username() -> str:
    while True:
        username = users.normalize_username(click.prompt("Username"))
        if users.USERNAME_RE.fullmatch(username):
            return username
        click.echo("  invalid username: 3–64 chars of a-z, 0-9, dot, underscore, dash", err=True)


def _prompt_password(username: str, label: str = "Password") -> str:
    """getpass-style prompt with confirmation + policy loop (§A5)."""
    while True:
        password = click.prompt(label, hide_input=True, confirmation_prompt=True)
        problems = validate_password(password, username)
        if not problems:
            return password
        for problem in problems:
            click.echo(f"  invalid password: {problem}", err=True)


@click.group()
def cli() -> None:
    """Multi-user operator commands."""


@cli.command("bootstrap-admin")
def bootstrap_admin() -> None:
    """Interactive FIRST-admin bootstrap; refuses if any admin exists."""
    db = _session()
    try:
        if users.has_any_admin(db):
            raise click.ClickException(
                "An admin already exists — bootstrap-admin refuses to run."
            )
        username = _prompt_username()
        if users.get_by_username(db, username):
            raise click.ClickException(f"Username '{username}' is already taken.")
        password = _prompt_password(username)
        user = users.bootstrap_first_admin(db, username, password)
        db.commit()
        click.echo(f"Admin '{username}' created (id {user.id}).")
    finally:
        db.close()


@cli.command("reset-password")
@click.argument("username")
def reset_password(username: str) -> None:
    """Operator recovery: temp password + forced change + revoke sessions."""
    db = _session()
    try:
        user = users.get_by_username(db, username)
        if user is None:
            raise click.ClickException(f"No such user: {username}")
        password = _prompt_password(user.username, label="New temporary password")
        users.admin_reset_password(db, user, password)
        db.commit()
        click.echo(f"Password for '{user.username}' reset; all their sessions revoked.")
        click.echo(
            "The temporary password is NOT shown anywhere — hand it over out-of-band."
        )
    finally:
        db.close()


@cli.command("unlock")
@click.argument("username")
def unlock(username: str) -> None:
    """Operator recovery: clear (username, IP) login-throttle counters."""
    db = _session()
    try:
        cleared = users.unlock_username(db, users.normalize_username(username))
        db.commit()
        click.echo(f"Cleared {cleared} throttle record(s) for '{username}'.")
    finally:
        db.close()


# --- B4 document ingestion (operator) ----------------------------------------


def _ingest_one(db, *, path: Path, scope: str, owner_id, source_label: str,
                group_id=None, uploaded_by=None) -> str:
    from app.rag.vectorstore import add_documents
    from app.services import documents as docs_service
    from app.services.ingestion import (
        ContentRejected,
        chunk_documents,
        content_matches_suffix,
        load_file,
        make_text_validator,
    )
    from app.services.validation import display_name_problems

    display_name = path.name
    suffix = path.suffix.lower()
    problems = display_name_problems(display_name)
    if problems:
        # F3: the same hygiene rules apply to operator-supplied names
        raise click.ClickException(
            f"invalid filename {display_name!r}: " + "; ".join(problems)
        )
    # F6: same content-vs-suffix gate as the API
    with path.open("rb") as fh:
        header = fh.read(8)
    if not content_matches_suffix(header, suffix):
        raise click.ClickException(
            f"{display_name!r}: content does not match the {suffix} type"
        )
    doc = docs_service.create_document(
        db, scope=scope, owner_id=owner_id, display_name=display_name,
        storage_path="", checksum="", size_bytes=0,
        mime_suffix=suffix or None, group_id=group_id, uploaded_by=uploaded_by,
    )
    dest_dir = Path(get_settings().upload_dir) / str(doc.id)
    try:
        dest, size, checksum = docs_service.save_upload_stream(
            path.open("rb"), dest_dir=dest_dir,
            storage_name=f"{doc.id}{suffix}",  # F1: server-generated
            chunk_validator=make_text_validator() if suffix in {".txt", ".md"} else None,
        )
    except ContentRejected as exc:
        db.rollback()  # the pending row dies; partial file already removed
        raise click.ClickException(f"{display_name!r}: {exc}") from None
    doc.storage_path = str(dest)
    doc.size_bytes = size
    doc.checksum = checksum
    db.flush()
    chunks = chunk_documents(load_file(dest))
    add_documents(chunks, doc_id=doc.id, scope=scope, owner_id=owner_id,
                  source_name=display_name, group_id=group_id)
    docs_service.attach_ingest_result(db, doc, len(chunks))
    record_event(
        db, "document_uploaded", object_type="document", object_id=doc.id,
        outcome="ok", route="cli", details={"scope": scope, "chunks": len(chunks), "via": source_label},
    )
    db.commit()
    return str(doc.id)


@cli.command("ingest-org")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
def ingest_org(directory: Path) -> None:
    """Ingest every supported file in DIRECTORY as ORG documents (§A7)."""
    from app.services.ingestion import SUPPORTED

    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED)
    if not files:
        raise click.ClickException(f"No supported files ({', '.join(sorted(SUPPORTED))}) in {directory}")
    db = _session()
    try:
        for path in files:
            doc_id = _ingest_one(db, path=path, scope="org", owner_id=None, source_label="ingest-org")
            click.echo(f"indexed {path.name} → org document {doc_id}")
    finally:
        db.close()


@cli.command("ingest-private")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--owner", required=True, help="Username of the private owner (e.g. D15 re-ingest).")
def ingest_private(path: Path, owner: str) -> None:
    """Ingest one file as a PRIVATE document owned by --owner (D15)."""
    db = _session()
    try:
        user = users.get_by_username(db, owner)
        if user is None:
            raise click.ClickException(f"No such user: {owner}")
        doc_id = _ingest_one(db, path=path, scope="private", owner_id=user.id, source_label="ingest-private")
        click.echo(f"indexed {path.name} → private document {doc_id} (owner {user.username})")
    finally:
        db.close()


@cli.command("create-group")
@click.argument("name")
@click.option("--kind", type=click.Choice(["department", "project_team"]),
              default="department", show_default=True)
@click.option("--contact", default=None, help="Shown on insufficient-evidence answers (G2 surface).")
def create_group(name: str, kind: str, contact: str | None) -> None:
    """Create a department/project_team group (§A14.4 M1)."""
    from app.services import groups as groups_svc

    db = _session()
    try:
        group = groups_svc.create_group(db, name, kind=kind, contact=contact)
        db.commit()
        click.echo(f"Group '{group.name}' created (id {group.id}, kind {group.kind}).")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        db.close()


def _active_group_or_fail(db, name: str):
    from app.services import groups as groups_svc

    group = groups_svc.get_active_by_name(db, name)
    if group is None:
        raise click.ClickException(f"No active group named '{name}'.")
    return group


@cli.command("add-member")
@click.argument("group")
@click.argument("username")
@click.option("--role", type=click.Choice(["member", "editor"]),
              default="member", show_default=True)
def add_member(group: str, username: str, role: str) -> None:
    """Add USERNAME to GROUP with a membership role (C1 — editor-ness lives
    HERE, never on users.role)."""
    from app.services import groups as groups_svc
    from app.services import users as users_svc

    db = _session()
    try:
        g = _active_group_or_fail(db, group)
        user = users_svc.get_by_username(db, username)
        if user is None:
            raise click.ClickException(f"No such user: {username}")
        m = groups_svc.add_member(db, g, user, role=role)
        db.commit()
        click.echo(f"{user.username} added to '{g.name}' as {m.role}.")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        db.close()


@cli.command("remove-member")
@click.argument("group")
@click.argument("username")
def remove_member(group: str, username: str) -> None:
    """Remove USERNAME from GROUP (membership row deleted; audited)."""
    from app.services import groups as groups_svc
    from app.services import users as users_svc

    db = _session()
    try:
        g = _active_group_or_fail(db, group)
        user = users_svc.get_by_username(db, username)
        if user is None:
            raise click.ClickException(f"No such user: {username}")
        groups_svc.remove_member(db, g, user)
        db.commit()
        click.echo(f"{user.username} removed from '{g.name}'.")
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        db.close()


@cli.command("ingest-group")
@click.argument("group")
@click.argument("path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
def ingest_group(group: str, path: Path) -> None:
    """Ingest one file as a GROUP document (§A14.4 M2): ownerless (C2),
    group_id set, vectors carry the strictly prefixed group:<id> token."""
    db = _session()
    try:
        g = _active_group_or_fail(db, group)
        doc_id = _ingest_one(db, path=path, scope="group", owner_id=None,
                             source_label="ingest-group", group_id=g.id)
        click.echo(f"indexed {path.name} → group document {doc_id} (group {g.name})")
    finally:
        db.close()


@cli.command("documents-retry-deleting")
def documents_retry_deleting() -> None:
    """Sweep (§A9.1): resume documents stuck in 'deleting' (Pinecone outage)."""
    from app.rag.vectorstore import delete_document_vectors
    from app.services import documents as docs_service

    db = _session()
    try:
        completed = docs_service.retry_stuck_deletes(db, delete_document_vectors)
        click.echo(f"Completed {completed} stuck deletion(s).")
    finally:
        db.close()


if __name__ == "__main__":
    cli()
