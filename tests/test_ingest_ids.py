"""M2 (B4 revision): deterministic DOC-SCOPED chunk IDs + delete-by-doc_id
on re-ingest (§A7). Identity moved from the source path to the document id,
so two users uploading the same filename can never collide or delete each
other's chunks."""
from langchain_core.documents import Document

import app.rag.vectorstore as vs
from app.core.config import get_settings

DOC_A = "11111111-1111-4111-8111-111111111111"
DOC_B = "22222222-2222-4222-8222-222222222222"


class _FakeIndex:
    def __init__(self):
        self.deleted_filters = []

    def delete(self, filter=None, namespace=None):
        self.deleted_filters.append((filter, namespace))


class _FakeStore:
    def __init__(self):
        self.index = _FakeIndex()
        self.added = None

    def add_documents(self, chunks, ids=None):
        self.added = (list(chunks), list(ids or []))
        return self.added[1]


def _chunk(text, source="C:\\kb\\handbook.md", start=0):
    return Document(page_content=text, metadata={"source": source, "start_index": start})


def _with_fake_store(monkeypatch):
    fake = _FakeStore()
    monkeypatch.setattr(vs, "get_vectorstore", lambda: fake)
    return fake


def _add(chunks, doc_id=DOC_A):
    return vs.add_documents(
        chunks, doc_id=doc_id, scope="org", owner_id=None, source_name="handbook.md"
    )


def test_ids_are_deterministic_across_runs(monkeypatch):
    _with_fake_store(monkeypatch)
    chunks = [_chunk("same text"), _chunk("other text", start=100)]

    ids1 = _add(chunks)
    ids2 = _add(chunks)

    assert ids1 == ids2
    assert len(ids1) == len(set(ids1))  # distinct chunks -> distinct ids


def test_changed_content_gets_new_id(monkeypatch):
    _with_fake_store(monkeypatch)

    old_id = _add([_chunk("version one text")])[0]
    new_id = _add([_chunk("version two text")])[0]

    assert old_id != new_id


def test_identical_text_in_different_documents_does_not_collide(monkeypatch):
    _with_fake_store(monkeypatch)

    ids_a = _add([_chunk("identical")], doc_id=DOC_A)
    ids_b = _add([_chunk("identical")], doc_id=DOC_B)

    assert ids_a[0] != ids_b[0]  # same filename/text in two docs never collide


def test_reingest_deletes_existing_vectors_per_doc_id(monkeypatch):
    """Replace semantics target the DOCUMENT, never a filename string."""
    fake = _with_fake_store(monkeypatch)
    ns = get_settings().pinecone_namespace

    _add([_chunk("a"), _chunk("b")], doc_id=DOC_A)

    deleted = {(f["doc_id"]["$eq"], namespace) for f, namespace in fake.index.deleted_filters}
    assert deleted == {(DOC_A, ns)}


def test_ids_align_with_chunks(monkeypatch):
    fake = _with_fake_store(monkeypatch)

    _add([_chunk("x"), _chunk("y", start=1), _chunk("z", start=2)])

    chunks, ids = fake.added
    assert len(ids) == len(chunks)
