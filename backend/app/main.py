import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import router as api_v1_router
from app.core.config import settings
from app.core.observability import configure_logging, log_request, metrics, request_timer
from app.db.session import engine


def runtime_configuration_errors() -> list[str]:
    unsafe: list[str] = []
    public_access = settings.remote_access_enabled or settings.production_like
    if public_access:
        if not settings.app_url.startswith("https://") or not settings.api_url.startswith("https://"):
            unsafe.append("APP_URL and API_URL must use HTTPS")
        if not settings.auth_cookie_secure:
            unsafe.append("AUTH_COOKIE_SECURE must be true")
        if settings.jwt_secret.startswith("change-") or len(settings.jwt_secret) < 32:
            unsafe.append("JWT_SECRET must be replaced with a long random value")
        if settings.mfa_encryption_secret.startswith("change-") or len(settings.mfa_encryption_secret) < 32:
            unsafe.append("MFA_ENCRYPTION_SECRET must be replaced with a long random value")
        if "*" in settings.cors_origin_list:
            unsafe.append("CORS_ORIGINS cannot contain a wildcard")
        if "*" in settings.trusted_host_list:
            unsafe.append("TRUSTED_HOSTS cannot contain a wildcard")
    if settings.production_like:
        if not settings.remote_access_enabled:
            unsafe.append("REMOTE_ACCESS_ENABLED must be true")
        if not settings.uses_file_backed_secrets:
            unsafe.append("staging and production require file-backed database, Redis, JWT, MFA, and media-encryption secrets")
        if not settings.media_encryption_enabled:
            unsafe.append("MEDIA_ENCRYPTION_ENABLED must be true")
        if settings.media_encryption_master_key == "RZAIpFM5CAciKTy4UABCTvvyD83LfeB9r_ZR4z0LnKQ=":
            unsafe.append("MEDIA_ENCRYPTION_MASTER_KEY must not use the development key")
        if settings.deployment_mode == "hybrid" and not (settings.control_plane_url or "").startswith("https://"):
            unsafe.append("hybrid deployments require an HTTPS CONTROL_PLANE_URL")
    try:
        settings.media_encryption_key
    except RuntimeError as exc:
        unsafe.append(str(exc))
    return unsafe


@asynccontextmanager
async def lifespan(app: FastAPI):
    unsafe = runtime_configuration_errors()
    if unsafe:
        raise RuntimeError("Unsafe deployment configuration: " + "; ".join(unsafe))
    configure_logging(settings.observability_log_json)
    yield
    await engine.dispose()


app = FastAPI(title="Drivebound API", version="0.5.0", lifespan=lifespan)
if settings.remote_access_enabled or settings.production_like:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Upload-Offset", "Upload-Length", "Upload-Status"],
)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(metrics.prometheus(), media_type="text/plain; version=0.0.4; charset=utf-8")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    started_at = request_timer()
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    traceparent = request.headers.get("traceparent")
    try:
        response = await call_next(request)
    except Exception:
        elapsed = request_timer() - started_at
        metrics.observe(request.method, request.url.path, 500, elapsed)
        log_request(request_id=request_id, traceparent=traceparent, method=request.method, path=request.url.path, status_code=500, elapsed_seconds=elapsed)
        raise
    elapsed = request_timer() - started_at
    metrics.observe(request.method, request.url.path, response.status_code, elapsed)
    log_request(request_id=request_id, traceparent=traceparent, method=request.method, path=request.url.path, status_code=response.status_code, elapsed_seconds=elapsed)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/v1/auth") else response.headers.get("Cache-Control", "private")
    if settings.remote_access_enabled:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
