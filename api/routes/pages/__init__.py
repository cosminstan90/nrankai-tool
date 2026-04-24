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
from .projects_views import router as _projects_router
from .contentiq_views import router as _contentiq_router
from .meta_generator import router as _meta_generator_router
from .ai_visibility import router as _ai_visibility_router

router = APIRouter()
router.include_router(_dashboard_router)
router.include_router(_settings_router)
router.include_router(_audit_router)
router.include_router(_tool_router)
router.include_router(_integration_router)
router.include_router(_analytics_router)
router.include_router(_projects_router)
router.include_router(_contentiq_router)
router.include_router(_meta_generator_router)
router.include_router(_ai_visibility_router)
