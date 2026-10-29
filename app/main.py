from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from app.api.auth import router as auth_router
from app.api.deps import GateDenied, gate_denied_handler
from app.api.routes import router
from app.core.config import (
    get_settings,
    BASE_DIR,
    validate_admin_key,
    validate_chat_security,
    validate_production_settings,
)
from app.core.logging import configure_logging
from app.services.audit import init_db


configure_logging()
settings = get_settings()
validate_admin_key(settings)
validate_chat_security(settings)
validate_production_settings(settings)
# Legacy SQLite query_audit init: stays until B5 stops legacy content writes
# and the removal commit deletes the module (docs/backend-design.md §A12).
init_db()


def run_migrations_to_head() -> None:
    """Bring the app DB to the current Alembic head.

    Dev convenience (compose app sets RUN_MIGRATIONS_ON_STARTUP=true); the
    documented `alembic upgrade` deploy step remains the production path
    (§A12 change table, app/main.py). URL resolution lives in
    migrations/env.py: ALEMBIC_DATABASE_URL → DATABASE_URL.
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(BASE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BASE_DIR / "migrations"))
    # alembic.ini's logging config would rewire/disable the app's loggers.
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")


if settings.run_migrations_on_startup:
    run_migrations_to_head()


app = FastAPI(title=settings.app_name, version="1.0.0")

# Dependency-level denials render the design's bare {"code": ...} bodies
# (§A5): csrf_failed, password_change_required.
app.add_exception_handler(GateDenied, gate_denied_handler)


app.include_router(router)
app.include_router(auth_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(request, "index.html", {"app_name": settings.app_name})
