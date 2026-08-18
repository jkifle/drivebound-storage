from fastapi import APIRouter

from app.api.v1.health import router as health_router
from app.api.v1.assets import router as assets_router
from app.api.v1.libraries import router as libraries_router
from app.api.v1.storage_status import router as storage_router
from app.api.v1.uploads import router as uploads_router
from app.api.v1.auth import router as auth_router
from app.api.v1.passkeys import router as passkeys_router
from app.api.v1.devices import router as devices_router
from app.api.v1.discovery import router as discovery_router
from app.api.v1.shares import router as shares_router
from app.api.v1.intelligence import router as intelligence_router
from app.api.v1.monitoring import router as monitoring_router
from app.api.v1.nodes import router as nodes_router
from app.api.v1.sync import router as sync_router
from app.api.v1.media_groups import router as media_groups_router

router = APIRouter()
router.include_router(health_router)
router.include_router(assets_router)
router.include_router(uploads_router)
router.include_router(libraries_router)
router.include_router(storage_router)
router.include_router(auth_router)
router.include_router(passkeys_router)
router.include_router(devices_router)
router.include_router(discovery_router)
router.include_router(shares_router)
router.include_router(intelligence_router)
router.include_router(monitoring_router)
router.include_router(nodes_router)
router.include_router(sync_router)
router.include_router(media_groups_router)
