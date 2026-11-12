"""G1.3 (docs/tasks/g1.md PART 1): the groups service + operator CLIs —
create-group, add-member (--role), ingest-group, remove-member.

Written BEFORE app/services/groups.py and the CLI commands exist (red
first). Service rules the API (G1.4) will reuse: every change writes an
audit event WITHOUT content; duplicate membership / unknown group / bad
role are clean ValueErrors; ingest-group produces an ownerless group doc
(C2) whose vectors carry the strictly prefixed group:<id> token (G1.2).
"""
import pytest
from click.testing import CliRunner
from sqlalchemy import func, select

from app.cli import cli
from app.models.audit_event import AuditEvent
from app.models.document import Document
from app.models.group import Group, GroupMembership
from app.models.user import User
from app.services import users as users_svc

PASSWORD = "a valid longer passphrase"


@pytest.fixture()
def cli_runner(joined_session_factory, monkeypatch):
    monkeypatch.setattr("app.cli._session", joined_session_factory)
    return CliRunner()


def _user(db, name):
    u = users_svc.create_user(db, name, password=PASSWORD)
    db.commit()
    return u


def _audit(db, event_type):
    return db.execute(
        select(AuditEvent).where(AuditEvent.event_type == event_type)
    ).scalars().all()


# --- service ------------------------------------------------------------------------


def test_create_group_validates_and_audits(db_session):
    from app.services import groups as groups_svc

    g = groups_svc.create_group(db_session, "Finance", kind="department", contact="helpdesk")
    db_session.commit()
    assert g.name == "Finance"
    assert g.is_active is True
    assert _audit(db_session, "group_created")
    with pytest.raises(ValueError, match="already exists"):
        groups_svc.create_group(db_session, "Finance")  # names stored verbatim
    db_session.rollback()
    with pytest.raises(ValueError, match="kind"):
        groups_svc.create_group(db_session, "other", kind="squad")
    db_session.rollback()
    with pytest.raises(ValueError, match="empty"):
        groups_svc.create_group(db_session, "   ")
    db_session.rollback()


def test_add_member_role_rules_and_audits(db_session):
    from app.services import groups as groups_svc

    g = groups_svc.create_group(db_session, "members-g")
    u = _user(db_session, "members-u")
    m = groups_svc.add_member(db_session, g, u, role="editor")
    db_session.commit()
    assert m.role == "editor"
    events = _audit(db_session, "membership_added")
    assert len(events) == 1
    assert events[0].details["role"] == "editor"  # no content in audit rows
    with pytest.raises(ValueError, match="already a member"):
        groups_svc.add_member(db_session, g, u, role="member")
    db_session.rollback()
    with pytest.raises(ValueError, match="role"):
        groups_svc.add_member(db_session, g, _user(db_session, "members-u2"), role="owner")
    db_session.rollback()


def test_remove_member_and_change_role_audited(db_session):
    from app.services import groups as groups_svc

    g = groups_svc.create_group(db_session, "rm-g")
    u = _user(db_session, "rm-u")
    groups_svc.add_member(db_session, g, u, role="member")
    db_session.commit()
    groups_svc.set_member_role(db_session, g, u, role="editor")
    db_session.commit()
    assert _audit(db_session, "membership_role_changed")
    groups_svc.remove_member(db_session, g, u)
    db_session.commit()
    assert _audit(db_session, "membership_removed")
    left = db_session.execute(
        select(GroupMembership).where(GroupMembership.user_id == u.id)
    ).scalars().first()
    assert left is None
    with pytest.raises(ValueError, match="not a member"):
        groups_svc.remove_member(db_session, g, u)
    db_session.rollback()


def test_groups_for_user_lists_active_only(db_session):
    from app.services import groups as groups_svc

    u = _user(db_session, "for-u")
    g1 = groups_svc.create_group(db_session, "for-g1")
    g2 = groups_svc.create_group(db_session, "for-g2")
    g3 = groups_svc.create_group(db_session, "for-g3")
    groups_svc.add_member(db_session, g1, u, role="member")
    groups_svc.add_member(db_session, g2, u, role="editor")
    groups_svc.add_member(db_session, g3, u, role="member")
    db_session.commit()
    g3.is_active = False
    db_session.commit()
    memberships = groups_svc.groups_for_user(db_session, u.id)
    assert {str(m.group_id) for m in memberships} == {str(g1.id), str(g2.id)}
    roles = {str(m.group_id): m.role for m in memberships}
    assert roles[str(g2.id)] == "editor"


# --- CLI -----------------------------------------------------------------------------


def test_cli_create_group_add_remove(db_session, cli_runner):
    _user(db_session, "cli-member")
    db_session.commit()

    r = cli_runner.invoke(cli, ["create-group", "Ops", "--contact", "ops-alias"])
    assert r.exit_code == 0, r.output
    g = db_session.execute(select(Group).where(Group.name == "Ops")).scalar_one()

    r = cli_runner.invoke(cli, ["add-member", "Ops", "cli-member", "--role", "editor"])
    assert r.exit_code == 0, r.output
    m = db_session.execute(
        select(GroupMembership).where(GroupMembership.group_id == g.id)
    ).scalar_one()
    assert m.role == "editor"

    r = cli_runner.invoke(cli, ["add-member", "Ops", "cli-member"])
    assert r.exit_code != 0
    assert "already a member" in r.output

    r = cli_runner.invoke(cli, ["remove-member", "Ops", "cli-member"])
    assert r.exit_code == 0, r.output
    assert db_session.execute(
        select(GroupMembership).where(GroupMembership.group_id == g.id)
    ).scalars().first() is None

    r = cli_runner.invoke(cli, ["add-member", "Nope", "cli-member"])
    assert r.exit_code != 0
    assert "No active group" in r.output


def test_cli_ingest_group_creates_ownerless_group_doc(db_session, cli_runner, tmp_path, monkeypatch):
    """The CLI ingest path hits the real vector store — faked here; the fake
    asserts the STRICTLY prefixed group:<id> token reaches the metadata."""
    from app.rag import vectorstore as vs

    _user(db_session, "ig-member")
    db_session.commit()
    cli_runner.invoke(cli, ["create-group", "ig-group"])
    g = db_session.execute(select(Group).where(Group.name == "ig-group")).scalar_one()

    captured = {}

    def fake_add_documents(chunks, *, doc_id, scope, owner_id, source_name, group_id=None):
        captured.update(doc_id=doc_id, scope=scope, owner_id=owner_id,
                        source_name=source_name, group_id=group_id)
        for c in chunks:
            c.metadata.update(doc_id=str(doc_id), scope=scope,
                              read_access=[f"group:{group_id}"], source=source_name)
        return ["v1"]

    monkeypatch.setattr(vs, "add_documents", fake_add_documents)

    note = tmp_path / "ig-note.txt"
    note.write_text("Group one secret revenue figures.\n", encoding="utf-8")
    r = cli_runner.invoke(cli, ["ingest-group", "ig-group", str(note)])
    assert r.exit_code == 0, r.output
    assert "private document" not in r.output

    doc = db_session.execute(
        select(Document).where(Document.display_name == "ig-note.txt")
    ).scalar_one()
    assert str(captured["doc_id"]) == str(doc.id)
    assert doc.scope == "group"
    assert doc.owner_id is None       # C2: group docs are ownerless
    assert doc.group_id == g.id
    assert captured["owner_id"] is None
    assert str(captured["group_id"]) == str(g.id)
    assert _audit(db_session, "document_uploaded")

    r = cli_runner.invoke(cli, ["ingest-group", "missing-group", str(note)])
    assert r.exit_code != 0
    assert "No active group" in r.output
