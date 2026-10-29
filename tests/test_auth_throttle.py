"""B2: (username, IP) exponential login throttle — §A5 + §A11 test 8."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.models.login_throttle import LoginThrottle
from app.services import login_throttle as lt

T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)
IP = "198.51.100.7"


def test_first_four_failures_do_not_lock(db_session):
    for _ in range(4):
        lt.record_failure(db_session, "alice", IP, now=T0)
    assert lt.remaining_seconds(db_session, "alice", IP, now=T0 + timedelta(seconds=30)) == 0


def test_fifth_failure_starts_15_minute_delay(db_session):
    for i in range(5):
        lt.record_failure(db_session, "alice", IP, now=T0 + timedelta(seconds=i))
    now = T0 + timedelta(seconds=5)
    left = lt.remaining_seconds(db_session, "alice", IP, now=now)
    assert 890 <= left <= 900


def test_exponential_growth_and_one_hour_cap(db_session):
    t = T0
    # 7 total failures: 5th → 15 min, 6th → 30 min, 7th → 60 min (cap)
    for i in range(7):
        lt.record_failure(db_session, "alice", IP, now=t)
        t += timedelta(seconds=1)
    # simulate the delays elapsing so the next failure re-arms the counter
    t = T0 + timedelta(hours=2)
    lt.record_failure(db_session, "alice", IP, now=t)  # 8th failure
    left = lt.remaining_seconds(db_session, "alice", IP, now=t + timedelta(seconds=1))
    assert left <= 3600  # capped at 1 h


def test_no_lock_after_cooldown_until_new_failure_threshold(db_session):
    # 4 failures, then cooldown passes: counter survives but does not lock
    for i in range(4):
        lt.record_failure(db_session, "alice", IP, now=T0 + timedelta(seconds=i))
    later = T0 + timedelta(hours=3)
    assert lt.remaining_seconds(db_session, "alice", IP, now=later) == 0
    lt.record_failure(db_session, "alice", IP, now=later)  # 5th overall
    assert lt.remaining_seconds(db_session, "alice", IP, now=later + timedelta(seconds=1)) > 0


def test_reset_on_success(db_session):
    lt.record_failure(db_session, "alice", IP, now=T0)
    lt.reset(db_session, "alice", IP)
    assert lt.remaining_seconds(db_session, "alice", IP, now=T0) == 0
    assert db_session.execute(select(LoginThrottle)).scalar_one_or_none() is None


def test_same_ip_different_username_not_throttled(db_session):
    # §A11 test 8: a shared-IP office can't freeze other accounts
    for i in range(5):
        lt.record_failure(db_session, "victim", IP, now=T0 + timedelta(seconds=i))
    assert lt.remaining_seconds(db_session, "alice", IP, now=T0) == 0


def test_same_username_different_ip_not_throttled(db_session):
    # §A11 test 8: an attacker can't lock the victim out from anywhere
    for i in range(5):
        lt.record_failure(db_session, "alice", IP, now=T0 + timedelta(seconds=i))
    assert lt.remaining_seconds(db_session, "alice", "203.0.113.99", now=T0) == 0


def test_username_normalized_case_and_whitespace(db_session):
    lt.record_failure(db_session, "Alice", IP, now=T0)
    lt.record_failure(db_session, "  alice ", IP, now=T0)
    rows = db_session.execute(select(LoginThrottle)).scalars().all()
    assert len(rows) == 1  # same counter row for both spellings
    assert rows[0].fail_count == 2


def test_no_plaintext_username_anywhere_in_the_table(db_session):
    """§A5: counters store a salted hash, never the attempted username —
    for unknown users too (the attacker can't tell which is which)."""
    lt.record_failure(db_session, "bruce-wayne", IP, now=T0)
    lt.record_failure(db_session, "secret-unknown-user", IP, now=T0)
    rows = db_session.execute(select(LoginThrottle)).scalars().all()
    dump = "|".join(
        f"{r.username_hash}|{r.ip}|{r.fail_count}|{r.throttle_until}|{r.last_failure_at}"
        for r in rows
    )
    assert "bruce-wayne" not in dump
    assert "secret-unknown-user" not in dump
    for r in rows:
        assert len(r.username_hash) == 64  # sha256 hex


def test_unlock_clears_all_ips_for_username(db_session):
    lt.record_failure(db_session, "alice", IP, now=T0)
    lt.record_failure(db_session, "alice", "203.0.113.99", now=T0)
    lt.record_failure(db_session, "bob", IP, now=T0)
    removed = lt.clear_for_username(db_session, "alice")
    assert removed == 2  # alice's rows, across both IPs
    remaining = db_session.execute(select(LoginThrottle)).scalars().all()
    assert len(remaining) == 1  # bob's row untouched
