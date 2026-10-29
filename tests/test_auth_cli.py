"""B2: users service + recovery CLIs (bootstrap-admin / reset-password / unlock).

CLI runs through click's CliRunner with the session factory patched onto the
conftest joined-session factory, so every CLI commit lands inside the
rolled-back test transaction (§A11: real Postgres).
"""
import pytest
from click.testing import CliRunner
from sqlalchemy import func, select

from app.cli import cli
from app.models.audit_event import AuditEvent
from app.models.session import Session as SessionRow
from app.models.user import User
from app.services import login_throttle, users


@pytest.fixture()
def cli_runner(joined_session_factory, monkeypatch):
    monkeypatch.setattr("app.cli._session", joined_session_factory)
    return CliRunner()


def _make_user(db, username="cli-user", role="user", **kwargs) -> User:
    user = User(username=username, password_hash="x", role=role, **kwargs)
    db.add(user)
    db.flush()
    return user


def _audit_count(db, event_type) -> int:
    return db.execute(
        select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == event_type)
    ).scalar_one()


# --- core service ----------------------------------------------------------


def test_bootstrap_creates_admin_and_audits(db_session):
    user = users.bootstrap_first_admin(db_session, "Founding Admin", "a strong passphrase 1")
    db_session.commit()
    assert user.role == "admin"
    assert user.username == "founding admin"  # normalized lowercase
    assert user.must_change_password is False
    assert user.password_hash.startswith("$argon2id$")
    assert _audit_count(db_session, "user_created") == 1


def test_bootstrap_refuses_when_any_admin_exists(db_session):
    users.bootstrap_first_admin(db_session, "first", "a strong passphrase 1")
    db_session.commit()
    with pytest.raises(ValueError, match="already exists"):
        users.bootstrap_first_admin(db_session, "second", "another passphrase 2")
    assert _audit_count(db_session, "user_created") == 1  # no event for refusal


def test_bootstrap_refuses_even_when_admin_disabled(db_session):
    _make_user(db_session, username="old-admin", role="admin", is_active=False)
    with pytest.raises(ValueError, match="already exists"):
        users.bootstrap_first_admin(db_session, "second", "another passphrase 2")


def test_bootstrap_enforces_password_policy(db_session):
    with pytest.raises(ValueError, match="12"):
        users.bootstrap_first_admin(db_session, "first", "short12")
    with pytest.raises(ValueError, match="username"):
        users.bootstrap_first_admin(db_session, "first", "first")


def test_username_normalization_and_charset(db_session):
    assert users.normalize_username(" Mixed Case ") == "mixed case"
    assert users.USERNAME_RE.fullmatch("alice.co_2-x") is not None
    assert users.USERNAME_RE.fullmatch("no") is None  # too short
    assert users.USERNAME_RE.fullmatch("has space") is None
    assert users.USERNAME_RE.fullmatch("semi;colon") is None


def _live_session_count(db) -> int:
    return db.execute(
        select(func.count()).select_from(SessionRow).where(SessionRow.revoked_at.is_(None))
    ).scalar_one()


def test_admin_reset_password_flags_revokes_audits(db_session):
    user = _make_user(db_session, username="locked-out")
    from app.services.sessions import create_session

    for _ in range(2):
        create_session(db_session, user.id)
    users.admin_reset_password(db_session, user, "brand new temp pw 1")
    db_session.commit()
    assert user.must_change_password is True
    assert user.password_changed_at is not None
    assert _live_session_count(db_session) == 0  # rows remain, all revoked
    assert _audit_count(db_session, "password_reset_by_admin") == 1


def test_admin_reset_enforces_policy(db_session):
    user = _make_user(db_session)
    with pytest.raises(ValueError):
        users.admin_reset_password(db_session, user, user.username)  # == username


