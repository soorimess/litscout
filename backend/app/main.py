"""ASGI app: API routers, security headers, CSRF check, the job runner and the built frontend."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .config import WebSettings, get_settings
from .db import Database
from .routes import auth, jobs, results
from .runner import JobRunner
from .security import RateLimiter

log = logging.getLogger("litscout.web")
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "LitScout"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")


def create_app(settings: Optional[WebSettings] = None, runner: Optional[JobRunner] = None,
               background: bool = True) -> FastAPI:
    s = settings or get_settings()
    db = Database(s.db_url)
    db.create_all()
    job_runner = runner or JobRunner(s, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        if background:
            job_runner.start_background()
        yield
        job_runner.stop()

    app = FastAPI(title="LitScout", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings, app.state.db, app.state.runner = s, db, job_runner
    app.state.login_limiter = RateLimiter(s.login_attempts_per_minute)

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        # Cookies are SameSite=Strict; requiring a custom header on writes also stops cross-site forms.
        if request.method in UNSAFE and request.url.path.startswith("/api/") \
                and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return JSONResponse({"detail": "Missing request header"}, status_code=403)
        response: Response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    app.include_router(auth.router)
    app.include_router(jobs.router)
    app.include_router(results.router)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    dist = s.frontend_dist
    if (dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        f = (dist / path).resolve()
        if path and f.is_file() and dist.resolve() in f.parents:
            return FileResponse(f)
        index = dist / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return Response("Frontend not built: run `npm run build` in frontend/.", media_type="text/plain", status_code=503)

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()
