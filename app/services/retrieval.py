"""Server-side retrieval filters (§A7) — B4.

The filter is ALWAYS built here, from authenticated identity — the client
sends nothing and no code path retrieves unfiltered (get_retriever now
refuses a missing filter).

Pinecone array semantics: a vector's ``read_access`` is ``["org"]`` for org
docs or ``[<owner_id>]`` for private docs; ``$in`` matches if ANY element
matches, so one condition expresses ``scope == org OR owner == me``.
"""

ORG_ONLY_FILTER = {"read_access": {"$in": ["org"]}}
# D8: legacy /api/chat holds no identity — a static key proves nothing, so
# legacy retrieval is org-only from B4 on.


def retrieval_filter_for(user) -> dict:
    """Authenticated users get org docs + their own private docs."""
    return {"read_access": {"$in": ["org", str(user.id)]}}
