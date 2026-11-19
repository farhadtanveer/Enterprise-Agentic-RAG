"""A5.6 (docs/tasks/g2.md): RLIMIT_AS is Linux-only — the parser module
must degrade EXPLICITLY (a clear message) on unsupported platforms and
must NEVER crash at import.

The audit found two gaps in ``_apply_memory_limit``: the ImportError path
degraded SILENTLY (a bare ``pass`` — no operator would ever know the
memory cap was not applied), and the AttributeError path (``resource``
exists but has no RLIMIT_AS on some non-Linux POSIX platforms) was NOT
CAUGHT AT ALL — it would crash the parse child and surface as a
misleading ParseFailed('crashed').

Tests stub the ``resource`` module (the Linux container cannot be the
unsupported platform, so the stub IS the unsupported platform).
"""
import subprocess
import sys

from app.services.ingestion import _apply_memory_limit


class _StubRLimit:
    """A 'resource' module lookalike WITHOUT RLIMIT_AS (the non-Linux
    POSIX shape): setrlimit exists, the constant does not."""

    def setrlimit(self, *a, **k):  # pragma: no cover - never reached
        raise AssertionError("setrlimit must not be reached without RLIMIT_AS")


def test_missing_resource_module_degrades_explicitly(monkeypatch, caplog):
    """Windows/unsupported: no 'resource' module at all — a CLEAR warning
    is logged and nothing raises (the silent `pass` is history)."""
    monkeypatch.setitem(sys.modules, "resource", None)  # import raises ImportError
    with caplog.at_level("WARNING"):
        _apply_memory_limit(64)  # must not raise
    assert any(
        "RLIMIT_AS unavailable" in r.message and "memory cap" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_resource_without_rlimit_as_degrades_explicitly(monkeypatch, caplog):
    """non-Linux POSIX: 'resource' imports but RLIMIT_AS is absent — must
    degrade explicitly, NOT crash with AttributeError (RED first: the old
    code only caught ImportError)."""
    monkeypatch.setitem(sys.modules, "resource", _StubRLimit())
    with caplog.at_level("WARNING"):
        _apply_memory_limit(64)  # must not raise
    assert any(
        "RLIMIT_AS unavailable" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


def test_ingestion_imports_cleanly_without_resource():
    """Not-crash-at-import, proven in a REAL subprocess: with 'resource'
    made unimportable, importing app.services.ingestion must exit 0."""
    code = (
        "import sys; sys.modules['resource'] = None; "
        "import app.services.ingestion; "
        "print('import-ok')"
    )
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    assert "import-ok" in r.stdout
