"""Request-scoped auth dependency (docs/backend-design.md §A5, B2 item 10).

Order matters and each step is deliberate:
1. cookie → sha256 → sessions lookup (unknown/expired/revoked → 401)
2. disabled-user check — a live cookie dies instantly on disable (401,
   same generic text as every other 401: no enumeration)
3. CSRF double-submit on mutating methods — cookie must equal header,
   constant-time compare (§A3); 403 {"code": "csrf_failed"}
4. forced-password-change gate — everything except GET /api/me,
   POST /api/auth/password, POST /api/auth/logout is 403
   {"code": "password_change_required"} (§A5)
5. throttled sliding renewal — rewrite idle_expires_at only after ≥10 min
   of idle budget was consumed; committed BEFORE the handler runs
"""
import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session as DBSession

from app.core.db import get_db
from app.models.session import Session as SessionRow
from app.models.user import User
from app.services import sessions

MUTATING_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
# (method, path) exemptions from the forced-change gate (§A5).
CHANGE_EXEMPT = {
    ("GET", "/api/me"),
    ("POST", "/api/auth/password"),
    ("POST", "/api/auth/logout"),
}

GENERIC_401 = "Not authenticated"


class GateDenied(Exception):
    """403 with a bare ``{"code": ...}`` body (§A5 shape).

    Raised from the dependency (a dependency that RETURNS a Response does
    not short-circuit in FastAPI — the value is handed to the endpoint);
    main.py registers the handler that renders the body.
    """

    def __init__(self, code: str):
        self.code = code


def gate_denied_handler(request: Request, exc: GateDenied) -> JSONResponse:
    return JSONResponse(status_code=403, content={"code": exc.code})


def get_current_user(
    request: Request, db: DBSession = Depends(get_db)
) -> User:
    token = request.cookies.get(sessions.session_cookie_name())
    if not token:
        raise HTTPException(status_code=401, detail=GENERIC_401)
    found = sessions.lookup_session(db, token)
    if found is None:
        raise HTTPException(status_code=401, detail=GENERIC_401)
    row, user = found
    if not user.is_active:
        raise HTTPException(status_code=401, detail=GENERIC_401)

    if request.method in MUTATING_METHODS:
        # Double-submit AND session binding: cookie and header must match
        # each other AND the HMAC expected for THIS session — a token
        # observed on another session (or forged) fails here (review fix 4).
        cookie_token = request.cookies.get(sessions.csrf_cookie_name(), "")
        header_token = request.headers.get("X-CSRF-Token", "")
        expected = sessions.csrf_token_for(row)
        if (
            not cookie_token
            or not header_token
            or not secrets.compare_digest(header_token, expected)
            or not secrets.compare_digest(cookie_token, expected)
        ):
            raise GateDenied("csrf_failed")

    if user.must_change_password and (request.method, request.url.path) not in CHANGE_EXEMPT:
        raise GateDenied("password_change_required")

    sessions.renew_if_due(db, row)
    db.commit()  # renewal persists even if the handler fails later
    request.state.session_row = row
    return user
