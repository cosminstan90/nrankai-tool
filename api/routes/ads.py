"""
Google Ads Data Integration.

Supports manual CSV upload from Google Ads search terms and campaigns report exports.
Auto-detects report type (search terms vs campaigns) from column headers.
Provides cross-reference views against GSC query data to identify paid/organic overlaps.

Endpoints
---------
POST   /api/ads/accounts                        create an account
GET    /api/ads/accounts                        list all accounts
DELETE /api/ads/accounts/{id}                   delete account + all its data
POST   /api/ads/accounts/{id}/upload            upload search-terms OR campaigns CSV (auto-detected)
GET    /api/ads/accounts/{id}/search-terms      paginated + filtered search term rows
GET    /api/ads/accounts/{id}/campaigns         paginated + filtered campaign rows
GET    /api/ads/accounts/{id}/cross-reference   cross-reference with GSC queries (paid/organic overlap)
"""

import csv
import io
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from api.limiter import limiter
from api.utils.errors import raise_not_found
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy import delete as sql_delete

from api.models.database import (
    AsyncSessionLocal,
    AdsAccount,
    AdsSearchTermRow,
    AdsCampaignRow,
    GscQueryRow,
)

router = APIRouter(prefix="/api/ads", tags=["ads"])

_MAX_CSV_BYTES = 20 * 1024 * 1024  # 20 MB


# ── Pydantic models ───────────────────────────────────────────────────────────

class CreateAccountRequest(BaseModel):
    name:       str
    account_id: str = ""
    currency:   str = ""


# ── CSV parsing ───────────────────────────────────────────────────────────────

def _parse_ads_csv(content: bytes) -> tuple[str, list[dict]]:
    """
    Parse a Google Ads CSV export (Search Terms or Campaigns report).

    Handles:
    - UTF-8 BOM (Windows exports)
    - 'Search term' / 'Keyword' column names    → report_type = 'search_terms'
    - 'Campaign' column as first column          → report_type = 'campaigns'
    - CTR formatted as "5.23%" → stored as 0.0523
    - Cost fields may contain currency symbols   → stripped before parsing
    - Comma-separated integers ("1,234" → 1234)

    Returns (report_type, rows).
    """
    # utf-8-sig strips BOM if present; fall back to latin-1 for legacy exports
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")

    # Skip Google Ads summary/totals rows (lines starting with "Total" or blank)
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("total") or stripped == "":
            continue
        lines.append(line)

    text = "\n".join(lines)
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    if not fieldnames:
        raise ValueError("Empty or invalid CSV file — no column headers found.")

    # Normalise column names for matching
    col_norm = {f.strip().lower(): f for f in fieldnames}

    # Detect report type from first column
    first_col_lower = (fieldnames[0] or "").strip().lower()
    TERM_HEADERS     = {"search term", "search terms", "keyword", "query", "search query"}
    CAMPAIGN_HEADERS = {"campaign", "campaign name"}

    if first_col_lower in TERM_HEADERS:
        report_type = "search_terms"
    elif first_col_lower in CAMPAIGN_HEADERS:
        report_type = "campaigns"
    else:
        raise ValueError(
            f"Cannot detect report type from first column: {fieldnames[0]!r}. "
            "Expected 'Search term', 'Keyword', or 'Campaign'."
        )

    key_col = fieldnames[0]  # actual column name (preserve original casing)

    # Helpers
    def _get(row: dict, *names: str) -> Optional[str]:
        for n in names:
            col = col_norm.get(n)
            if col and col in row:
                return row[col]
        return None

    def _int(v) -> int:
        try:
            return int(str(v or 0).replace(",", "").strip())
        except (ValueError, TypeError):
            return 0

    def _ctr(v) -> Optional[float]:
        """Parse CTR like '5.23%' → 0.0523. Google Ads always includes %."""
        try:
            return float(str(v or "").strip().rstrip("%")) / 100.0
        except (ValueError, TypeError):
            return None

    def _cost(v) -> Optional[float]:
        """Strip currency symbols and commas, then parse as float."""
        try:
            cleaned = re.sub(r"[^\d.]", "", str(v or "").replace(",", ""))
            return float(cleaned) if cleaned else None
        except (ValueError, TypeError):
            return None

    def _float(v) -> Optional[float]:
        try:
            s = str(v or "").strip().rstrip("%")
            return float(s) if s else None
        except (ValueError, TypeError):
            return None

    rows: list[dict] = []
    for row in reader:
        key_value = (row.get(key_col) or "").strip()
        if not key_value or key_value.lower() in {"total", "--", "-"}:
            continue

        if report_type == "search_terms":
            rows.append({
                "key":         key_value,
                "campaign":    (_get(row, "campaign", "campaign name") or "").strip() or None,
                "ad_group":    (_get(row, "ad group", "ad group name") or "").strip() or None,
                "match_type":  (_get(row, "match type") or "").strip() or None,
                "impressions": _int(_get(row, "impressions", "impr.")),
                "clicks":      _int(_get(row, "clicks")),
                "ctr":         _ctr(_get(row, "ctr")),
                "cost":        _cost(_get(row, "cost", "cost (all conv.)", "amount spent", "spend")),
                "conversions": _float(_get(row, "conversions", "all conv.", "key events")),
                "conv_rate":   _ctr(_get(row, "conv. rate", "conversion rate", "all conv. rate")),
            })
        else:  # campaigns
            rows.append({
                "key":           key_value,
                "campaign_type": (_get(row, "campaign type", "type") or "").strip() or None,
                "impressions":   _int(_get(row, "impressions", "impr.")),
                "clicks":        _int(_get(row, "clicks")),
                "ctr":           _ctr(_get(row, "ctr")),
                "cost":          _cost(_get(row, "cost", "amount spent", "spend")),
                "conversions":   _float(_get(row, "conversions", "all conv.", "key events")),
                "conv_rate":     _ctr(_get(row, "conv. rate", "conversion rate", "all conv. rate")),
            })

    if not rows:
        raise ValueError("CSV parsed successfully but contained no data rows.")

    return report_type, rows


