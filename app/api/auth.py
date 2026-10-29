"""Auth endpoints (docs/backend-design.md §A8) — B2 items 7/8.

Responses on the auth surface always send ``Cache-Control: no-store``
(B2 requirement 4). Login defends against enumeration in TEXT (one shared
generic message), in TIMING (dummy argon2id verify for unknown users), and
in the audit trail (known users: actor id only; unknown: no identifier
at all — §A6/§A11.13).
"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DBSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.models.user import User
from app.services import login_throttle, passwords, sessions, users
from app.services.audit_events import record_event
from app.services.ratelimit import SlidingWindowLimiter

router = APIRouter(prefix="/api", tags=["auth"])
settings = get_settings()
login_limiter = SlidingWindowLimiter(settings.login_rate_limit_per_minute)

LOGIN_ROUTE = "/api/auth/login"
PASSWORD_ROUTE = "/api/auth/password"
LOGOUT_ROUTE = "/api/auth/logout"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class PasswordChangeRequest(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=1, max_length=128)


def _user_payload(user: User) -> dict:
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "must_change_password": user.must_change_password,
    }


def _client_ip(request: Request) -> str:
    # socket peer IP (no X-Forwarded-For trust — §A3); the limiter falls
    # back to "unknown" when Starlette has no client address.
    return request.client.host if request.client else "unknown"


def _set_auth_cookies(response: Response, token: str, csrf_token: str) -> None:
    max_age = settings.session_absolute_days * 86400
    secure = settings.cookie_secure
    response.set_cookie(
        sessions.SESSION_COOKIE, token, max_age=max_age,
        httponly=True, samesite="lax", secure=secure, path="/",
    )
    response.set_cookie(
        sessions.CSRF_COOKIE, csrf_token, max_age=max_age,
        httponly=False, samesite="lax", secure=secure, path="/",
    )


@router.post("/auth/login")
def login(
    payload: LoginRequest, request: Request, response: Response, db: DBSession = Depends(get_db)
) -> dict:
    ip = _client_ip(request)
    if settings.login_rate_limit_per_minute > 0 and not login_limiter.allow(ip):
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again shortly.",
            headers={"Retry-After": "60"},
        )

    username = users.normalize_username(payload.username)
    now = sessions.utcnow()

    remaining = login_throttle.remaining_seconds(db, username, ip, now=now)
    if remaining > 0:
        record_event(db, "login_throttled", ip=ip, outcome="denied", route=LOGIN_ROUTE)
        db.commit()
        raise HTTPException(
            status_code=429,
            detail="Too many failed attempts. Try again later.",
            headers={"Retry-After": str(remaining)},
        )

    user = users.get_by_username(db, username)
    if user is None:
        # Timing equalization: the same argon2id work happens here as for a
        # real account (B2 requirement 1). No identifier reaches the audit
        # trail — not even the typed username (§A6/§A11.13).
        passwords.verify_dummy(payload.password)
        login_throttle.record_failure(db, username, ip, now=now)
        record_event(db, "login_failed", ip=ip, outcome="denied", route=LOGIN_ROUTE)
        db.commit()
        raise HTTPException(status_code=401, detail=passwords.GENERIC_LOGIN_FAILED)

    if not passwords.verify_password(payload.password, user.password_hash):
        login_throttle.record_failure(db, username, ip, now=now)
        # Known user: actor_user_id only — never the raw username (§A6).
        record_event(
            db, "login_failed", actor_user_id=user.id, ip=ip, outcome="denied", route=LOGIN_ROUTE
        )
        db.commit()
        raise HTTPException(status_code=401, detail=passwords.GENERIC_LOGIN_FAILED)

    if not user.is_active:
        # Correct credentials, disabled account: same generic text, and the
        # failure still counts (else lockout behavior would leak that the
        # password was right — an oracle on disabled accounts).
        login_throttle.record_failure(db, username, ip, now=now)
        record_event(
            db,
            "login_failed",
            actor_user_id=user.id,
            ip=ip,
            outcome="denied",
            route=LOGIN_ROUTE,
            details={"disabled": True},
        )
        db.commit()
        raise HTTPException(status_code=401, detail=passwords.GENERIC_LOGIN_FAILED)

    login_throttle.reset(db, username, ip)
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(payload.password)
    # Every login issues a NEW session (no reuse) — §A3/B2 requirement 2.
    token = sessions.create_session(
        db, user.id, ip=ip, user_agent=request.headers.get("user-agent")
    )
    csrf_token = secrets.token_urlsafe(32)
    record_event(db, "login", actor_user_id=user.id, ip=ip, outcome="ok", route=LOGIN_ROUTE)
    db.commit()

    _set_auth_cookies(response, token, csrf_token)
    response.headers["Cache-Control"] = "no-store"
    return {"user": _user_payload(user), "csrf_token": csrf_token}


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    sessions.revoke_session(db, request.state.session_row)
    record_event(
        db, "logout", actor_user_id=user.id, ip=_client_ip(request), outcome="ok", route=LOGOUT_ROUTE
    )
    db.commit()
    response.delete_cookie(sessions.SESSION_COOKIE, path="/")
    response.delete_cookie(sessions.CSRF_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return {"status": "logged_out"}


@router.post("/auth/password")
def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: DBSession = Depends(get_db),
) -> dict:
    if not passwords.verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Incorrect old password")
    try:
        users.change_own_password(
            db, user, payload.new_password, keep_session_id=request.state.session_row.id
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {"status": "password_changed", "user": _user_payload(user)}


@router.get("/me")
def me(response: Response, user: User = Depends(get_current_user)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"user": _user_payload(user)}
