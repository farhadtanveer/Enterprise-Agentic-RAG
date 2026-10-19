"""M2: deterministic chunk IDs + delete-by-source on re-ingest."""
from langchain_core.documents import Document

import app.rag.vectorstore as vs
from app.core.config import get_settings


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


def test_ids_are_deterministic_across_runs(monkeypatch):
    fake = _with_fake_store(monkeypatch)
    chunks = [_chunk("same text"), _chunk("other text", start=100)]

    ids1 = vs.add_documents(chunks)
    ids2 = vs.add_documents(chunks)

    assert ids1 == ids2
    assert len(ids1) == len(set(ids1))  # distinct chunks -> distinct ids


def test_changed_content_gets_new_id(monkeypatch):
    fake = _with_fake_store(monkeypatch)

    old_id = vs.add_documents([_chunk("version one text")])[0]
    new_id = vs.add_documents([_chunk("version two text")])[0]

    assert old_id != new_id


def test_identical_text_in_different_documents_does_not_collide(monkeypatch):
    fake = _with_fake_store(monkeypatch)

    ids = vs.add_documents([_chunk("identical", source="a.md"), _chunk("identical", source="b.md")])

    assert ids[0] != ids[1]


def test_reingest_deletes_existing_vectors_per_source(monkeypatch):
    fake = _with_fake_store(monkeypatch)
    ns = get_settings().pinecone_namespace

    vs.add_documents([_chunk("a", source="a.md"), _chunk("b", source="b.md")])

    deleted = {(f["source"]["$eq"], namespace) for f, namespace in fake.index.deleted_filters}
    assert deleted == {("a.md", ns), ("b.md", ns)}


def test_ids_align_with_chunks(monkeypatch):
    fake = _with_fake_store(monkeypatch)

    vs.add_documents([_chunk("x"), _chunk("y", start=1), _chunk("z", start=2)])

    chunks, ids = fake.added
    assert len(ids) == len(chunks)
