"""
HTML page routes — split into domain submodules.
"""
from fastapi import APIRouter

from .dashboard import router as _dashboard_router
from .settings_views import router as _settings_router
from .audit_views import router as _audit_router
from .tool_views import router as _tool_router
from .integration_views import router as _integration_router
from .analytics_views import router as _analytics_router

router = APIRouter()
router.include_router(_dashboard_router)
router.include_router(_settings_router)
router.include_router(_audit_router)
router.include_router(_tool_router)
router.include_router(_integration_router)
router.include_router(_analytics_router)
