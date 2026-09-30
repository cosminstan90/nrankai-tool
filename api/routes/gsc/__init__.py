"""GSC routes package — Google Search Console integration."""

from fastapi import APIRouter
from .properties import router as properties_router
from .oauth_sync import router as oauth_sync_router
from .optimizer import router as optimizer_router
from .url_inspection import router as url_inspection_router
from .opportunities import router as opportunities_router
from .decay import router as decay_router

router = APIRouter()
router.include_router(properties_router)
router.include_router(oauth_sync_router)
router.include_router(optimizer_router)
router.include_router(url_inspection_router)
router.include_router(opportunities_router)
router.include_router(decay_router)

__all__ = ["router"]
