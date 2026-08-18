import uuid
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import router as api_v1_router
from app.core.config import settings
from app.core.observability import configure_logging, log_request, metrics, request_timer
from app.db.session import engine


def runtime_configuration_errors() -> list[str]:
    unsafe: list[str] = []
    unsafe.extend(settings.webauthn_configuration_errors)
    public_access = settings.remote_access_enabled or settings.production_like
    if public_access:
        if not settings.app_url.startswith("https://") or not settings.api_url.startswith("https://"):
            unsafe.append("APP_URL and API_URL must use HTTPS")
        if not settings.auth_cookie_secure:
            unsafe.append("AUTH_COOKIE_SECURE must be true")
        if not settings.auth_cookie_name.startswith("__Host-"):
            unsafe.append("AUTH_COOKIE_NAME must use the __Host- prefix for public access")
        if not settings.refresh_cookie_name.startswith("__Host-"):
            unsafe.append("REFRESH_COOKIE_NAME must use the __Host- prefix for public access")
        if settings.auth_cookie_name == settings.refresh_cookie_name:
            unsafe.append("AUTH_COOKIE_NAME and REFRESH_COOKIE_NAME must be different")
        if settings.jwt_secret.startswith("change-") or len(settings.jwt_secret) < 32:
            unsafe.append("JWT_SECRET must be replaced with a long random value")
        if settings.mfa_encryption_secret.startswith("change-") or len(settings.mfa_encryption_secret) < 32:
            unsafe.append("MFA_ENCRYPTION_SECRET must be replaced with a long random value")
        if "*" in settings.cors_origin_list:
            unsafe.append("CORS_ORIGINS cannot contain a wildcard")
        if "*" in settings.trusted_host_list:
            unsafe.append("TRUSTED_HOSTS cannot contain a wildcard")
        try:
            proxy_networks = settings.trusted_proxy_networks
        except ValueError:
            unsafe.append("TRUSTED_PROXY_CIDRS contains an invalid network")
        else:
            if any(network.prefixlen == 0 for network in proxy_networks):
                unsafe.append("TRUSTED_PROXY_CIDRS cannot trust the entire internet")
        if not settings.metrics_auth_token or len(settings.metrics_auth_token) < 32:
            unsafe.append("METRICS_AUTH_TOKEN must be a long random value")
        if settings.email_delivery_mode != "smtp" or not settings.smtp_host:
            unsafe.append("public access requires EMAIL_DELIVERY_MODE=smtp and SMTP_HOST")
        elif not settings.smtp_use_tls:
            unsafe.append("public SMTP delivery requires SMTP_USE_TLS=true")
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
        if settings.metrics_auth_token_file is None:
            unsafe.append("staging and production require a file-backed metrics token")
        if settings.smtp_username and settings.smtp_password_file is None:
            unsafe.append("staging and production require a file-backed SMTP password when SMTP authentication is used")
        if settings.google_auth_enabled and settings.google_client_secret_file is None:
            unsafe.append("staging and production require a file-backed Google client secret")
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


class RequestBodyTooLarge(Exception):
    pass


class BoundedRequestBodyMiddleware:
    """Bound JSON/control-plane bodies while preserving streaming media uploads."""

    def __init__(self, app, limit: int = 128 * 1024):
        self.app = app
        self.limit = limit

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        exempt = path.startswith("/api/v1/uploads") or path == "/api/v1/assets/upload"
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        length = headers.get(b"content-length")
        if not exempt and length:
            try:
                oversized = int(length) > self.limit
            except ValueError:
                oversized = True
            if oversized:
                await JSONResponse(status_code=413, content={"detail": "Request body is too large"})(scope, receive, send)
                return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if not exempt and message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.limit:
                    raise RequestBodyTooLarge
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except RequestBodyTooLarge:
            await JSONResponse(status_code=413, content={"detail": "Request body is too large"})(scope, receive, send)


app.add_middleware(BoundedRequestBodyMiddleware)
if settings.remote_access_enabled or settings.production_like:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_host_list)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Upload-Offset", "Upload-Length", "Upload-Status", "X-Drivebound-Reauthentication"],
)
app.include_router(api_v1_router, prefix="/api/v1")


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> PlainTextResponse:
    expected = settings.metrics_auth_token
    if expected:
        scheme, _, credential = request.headers.get("Authorization", "").partition(" ")
        authorized = scheme.lower() == "bearer" and bool(credential) and secrets.compare_digest(credential, expected)
        if not authorized:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Metrics authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return PlainTextResponse(
        metrics.prometheus(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


def route_label(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path else "/unmatched"


@app.middleware("http")
async def security_headers(request: Request, call_next):
    started_at = request_timer()
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    traceparent = request.headers.get("traceparent")
    public_access = settings.remote_access_enabled or settings.production_like
    unsafe_method = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    cookie_authenticated = bool(
        request.cookies.get(settings.auth_cookie_name)
        or request.cookies.get(settings.refresh_cookie_name)
        or request.cookies.get(
            "__Host-drivebound_google_mfa" if settings.auth_cookie_secure else "drivebound_google_mfa"
        )
    )
    authorization_scheme = request.headers.get("Authorization", "").partition(" ")[0].lower()
    if public_access and unsafe_method and cookie_authenticated and authorization_scheme != "bearer":
        origin = request.headers.get("Origin", "").rstrip("/")
        app = urlsplit(settings.app_url)
        app_origin = f"{app.scheme}://{app.netloc}".rstrip("/")
        allowed_origins = {item.rstrip("/") for item in settings.cors_origin_list}
        allowed_origins.add(app_origin)
        if not origin or origin not in allowed_origins:
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
    try:
        response = await call_next(request)
    except Exception:
        elapsed = request_timer() - started_at
        path = route_label(request)
        metrics.observe(request.method, path, 500, elapsed)
        log_request(request_id=request_id, traceparent=traceparent, method=request.method, path=path, status_code=500, elapsed_seconds=elapsed)
        raise
    elapsed = request_timer() - started_at
    path = route_label(request)
    metrics.observe(request.method, path, response.status_code, elapsed)
    log_request(request_id=request_id, traceparent=traceparent, method=request.method, path=path, status_code=response.status_code, elapsed_seconds=elapsed)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/v1/auth") else response.headers.get("Cache-Control", "private")
    if settings.remote_access_enabled:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
