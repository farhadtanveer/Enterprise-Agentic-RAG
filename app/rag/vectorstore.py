import hashlib
import time
from pinecone import Pinecone , ServerlessSpec
from langchain_openai import OpenAIEmbeddings
from langchain_pinecone import PineconeVectorStore
from app.core.config import get_settings

settings = get_settings()


_embeddings = None
_vectorstore = None


EMBEDDING_DIMENSIONS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "all-minilm-l6-v2": 384,
}



def get_embedding_dimension(model_name: str | None = None) -> int:
    name = (model_name or settings.embedding_model or "").strip()
    if not name:
        raise RuntimeError("Embedding model is not configured")

    normalized = name.lower()
    if normalized in EMBEDDING_DIMENSIONS:
        return EMBEDDING_DIMENSIONS[normalized]
    if "text-embedding-3-small" in normalized:
        return 1536
    if "text-embedding-3-large" in normalized:
        return 3072
    if "text-embedding-ada-002" in normalized:
        return 1536
    if "all-minilm" in normalized:
        return 384

    raise ValueError(
        f"Unsupported embedding model '{model_name or settings.embedding_model}' for Pinecone. "
        "Add the matching dimension to EMBEDDING_DIMENSIONS."
    )



def get_embeddings():
    global _embeddings
    if _embeddings is None:
        if not settings.openai_api_key:
            raise RuntimeError("OPENAI_API_KEY is missing")
        _embeddings = OpenAIEmbeddings(
            model=settings.embedding_model,
            api_key=settings.openai_api_key,
        )
    return _embeddings




def ensure_index():
    if not settings.pinecone_api_key:
        raise RuntimeError("PINECONE_API_KEY is missing")

    desired_dimension = get_embedding_dimension()
    pc = Pinecone(api_key=settings.pinecone_api_key)
    names = [x["name"] for x in pc.list_indexes()]

    if settings.pinecone_index_name in names:
        index_info = pc.describe_index(settings.pinecone_index_name)
        current_dimension = getattr(index_info, "dimension", None)
        if current_dimension is None and isinstance(index_info, dict):
            current_dimension = index_info.get("dimension")

        if current_dimension is not None and current_dimension != desired_dimension:
            # Never delete existing data automatically. Fail loudly instead.
            raise RuntimeError(
                f"Pinecone index '{settings.pinecone_index_name}' has dimension {current_dimension}, "
                f"but embedding model '{settings.embedding_model}' requires {desired_dimension}. "
                "Existing vectors were NOT touched. Fix EMBEDDING_MODEL, or manually delete the "
                "index (Pinecone console or pc.delete_index) and re-run ingestion."
            )

    if settings.pinecone_index_name not in [x["name"] for x in pc.list_indexes()]:
        pc.create_index(
            name=settings.pinecone_index_name,
            dimension=desired_dimension,
            metric="cosine",
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        deadline = time.monotonic() + settings.index_ready_timeout_seconds
        while not pc.describe_index(settings.pinecone_index_name).status["ready"]:
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"Pinecone index '{settings.pinecone_index_name}' not ready after "
                    f"{settings.index_ready_timeout_seconds}s"
                )
            time.sleep(1)

    return pc.Index(settings.pinecone_index_name)





def get_vectorstore():
    global _vectorstore
    if _vectorstore is None:
        index = ensure_index()
        _vectorstore = PineconeVectorStore(
            index=index,
            embedding=get_embeddings(),
            namespace=settings.pinecone_namespace,
        )
    return _vectorstore



def get_retriever(filter: dict | None = None):
    """B3: optional metadata filter (Pinecone $-syntax, §A7). B4 makes this
    parameter REQUIRED and builds the ACL filter server-side per user."""
    search_kwargs: dict = {"k": settings.top_k}
    if filter:
        search_kwargs["filter"] = filter
    return get_vectorstore().as_retriever(search_kwargs=search_kwargs)



def _chunk_id(chunk) -> str:
    # Deterministic: re-ingesting unchanged content upserts over itself.
    # Source is part of the hash so identical text in different documents
    # can never collide. start_index disambiguates repeated chunks within
    # one document.
    source = str(chunk.metadata.get("source", ""))
    start = str(chunk.metadata.get("start_index", ""))
    return hashlib.sha256(f"{source}\x00{start}\x00{chunk.page_content}".encode("utf-8")).hexdigest()


def add_documents(chunks):
    store = get_vectorstore()
    # Replace semantics: wipe each source's existing vectors before adding,
    # so a changed re-upload doesn't leave stale chunks behind.
    sources = {str(c.metadata.get("source", "")) for c in chunks}
    for src in sources:
        if src:
            store.index.delete(
                filter={"source": {"$eq": src}},
                namespace=settings.pinecone_namespace,
            )
    ids = [_chunk_id(c) for c in chunks]
    return store.add_documents(chunks, ids=ids)