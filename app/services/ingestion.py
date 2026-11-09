import codecs
from pathlib import Path
from typing import Callable, Iterable
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from docx import Document as DocxDocument


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



def chunk_documents(docs: Iterable[Document]) -> list[Document]:
    splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=120, add_start_index=True)
    return splitter.split_documents(list(docs))