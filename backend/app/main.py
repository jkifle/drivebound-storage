from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.api.v1.router import router as api_v1_router
from app.core.config import settings
from app.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.remote_access_enabled:
        unsafe = []
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
        if unsafe:
            raise RuntimeError("Unsafe remote-access configuration: " + "; ".join(unsafe))
    yield
    await engine.dispose()


app = FastAPI(title="Drivebound API", version="0.5.0", lifespan=lifespan)
if settings.remote_access_enabled:
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


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/v1/auth") else response.headers.get("Cache-Control", "private")
    if settings.remote_access_enabled:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response
