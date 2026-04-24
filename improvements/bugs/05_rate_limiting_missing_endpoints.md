Add `@limiter.limit()` to endpoints currently missing rate limiting.

## Problem
Only `api/routes/audits.py` has rate limiting. The following routes have expensive
LLM calls or DB writes with no rate protection:

- `api/routes/fanout.py` — `POST /api/fanout/analyze` (calls OpenAI/Anthropic)
- `api/routes/content_iq.py` — analyze endpoint (LLM calls)
- `api/routes/geo_monitor.py` — `POST /api/geo-monitor/projects/{id}/scan`
- `api/routes/citation_tracker.py` — `POST /api/citations/trackers/{id}/scan`
- `api/routes/ads.py` — CSV upload endpoints
- `api/routes/ga4.py` — CSV upload endpoints
- `api/routes/keyword_research.py` — DataForSEO calls

## Fix
Import limiter and add decorator. Pattern from `api/routes/audits.py`:

```python
from api.limiter import limiter
from fastapi import Request

@router.post("/api/fanout/analyze")
@limiter.limit("20/hour")
async def analyze_fanout(request: Request, ...):  # request must be first param
    ...
```

### Recommended limits by endpoint type
- LLM analysis endpoints: `"20/hour"` 
- DataForSEO calls: `"50/hour"`
- File upload endpoints: `"30/hour"`
- Read-only GET endpoints: `"200/hour"` (or skip)

## Important
`request: Request` MUST be the first parameter when using `@limiter.limit()`.
Check existing route signatures before adding the decorator.

## Files
- `api/routes/fanout.py`
- `api/routes/content_iq.py`
- `api/routes/geo_monitor.py`
- `api/routes/citation_tracker.py`
- `api/routes/ads.py`
- `api/routes/ga4.py`
- `api/routes/keyword_research.py`
