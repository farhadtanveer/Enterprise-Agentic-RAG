"""Client-supplied text hygiene (review finding 3, F3).

Filenames and titles are client-controlled strings that end up stored,
rendered, and (for display_name) echoed in citations. They must never carry
NUL or other control characters (a NUL crashes the Postgres driver and
smuggles terminal sequences through the UI), must never collide with
Windows reserved device names, and must be length-bounded even though the
DB columns are unbounded Text. Endpoints map any problem to 422; the
original filename is NEVER a filesystem path component since F1
(save_upload_stream uses server-generated storage names) — this module is
defense in depth for storage, UI, and the driver.
"""

# Windows reserved device names, case-insensitive, matched on the STEM
# ("CON.txt" is as reserved as "CON"). Includes the COM1–9 / LPT1–9 range
# (COM10+ is NOT reserved).
WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Characters Windows forbids in filenames — rejected for display_name even
# though it no longer reaches the filesystem, so downloads/exports of the
# original name stay portable.
FILENAME_FORBIDDEN_CHARS = set('<>:"/\\|?*')

MAX_FILENAME_LENGTH = 255  # classic filesystem bound; DB column is Text
MAX_TITLE_LENGTH = 200     # mirrors the API request models


def _has_control_chars(value: str) -> bool:
    """C0 controls (incl. NUL), DEL, and C1 controls — nothing printable."""
    return any(ord(c) < 0x20 or ord(c) in (0x7F,) or 0x80 <= ord(c) <= 0x9F for c in value)


def display_name_problems(raw: str) -> list[str]:
    """Problems that make a client-supplied FILENAME unacceptable (422)."""
    problems: list[str] = []
    if not raw or not raw.strip():
        problems.append("filename is empty")
        return problems
    if len(raw) > MAX_FILENAME_LENGTH:
        problems.append(f"filename exceeds {MAX_FILENAME_LENGTH} characters")
    if _has_control_chars(raw):
        problems.append("filename contains NUL or control characters")
    if set(raw) & FILENAME_FORBIDDEN_CHARS:
        problems.append("filename contains reserved characters <>:\"/\\|?*")
    stem = raw.split(".")[0].upper()  # first dot-separated component
    if stem in WINDOWS_RESERVED_STEMS:
        problems.append(f"filename uses a Windows reserved name ({stem})")
    if raw.endswith((".", " ")) and len(raw) > 1:
        problems.append("filename ends with a dot or space (Windows strips them)")
    return problems


def title_problems(raw: str | None) -> list[str]:
    """Problems that make a client-supplied TITLE unacceptable (422)."""
    if raw is None:
        return []
    problems: list[str] = []
    if len(raw) > MAX_TITLE_LENGTH:
        problems.append(f"title exceeds {MAX_TITLE_LENGTH} characters")
    if _has_control_chars(raw):
        problems.append("title contains NUL or control characters")
    return problems