# ── CRUD endpoints ────────────────────────────────────────────────────────────

@router.post("/accounts", status_code=201)
async def create_account(req: CreateAccountRequest):
    """Create a new Google Ads account entry."""
    async with AsyncSessionLocal() as db:
        acct = AdsAccount(
            id         = str(uuid.uuid4()),
            name       = req.name.strip(),
            account_id = req.account_id.strip() or None,
            currency   = req.currency.strip() or None,
        )
        db.add(acct)
        await db.commit()
    return {"id": acct.id, "name": acct.name}


@router.get("/accounts")
async def list_accounts():
    """Return all Ads accounts, newest first."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(AdsAccount).order_by(AdsAccount.created_at.desc())
        )).scalars().all()

    return [
        {
            "id":              a.id,
            "name":            a.name,
            "account_id":      a.account_id,
            "currency":        a.currency,
            "total_terms":     a.total_terms,
            "total_campaigns": a.total_campaigns,
            "created_at":      a.created_at.isoformat() if a.created_at else None,
            "updated_at":      a.updated_at.isoformat() if a.updated_at else None,
        }
        for a in rows
    ]


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str):
    """Delete an account and all its rows (CASCADE)."""
    async with AsyncSessionLocal() as db:
        await db.execute(sql_delete(AdsSearchTermRow).where(AdsSearchTermRow.account_id == account_id))
        await db.execute(sql_delete(AdsCampaignRow).where(AdsCampaignRow.account_id == account_id))
        await db.execute(sql_delete(AdsAccount).where(AdsAccount.id == account_id))
        await db.commit()
    return {"success": True}


# ── CSV upload ────────────────────────────────────────────────────────────────

@router.post("/accounts/{account_id}/upload")
@limiter.limit("30/hour")
async def upload_csv(request: Request, account_id: str, file: UploadFile = File(...)):
    """
    Upload a Google Ads CSV (search terms OR campaigns).
    Report type is auto-detected from column headers.
    Replaces any previously uploaded data of that type for this account.
    """
    async with AsyncSessionLocal() as db:
        acct = await db.get(AdsAccount, account_id)
        if not acct:
            raise_not_found("Account")

        content = await file.read()
        if len(content) > _MAX_CSV_BYTES:
            raise HTTPException(status_code=413, detail="File too large (max 20MB)")
        try:
            report_type, rows = _parse_ads_csv(content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        if report_type == "search_terms":
            await db.execute(
                sql_delete(AdsSearchTermRow).where(AdsSearchTermRow.account_id == account_id)
            )
            db.add_all([
                AdsSearchTermRow(
                    account_id  = account_id,
                    search_term = r["key"],
                    campaign    = r["campaign"],
                    ad_group    = r["ad_group"],
                    match_type  = r["match_type"],
                    impressions = r["impressions"],
                    clicks      = r["clicks"],
                    ctr         = r["ctr"],
                    cost        = r["cost"],
                    conversions = r["conversions"],
                    conv_rate   = r["conv_rate"],
                )
                for r in rows
            ])
            acct.total_terms = len(rows)

        else:  # campaigns
            await db.execute(
                sql_delete(AdsCampaignRow).where(AdsCampaignRow.account_id == account_id)
            )
            db.add_all([
                AdsCampaignRow(
                    account_id    = account_id,
                    campaign      = r["key"],
                    campaign_type = r["campaign_type"],
                    impressions   = r["impressions"],
                    clicks        = r["clicks"],
                    ctr           = r["ctr"],
                    cost          = r["cost"],
                    conversions   = r["conversions"],
                    conv_rate     = r["conv_rate"],
                )
                for r in rows
            ])
            acct.total_campaigns = len(rows)

        acct.updated_at = datetime.now(timezone.utc)
        await db.commit()

    return {
        "report_type":   report_type,
        "rows_imported": len(rows),
        "account_id":    account_id,
    }


# ── Data query endpoints ──────────────────────────────────────────────────────

_TERM_SORT = {
    "clicks_desc":     lambda: AdsSearchTermRow.clicks.desc(),
    "clicks_asc":      lambda: AdsSearchTermRow.clicks.asc(),
    "impressions_desc":lambda: AdsSearchTermRow.impressions.desc(),
    "cost_desc":       lambda: AdsSearchTermRow.cost.desc(),
    "cost_asc":        lambda: AdsSearchTermRow.cost.asc(),
    "ctr_desc":        lambda: AdsSearchTermRow.ctr.desc(),
    "conv_desc":       lambda: AdsSearchTermRow.conversions.desc(),
    "term_asc":        lambda: AdsSearchTermRow.search_term.asc(),
}

_CAMP_SORT = {
    "clicks_desc":     lambda: AdsCampaignRow.clicks.desc(),
    "cost_desc":       lambda: AdsCampaignRow.cost.desc(),
    "impressions_desc":lambda: AdsCampaignRow.impressions.desc(),
    "conv_desc":       lambda: AdsCampaignRow.conversions.desc(),
    "campaign_asc":    lambda: AdsCampaignRow.campaign.asc(),
}


@router.get("/accounts/{account_id}/search-terms")
async def get_search_terms(
    account_id: str,
    q:         str = "",
    sort:      str = "clicks_desc",
    page:      int = 0,
    page_size: int = 50,
    campaign:  str = "",
):
    """Return paginated, filtered, sorted search term rows."""
    order_fn = _TERM_SORT.get(sort, _TERM_SORT["clicks_desc"])

    async with AsyncSessionLocal() as db:
        stmt = select(AdsSearchTermRow).where(AdsSearchTermRow.account_id == account_id)
        if q:
            stmt = stmt.where(AdsSearchTermRow.search_term.ilike(f"%{q}%"))
        if campaign:
            stmt = stmt.where(AdsSearchTermRow.campaign.ilike(f"%{campaign}%"))

        total = (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar_one()

        items = (await db.execute(
            stmt.order_by(order_fn()).offset(page * page_size).limit(page_size)
        )).scalars().all()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items": [
            {
                "id":          r.id,
                "search_term": r.search_term,
                "campaign":    r.campaign,
                "ad_group":    r.ad_group,
                "match_type":  r.match_type,
                "impressions": r.impressions,
                "clicks":      r.clicks,
                "ctr":         round(r.ctr * 100, 2) if r.ctr is not None else None,
                "cost":        round(r.cost, 2)       if r.cost is not None else None,
                "conversions": r.conversions,
                "conv_rate":   round(r.conv_rate * 100, 2) if r.conv_rate is not None else None,
            }
            for r in items
        ],
    }


@router.get("/accounts/{account_id}/campaigns")
async def get_campaigns(
    account_id: str,
    sort:      str = "clicks_desc",
    page:      int = 0,
    page_size: int = 50,
):
    """Return paginated, sorted campaign rows."""
    order_fn = _CAMP_SORT.get(sort, _CAMP_SORT["clicks_desc"])

    async with AsyncSessionLocal() as db:
        stmt = select(AdsCampaignRow).where(AdsCampaignRow.account_id == account_id)

        total = (await db.execute(
            select(func.count()).select_from(stmt.subquery())
        )).scalar_one()

        items = (await db.execute(
            stmt.order_by(order_fn()).offset(page * page_size).limit(page_size)
        )).scalars().all()

    return {
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "items": [
            {
                "id":            r.id,
                "campaign":      r.campaign,
                "campaign_type": r.campaign_type,
                "impressions":   r.impressions,
                "clicks":        r.clicks,
                "ctr":           round(r.ctr * 100, 2) if r.ctr is not None else None,
                "cost":          round(r.cost, 2)       if r.cost is not None else None,
                "conversions":   r.conversions,
                "conv_rate":     round(r.conv_rate * 100, 2) if r.conv_rate is not None else None,
            }
            for r in items
        ],
    }


# ── Cross-reference endpoint ──────────────────────────────────────────────────

@router.get("/accounts/{account_id}/cross-reference")
async def cross_reference(account_id: str):
    """
    Cross-reference Ads search terms against GSC queries.

    Returns:
    - paid_and_organic : terms present in BOTH Ads and GSC (paying for organic traffic)
    - paid_only        : Ads terms with no GSC match (blind spend — no organic presence)
    - organic_only     : GSC queries not in Ads (organic opportunities not yet captured)
    - summary          : counts
    """
    async with AsyncSessionLocal() as db:

        # ── Load all Ads search terms ──────────────────────────────────────
        all_terms = (await db.execute(
            select(AdsSearchTermRow).where(AdsSearchTermRow.account_id == account_id)
        )).scalars().all()

        # ── Load all GSC queries (across all properties) ───────────────────
        gsc_rows = (await db.execute(
            select(GscQueryRow.query, GscQueryRow.clicks, GscQueryRow.impressions,
                   GscQueryRow.ctr, GscQueryRow.position)
        )).all()

        # Build normalised GSC lookup
        gsc_lookup: dict[str, dict] = {}
        for query, clicks, impressions, ctr, position in gsc_rows:
            q_norm = (query or "").lower().strip()
            # Keep the best (highest clicks) GSC entry if query appears in multiple properties
            existing = gsc_lookup.get(q_norm)
            if existing is None or clicks > existing["clicks"]:
                gsc_lookup[q_norm] = {
                    "query":       query,
                    "clicks":      clicks,
                    "impressions": impressions,
                    "ctr":         round(ctr * 100, 2) if ctr is not None else None,
                    "position":    round(position, 1)  if position is not None else None,
                }

    # ── Cross-reference ───────────────────────────────────────────────────
    paid_and_organic: list[dict] = []
    paid_only:        list[dict] = []

    ads_term_set: set[str] = set()

    for t in all_terms:
        t_norm = (t.search_term or "").lower().strip()
        ads_term_set.add(t_norm)

        base_info = {
            "search_term": t.search_term,
            "campaign":    t.campaign,
            "clicks":      t.clicks,
            "impressions": t.impressions,
            "ctr":         round(t.ctr * 100, 2) if t.ctr is not None else None,
            "cost":        round(t.cost, 2)       if t.cost is not None else None,
            "conversions": t.conversions,
        }

        gsc_match = gsc_lookup.get(t_norm)
        if gsc_match:
            paid_and_organic.append({
                **base_info,
                "gsc_clicks":      gsc_match["clicks"],
                "gsc_impressions": gsc_match["impressions"],
                "gsc_ctr":         gsc_match["ctr"],
                "gsc_position":    gsc_match["position"],
            })
        else:
            paid_only.append(base_info)

    # Organic-only: GSC queries not present in any Ads search term
    organic_only: list[dict] = [
        {
            "query":       v["query"],
            "clicks":      v["clicks"],
            "impressions": v["impressions"],
            "ctr":         v["ctr"],
            "position":    v["position"],
        }
        for k, v in gsc_lookup.items()
        if k not in ads_term_set
    ]

    # ── Sort ──────────────────────────────────────────────────────────────
    paid_and_organic.sort(key=lambda x: (x.get("cost") or 0),    reverse=True)
    paid_only.sort(       key=lambda x: (x.get("cost") or 0),    reverse=True)
    organic_only.sort(    key=lambda x: (x.get("clicks") or 0),  reverse=True)

    return {
        "paid_and_organic": paid_and_organic[:200],
        "paid_only":        paid_only[:200],
        "organic_only":     organic_only[:200],
        "summary": {
            "total_ads_terms":     len(all_terms),
            "total_gsc_queries":   len(gsc_lookup),
            "paid_and_organic":    len(paid_and_organic),
            "paid_only":           len(paid_only),
            "organic_only":        len(organic_only),
        },
    }
