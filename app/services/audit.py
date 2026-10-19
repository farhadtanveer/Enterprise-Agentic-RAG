import json
import sqlite3
from datetime import datetime, timezone
from app.core.config import get_settings

settings = get_settings()

_SCHEMA_COLUMNS = {
    "client_ip": "TEXT NOT NULL DEFAULT ''",
    "authenticated": "INTEGER NOT NULL DEFAULT 0",
    "duration_ms": "INTEGER",
}


def init_db() -> None:
    con = sqlite3.connect(settings.audit_db_path)
    con.execute(
        """CREATE TABLE IF NOT EXISTS query_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            question TEXT NOT NULL,
            source_used TEXT NOT NULL,
            trace_json TEXT NOT NULL
        )"""
    )
    # Idempotent migration for databases created before attribution existed.
    existing = {row[1] for row in con.execute("PRAGMA table_info(query_audit)")}
    for column, decl in _SCHEMA_COLUMNS.items():
        if column not in existing:
            con.execute(f"ALTER TABLE query_audit ADD COLUMN {column} {decl}")
    con.commit()
    con.close()

def write_audit(
    question: str,
    source_used: str,
    trace: list[str],
    client_ip: str = "",
    authenticated: bool = False,
    duration_ms: int | None = None,
) -> None:
    con = sqlite3.connect(settings.audit_db_path)
    con.execute(
        """INSERT INTO query_audit(created_at, question, source_used, trace_json,
                                    client_ip, authenticated, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            datetime.now(timezone.utc).isoformat(),
            question,
            source_used,
            json.dumps(trace),
            client_ip,
            int(authenticated),
            duration_ms,
        ),
    )
    con.commit()
    con.close()
