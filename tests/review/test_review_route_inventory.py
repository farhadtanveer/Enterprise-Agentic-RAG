"""Route inventory (review task 2): every route must be either explicitly
allowlisted as unauthenticated or behind get_current_user.

This is a NEGATIVE test on future routes: any endpoint added later without
the auth dependency (and without being added to the reviewed allowlist)
fails here. The allowlist documents WHY each unauthenticated route is
acceptable:
- GET  /                     — static HTML shell (no data)
- GET  /api/health           — liveness probe, no data
- POST /api/chat             — legacy static-key chat (X-API-Key; D8)
- POST /api/ingest           — legacy admin ingest (X-Admin-Key; D8)
- POST /api/auth/login       — credentials themselves are the auth
- GET  /openapi.json|/docs|/docs/oauth2-redirect|/redoc — FastAPI defaults
"""
import inspect

from fastapi.routing import APIRoute
from starlette.routing import Route as StarletteRoute

from app.api.deps import get_current_user
from app.main import app as fastapi_app
from app.services import conversations as svc, users

PASSWORD = "a valid longer passphrase"


def make_user(db, username, **kw):
    return users.create_user(db, username, password=PASSWORD, **kw)


def login(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]

# (method, path) pairs that intentionally do NOT use session auth.
UNAUTHENTICATED_ALLOWLIST = {
    ("GET", "/"),
    ("GET", "/api/health"),
    ("POST", "/api/chat"),
    ("POST", "/api/ingest"),
    ("POST", "/api/auth/login"),
    ("GET", "/openapi.json"),
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
}


def _iter_routes():
    """Walk the app's route tree. Newer FastAPI nests routers
    (``_IncludedRouter`` wrapping ``original_router``), so recurse into both
    ``.routes`` and ``.original_router.routes``; APIRoute leaves yield their
    (method, path). HEAD is skipped: Starlette derives it implicitly from
    GET and never routes it to a separate handler."""
    seen = set()
    stack = list(fastapi_app.routes)
    while stack:
        route = stack.pop()
        if id(route) in seen:
            continue
        seen.add(id(route))
        methods = None
        if isinstance(route, APIRoute):
            methods = route.methods
        elif isinstance(route, StarletteRoute) and getattr(route, "methods", None):
            # the plain docs/openapi/redoc routes — no FastAPI dependency tree
            methods = route.methods
        if methods is not None:
            for method in methods:
                if method != "HEAD":
                    yield method, route.path, route
            continue
        if hasattr(route, "original_router"):
            stack.extend(route.original_router.routes)
        elif hasattr(route, "routes"):
            stack.extend(route.routes)


def _dependency_calls(dependant):
    """All callables reachable from this FastAPI dependency tree."""
    yield dependant.call
    for dep in dependant.dependencies:
        yield from _dependency_calls(dep)


def _uses_session_auth(route) -> bool:
    return any(
        call is get_current_user or inspect.isfunction(call)
        and getattr(call, "__module__", "") == "app.api.deps"
        and call.__name__ == "get_current_user"
        for call in _dependency_calls(route.dependant)
    )


def test_inventory_snapshot_matches_reviewed_allowlist():
    """Pin the FULL inventory: a new route must either carry session auth or
    be consciously added to the allowlist after review."""
    inventory = {(m, p) for m, p, _ in _iter_routes()}
    assert inventory == UNAUTHENTICATED_ALLOWLIST | {
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/password"),
        ("GET", "/api/me"),
        ("GET", "/api/conversations"),
        ("POST", "/api/conversations"),
        ("GET", "/api/conversations/{conversation_id}"),
        ("PATCH", "/api/conversations/{conversation_id}"),
        ("DELETE", "/api/conversations/{conversation_id}"),
        ("GET", "/api/conversations/{conversation_id}/messages"),
        ("POST", "/api/conversations/{conversation_id}/messages"),
    }


def test_every_non_allowlisted_route_requires_session_auth():
    """THE review check: fails if any route escapes both the allowlist and
    get_current_user (e.g. a new endpoint wired without the dependency)."""
    unauthenticated = []
    for method, path, route in _iter_routes():
        if (method, path) in UNAUTHENTICATED_ALLOWLIST:
            continue
        if not _uses_session_auth(route):
            unauthenticated.append(f"{method} {path}")
    assert not unauthenticated, (
        f"routes without session auth and not on the allowlist: {unauthenticated}"
    )


def test_admin_role_confers_no_conversation_access(client, db_session):
    """No query-by-id without an owner filter may exist on the conversation
    surface — an ADMIN poking another user's conversation id is 404, exactly
    like any other outsider (§A8 404-not-403; admin conversation reads are a
    must-NOT-exist endpoint per the design's 'endpoints that must not exist')."""
    victim = make_user(db_session, "idor-victim")
    conv = svc.create_conversation(db_session, victim.id)
    db_session.commit()

    make_user(db_session, "the-admin", role="admin")
    db_session.commit()

    csrf = login(client, "the-admin")
    base = f"/api/conversations/{conv.id}"
    assert client.get(base).status_code == 404
    assert client.get(f"{base}/messages").status_code == 404
    assert client.patch(base, json={"title": "x"}, headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.delete(base, headers={"X-CSRF-Token": csrf}).status_code == 404
    assert client.post(
        f"{base}/messages", json={"question": "mine?"}, headers={"X-CSRF-Token": csrf}
    ).status_code == 404
