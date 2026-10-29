"""argon2id password hashing + policy (docs/backend-design.md §A3/§A5).

Timing-equalization: when a login names a username that does not exist, we
still run an argon2id verify against DUMMY_HASH (a hash of a random secret
generated per process start — never a real credential), so "unknown user"
and "wrong password" are indistinguishable by response time. Response TEXT
is already identical (GENERIC_LOGIN_FAILED); the user-enumeration rule
extends to timing (B2 requirement 1).

Policy: minimum 12 chars, no composition rules; the username (case- and
whitespace-tolerant comparison) is rejected as a password; a 128-char cap
bounds argon2 hashing cost (DoS guard).
"""
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# OWASP-ish argon2id defaults; memory-hard hashing makes offline attacks
# expensive. Explicit so a library default change can't silently weaken us.
_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=4)

# Hash of a random per-process secret: worthless as a credential, same cost
# profile as a real verify.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(24))

GENERIC_LOGIN_FAILED = "Invalid username or password"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """True/False wrapper — argon2 signals mismatch and malformed hashes
    with exceptions; callers just need a bool."""
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_dummy(password: str) -> bool:
    """Run the same verify work for a nonexistent user (timing equalization)."""
    return verify_password(password, DUMMY_HASH)


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash was made with older/cheaper parameters —
    re-hash on successful login to roll parameters forward."""
    return _hasher.check_needs_rehash(password_hash)


def validate_password(password: str, username: str) -> list[str]:
    """Return the list of policy violations ([] = acceptable)."""
    problems: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > MAX_PASSWORD_LENGTH:
        problems.append(f"must be at most {MAX_PASSWORD_LENGTH} characters")
    if password.strip().lower() == username.strip().lower():
        problems.append("must not be the username")
    return problems
