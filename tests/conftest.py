"""Test bootstrap: force offline, keyless settings before any app import."""
import os
import tempfile

# Env vars beat the project .env in pydantic-settings. Empty keys mean any
# accidental real network call raises instead of reaching out.
os.environ["OPENAI_API_KEY"] = ""
os.environ["TAVILY_API_KEY"] = ""
os.environ["PINECONE_API_KEY"] = ""
# Keep the real data/audit.db untouched by tests.
os.environ["AUDIT_DB_PATH"] = os.path.join(tempfile.gettempdir(), "audit-test.db")
