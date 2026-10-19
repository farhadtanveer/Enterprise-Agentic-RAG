"""H3: ensure_index must never auto-delete, and must time out."""
import pytest

import app.rag.vectorstore as vs
from app.core.config import get_settings


class _FakeIndexInfo:
    def __init__(self, dimension, ready=True):
        self.dimension = dimension
        self.status = {"ready": ready}


class _FakePinecone:
    def __init__(self, names, dimension):
        self._names = names
        self._dimension = dimension
        self.delete_called = False
        self.create_called = False

    def list_indexes(self):
        return [{"name": n} for n in self._names]

    def describe_index(self, name):
        return _FakeIndexInfo(self._dimension, ready=False)

    def delete_index(self, name):
        self.delete_called = True

    def create_index(self, **kwargs):
        self.create_called = True

    def Index(self, name):
        return object()


def test_dimension_mismatch_raises_and_never_deletes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "pinecone_api_key", "test-key")
    monkeypatch.setattr(s, "embedding_model", "text-embedding-3-small")  # needs 1536
    fake = _FakePinecone([s.pinecone_index_name], dimension=3072)
    monkeypatch.setattr(vs, "Pinecone", lambda api_key: fake)

    with pytest.raises(RuntimeError, match="dimension"):
        vs.ensure_index()

    assert fake.delete_called is False
    assert fake.create_called is False


def test_index_creation_times_out(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "pinecone_api_key", "test-key")
    monkeypatch.setattr(s, "index_ready_timeout_seconds", 1)
    fake = _FakePinecone([], dimension=1536)  # not listed -> create path, never ready
    monkeypatch.setattr(vs, "Pinecone", lambda api_key: fake)

    with pytest.raises(RuntimeError, match="not ready"):
        vs.ensure_index()

    assert fake.create_called is True