def test_would_leave_zero_admins_matrix(db_session):
    admin = _make_user(db_session, username="solo-admin", role="admin")
    user = _make_user(db_session, username="plain-user")
    assert users.would_leave_zero_admins(db_session, admin) is True  # last admin
    assert users.would_leave_zero_admins(db_session, user) is False  # not an admin
    second = _make_user(db_session, username="admin-two", role="admin")
    assert users.would_leave_zero_admins(db_session, admin) is False  # two admins now
    second.is_active = False
    assert users.would_leave_zero_admins(db_session, admin) is True  # one active again


def test_active_admin_count_ignores_disabled(db_session):
    _make_user(db_session, username="a1", role="admin", is_active=True)
    _make_user(db_session, username="a2", role="admin", is_active=False)
    assert users.active_admin_count(db_session) == 1


def test_unlock_username_audits(db_session):
    login_throttle.record_failure(db_session, "alice", "198.51.100.7")
    cleared = users.unlock_username(db_session, "alice")
    db_session.commit()
    assert cleared == 1
    assert _audit_count(db_session, "account_unlocked") == 1


# --- CLI surface (click prompts, refusal exit codes) -----------------------


def test_cli_bootstrap_admin_happy_path(cli_runner, db_session):
    result = cli_runner.invoke(
        cli,
        ["bootstrap-admin"],
        input="boss\na valid longer passphrase\na valid longer passphrase\n",
    )
    assert result.exit_code == 0, result.output
    row = db_session.execute(select(User).where(User.username == "boss")).scalar_one()
    assert row.role == "admin"


def test_cli_bootstrap_admin_refusal_exit(cli_runner, db_session):
    _make_user(db_session, username="existing-admin", role="admin")
    db_session.commit()
    result = cli_runner.invoke(cli, ["bootstrap-admin"], input="")
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_cli_bootstrap_reprompts_on_weak_password(cli_runner, db_session):
    result = cli_runner.invoke(
        cli,
        ["bootstrap-admin"],
        input="boss\nweakpw\nweakpw\na valid longer passphrase\na valid longer passphrase\n",
    )
    assert result.exit_code == 0, result.output
    assert "invalid password" in result.output  # the re-prompt message
    row = db_session.execute(select(User).where(User.username == "boss")).scalar_one()
    assert row.role == "admin"


def test_cli_reset_password_flow(cli_runner, db_session):
    user = _make_user(db_session, username="forgotten")
    from app.services.sessions import create_session

    create_session(db_session, user.id)
    result = cli_runner.invoke(
        cli,
        ["reset-password", "forgotten"],
        input="fresh temp password\nfresh temp password\n",
    )
    assert result.exit_code == 0, result.output
    db_session.expire_all()
    user = db_session.get(User, user.id)
    assert user.must_change_password is True
    assert user.password_hash != "x"
    assert _live_session_count(db_session) == 0
    assert _audit_count(db_session, "password_reset_by_admin") == 1


def test_cli_reset_password_unknown_user(cli_runner, db_session):
    result = cli_runner.invoke(cli, ["reset-password", "nobody"], input="")
    assert result.exit_code != 0
    assert "No such user" in result.output


def test_cli_unlock_flow(cli_runner, db_session):
    login_throttle.record_failure(db_session, "alice", "198.51.100.7")
    login_throttle.record_failure(db_session, "alice", "203.0.113.99")
    login_throttle.record_failure(db_session, "bob", "198.51.100.7")
    result = cli_runner.invoke(cli, ["unlock", "alice"], input="")
    assert result.exit_code == 0, result.output
    assert "2 throttle record(s)" in result.output
    from app.models.login_throttle import LoginThrottle

    remaining = db_session.execute(select(LoginThrottle)).scalars().all()
    assert len(remaining) == 1  # bob's row survives


def test_cli_never_accepts_passwords_via_argv():
    """Review fix 5: passwords come from hidden prompts/stdin only — no
    command may expose a password parameter (shell history / ps leakage)."""
    for name, command in cli.commands.items():
        param_names = {p.name for p in command.params}
        secrets_on_argv = param_names & {"password", "new_password", "temp_password", "old_password"}
        assert not secrets_on_argv, f"{name} accepts a password via argv: {secrets_on_argv}"
