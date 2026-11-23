"""P1 (docs/tasks/pgvector-p1.md): prove the vector extension exists and is
at least 0.8.0 — the version that brought iterative scans (hnsw.iterative_
scan / max_scan_tuples) which the retrieval design depends on under
restrictive access filters (docs/pgvector-design.md §4).

Runs against the REAL database the suite already uses (pg_engine fixture —
compose test profile and CI alike), so the image swap is proven by the
same database the rest of the suite runs on, not by a side check.
"""
from sqlalchemy import text

REQUIRED = (0, 8, 0)


def _extversion(pg_engine) -> str | None:
    with pg_engine.connect() as conn:
        return conn.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()


def _semver(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in version.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break  # tolerate suffixes like '0.8.1rc1'
        parts.append(int(digits or 0))
    return tuple(parts)


def test_vector_extension_exists(pg_engine):
    version = _extversion(pg_engine)
    assert version is not None, (
        "extension 'vector' is missing on the test database — the "
        "pgvector/pgvector:pg18 image (fresh volume: docker/initdb/) or "
        "scripts/sql/bootstrap-existing.sql (existing cluster) did not run"
    )


def test_vector_extension_is_at_least_0_8_0(pg_engine):
    version = _extversion(pg_engine)
    assert version is not None, "extension 'vector' is missing"
    parsed = _semver(version)
    assert parsed >= REQUIRED, (
        f"pgvector {version} < 0.8.0 — iterative scans unavailable; the "
        "retrieval design (docs/pgvector-design.md §4) requires >= 0.8.0"
    )
