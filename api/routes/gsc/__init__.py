"""GSC routes package — Google Search Console integration."""

from fastapi import APIRouter
from .properties import router as properties_router
from .oauth_sync import router as oauth_sync_router
from .optimizer import router as optimizer_router

router = APIRouter()
router.include_router(properties_router)
router.include_router(oauth_sync_router)
router.include_router(optimizer_router)

__all__ = ["router"]
