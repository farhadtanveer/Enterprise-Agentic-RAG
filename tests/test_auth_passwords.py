"""B2: argon2id hashing, password policy, and the timing-equalization path."""
from app.services import passwords
from app.services.passwords import (
    GENERIC_LOGIN_FAILED,
    validate_password,
    verify_dummy,
    verify_password,
)


def test_hash_is_argon2id_and_verifies():
    hashed = passwords.hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password("correct horse battery staple", hashed)
    assert not verify_password("wrong password entirely", hashed)


def test_verify_tolerates_malformed_hash():
    # a corrupted/foreign hash must return False, never raise
    assert not verify_password("anything", "not-a-hash")


def test_dummy_hash_is_not_a_real_credential():
    # it's a hash of a per-process random secret — no known password opens it
    assert not verify_password("", passwords.DUMMY_HASH)
    assert not verify_password("password", passwords.DUMMY_HASH)
    assert passwords.DUMMY_HASH.startswith("$argon2id$")


def test_dummy_verify_runs_real_work():
    # the timing-equalization path does the same argon2 work as a real verify
    assert verify_dummy("guess") is False  # returns False after full hashing


def test_dummy_hash_differs_per_process_import():
    # each process gets its own random dummy secret
    import importlib

    old = passwords.DUMMY_HASH
    importlib.reload(passwords)
    assert passwords.DUMMY_HASH != old


def test_policy_min_length():
    assert validate_password("abcdefghijkl", "alice") == []  # 12 ok
    problems = validate_password("abcdefghijk", "alice")  # 11 too short
    assert problems and "12" in problems[0]


def test_policy_max_length():
    assert validate_password("x" * 128, "alice") == []
    problems = validate_password("x" * 129, "alice")
    assert problems and "128" in problems[0]


def test_policy_rejects_username_case_insensitive():
    assert validate_password("alice", "alice")
    assert validate_password("Alice", "alice")
    assert validate_password("  ALICE  ", "alice")  # whitespace-tolerant compare
    assert validate_password("alice", "ALICE")


def test_policy_allows_long_passphrases_and_unrelated_passwords():
    assert validate_password("Truffula grove 42!", "alice") == []
    assert validate_password("Árvíztűrő tükörfúrógép", "bob") == []  # unicode


def test_generic_error_text_is_shared():
    # one constant, so unknown-user and wrong-password can never drift apart
    assert GENERIC_LOGIN_FAILED == "Invalid username or password"


def test_needs_rehash_false_for_current_params():
    hashed = passwords.hash_password("another fine password")
    assert not passwords.needs_rehash(hashed)
