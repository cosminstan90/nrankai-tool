"""API route modules (master unified build)."""

from .pages import router as pages_router
from .audits import router as audits_router
from .results import router as results_router
from .health import router as health_router
from .compare import router as compare_router
from .summary import router as summary_router
from .benchmarks import router as benchmarks_router
from .schedules import router as schedules_router
from .geo_monitor import router as geo_monitor_router
from .content_briefs import router as content_briefs_router
from .pdf_reports import router as pdf_reports_router
from .schema_gen import router as schema_gen_router
from .citation_tracker import router as citation_tracker_router
from .portfolio import router as portfolio_router
from .costs import router as costs_router
from .gap_analysis import router as gap_analysis_router
from .content_gaps import router as content_gaps_router
from .action_cards import router as action_cards_router
from .templates_manager import router as templates_manager_router
from .tracking import router as tracking_router
from .cross_reference import router as cross_reference_router
from .settings import router as settings_router
from .notes import router as notes_router
from .keyword_research import router as keyword_research_router
from .gsc import router as gsc_router
from .ga4 import router as ga4_router
from .ads import router as ads_router
from .insights import router as insights_router
from .llms_txt import router as llms_txt_router
from .guide import router as guide_router

__all__ = [name for name in globals() if name.endswith('_router')]
