"""L5: build context must exclude runtime data and local-only files."""
from pathlib import Path

REQUIRED = [
    "uploads/",
    "data/*.db",
    "state/",
    "Agentic-RAG-demo/",
    "create_project.py",
    "steps.md",
    ".env",
]


def test_dockerignore_excludes_runtime_data_and_local_files():
    lines = {
        ln.strip()
        for ln in (Path(__file__).resolve().parents[1] / ".dockerignore")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    missing = [pattern for pattern in REQUIRED if pattern not in lines]
    assert missing == [], f".dockerignore is missing: {missing}"
