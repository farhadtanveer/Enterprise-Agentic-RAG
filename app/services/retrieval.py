"""Server-side retrieval filters (§A7/§A14.3).

Authenticated filters are built by the AccessResolver
(app.services.access.DbAccessResolver) since G1.2 — org + active group
tokens + the user's own private token, recomputed from the DB on every
request. This module keeps only the legacy D8 constant: a static key
proves nothing, so legacy /api/chat retrieval is org-only.

Pinecone array semantics: a vector's ``read_access`` lists STRICTLY
prefixed tokens ("org", "group:<id>", "user:<id>" — DECISION #9); ``$in``
matches if ANY element matches.
"""

ORG_ONLY_FILTER = {"read_access": {"$in": ["org"]}}
# D8: legacy /api/chat holds no identity — a static key proves nothing, so
# legacy retrieval is org-only from B4 on.
