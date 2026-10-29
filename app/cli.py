"""Operator CLIs (docs/backend-design.md §A5) — run inside the compose network:

    docker compose run --rm app python -m app.cli bootstrap-admin
    docker compose run --rm app python -m app.cli reset-password <username>
    docker compose run --rm app python -m app.cli unlock <username>

Interactive getpass prompts (no echo, confirmed); the operator has shell
access — the same trust level as the design assumes. Every command writes
an audit event. There is no default password anywhere (§A5).
"""
import click

from app.core.db import get_sessionmaker
from app.services import users
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


if __name__ == "__main__":
    cli()
