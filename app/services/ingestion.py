import codecs
import logging
import multiprocessing
import threading
import zipfile
from pathlib import Path
from typing import Callable, Iterable
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from docx import Document as DocxDocument

from app.core.config import get_settings

logger = logging.getLogger(__name__)


SUPPORTED = {".pdf", ".txt", ".md", ".docx"}

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"  # every docx is a ZIP local-file-header archive


def content_matches_suffix(header: bytes, suffix: str) -> bool:
    """F6 (review finding 4): the claimed type is checked against the BYTES,
    not the client-controlled extension. PDF must start with %PDF-, docx
    with a ZIP local file header. Full zip STRUCTURE stays at the parser
    stage (a PK-prefixed non-zip ends in the handled ingest-failure path —
    pinned contract); text content is validated incrementally during
    streaming (make_text_validator)."""
    s = suffix.lower()
    if s == ".pdf":
        return header.startswith(PDF_MAGIC)
    if s == ".docx":
        return header.startswith(ZIP_MAGIC)
    return True  # .txt/.md: no cheap magic; validated while streaming


class ContentRejected(Exception):
    """Raised when streamed bytes violate the text contract (maps to 422)."""


def make_text_validator() -> Callable[[bytes], None]:
    """Streaming validator for .txt/.md uploads: the whole file must decode
    as UTF-8 and contain no NUL byte (binary smuggled as text). Raises
    ContentRejected; save_upload_stream invokes it per chunk and once more
    with b"" to flush the incremental decoder."""
    decoder = codecs.getincrementaldecoder("utf-8")()

    def validate(chunk: bytes) -> None:
        if b"\x00" in chunk:
            raise ContentRejected("text upload contains NUL bytes")
        try:
            decoder.decode(chunk, final=(chunk == b""))
        except UnicodeDecodeError as exc:
            raise ContentRejected("text upload is not valid UTF-8") from exc

    return validate



def load_file(path: Path) -> list[Document]:
    """DECISION #8 (G2 A3): parse in a SPAWNED SUBPROCESS with a hard
    timeout and a memory limit — an unbounded or malicious file can kill
    the CHILD, never this process. Pages, extracted characters and the
    docx uncompressed size are capped in the child (zip-bomb guard before
    parsing). ANY child failure raises ParseFailed; the routes map that to
    the generic 422 "File could not be parsed" (S2: never 500, never raw
    parser text)."""
    payload = _spawn_and_collect(
        _parse_worker, (str(path),),
        timeout_s=get_settings().parser_timeout_seconds,
        memory_limit_mb=get_settings().parser_max_memory_mb,
    )
    return [Document(page_content=c, metadata=m or {}) for c, m in payload]


class ParseFailed(Exception):
    """A parse could not complete (timeout / killed child / cap refusal /
    corrupt file). Carries a CLASS-only reason — never parser text (§A6).
    Routes map this to the generic 422."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _apply_memory_limit(limit_mb: int) -> None:
    try:
        import resource

        limit = limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except ImportError:
        pass  # non-POSIX host: the subprocess + timeout still bound the parse


def _parse_worker(conn, path_str: str) -> None:
    """Runs in the spawned child. Never raises across the boundary — every
    outcome travels through the pipe as (status, payload). A Pipe is used
    instead of a Queue deliberately: Queue.put spawns a feeder THREAD,
    which a tight RLIMIT_AS (the very limit this worker enforces) can
    refuse — send() on a Pipe runs on the calling thread."""
    try:
        settings = get_settings()
        _apply_memory_limit(settings.parser_max_memory_mb)
        path = Path(path_str)
        suffix = path.suffix.lower()
        if suffix == ".docx":  # zip-bomb guard BEFORE any decompression
            with zipfile.ZipFile(path_str) as z:
                uncompressed = sum(i.file_size for i in z.infolist())
            if uncompressed > settings.parser_max_docx_uncompressed_mb * 1024 * 1024:
                conn.send(("caps", "docx uncompressed size"))
                return
        docs = _load_local(path)
        if len(docs) > settings.parser_max_pages:  # pages cap
            docs = docs[: settings.parser_max_pages]
        remaining = settings.parser_max_chars  # extracted-characters cap
        out = []
        for d in docs:
            if remaining <= 0:
                break
            content = d.page_content[:remaining]
            remaining -= len(content)
            out.append((content, dict(d.metadata)))
        conn.send(("ok", out))
    except Exception as exc:
        conn.send(("error", type(exc).__name__))  # class only — never the body


def _spawn_and_collect(target, args, *, timeout_s: float, memory_limit_mb: int | None = None):
    """Run ``target(conn, *args)`` in a spawned process; collect its pipe
    verdict. Timeout → kill → ParseFailed('timeout'); nonzero exit, no
    verdict, or a broken pipe (incl. an OOM kill) → ParseFailed('crashed');
    a 'caps' verdict → ParseFailed('caps'). The parent always survives."""
    ctx = multiprocessing.get_context("spawn")
    parent_conn, child_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(target=_child_entry, args=(target, args, child_conn, memory_limit_mb))
    proc.start()
    child_conn.close()  # parent holds only the read end
    proc.join(timeout=timeout_s)
    if proc.is_alive():
        proc.kill()
        proc.join()
        raise ParseFailed("timeout")
    try:
        if not parent_conn.poll(timeout=5):
            raise ParseFailed("crashed")  # exited without a verdict
        status, payload = parent_conn.recv()
    except (EOFError, OSError):
        raise ParseFailed("crashed") from None  # died mid-send (OOM kill)
    if status == "ok":
        return payload
    if status == "error":
        # class only — never parser text (§A6/F4)
        logger.warning("parse subprocess failed: %s", payload)
    raise ParseFailed(status if status == "caps" else "crashed")


def _child_entry(target, args, q, memory_limit_mb) -> None:
    if memory_limit_mb is not None:
        _apply_memory_limit(memory_limit_mb)
    target(q, *args)


def _load_local(path: Path) -> list[Document]:
    """The in-child loader (the pre-A3 load_file body). Runs INSIDE the
    subprocess only."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return PyPDFLoader(str(path)).load()
    if suffix in {".txt", ".md"}:
        return TextLoader(str(path), encoding="utf-8").load()
    if suffix == ".docx":
        doc = DocxDocument(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        return [Document(page_content=text, metadata={"source": str(path)})]
    raise ValueError(f"Unsupported file type: {suffix}")


class IngestGate:
    """Global ingest concurrency cap (DECISION #7 addition): at most
    ``limit`` parse/embed runs at once, so N parallel uploads cannot starve
    the process (chat shares the same worker threads). Proven by the
    parallel-upload test in tests/test_parser_subprocess.py."""

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._sem = threading.BoundedSemaphore(self.limit)

    def __enter__(self):
        self._sem.acquire()
        return self

    def __exit__(self, *exc):
        self._sem.release()
        return False


ingest_gate = IngestGate(get_settings().ingest_concurrency)



def chunk_documents(docs: Iterable[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=120, add_start_index=True)
    return splitter.split_documents(list(docs))