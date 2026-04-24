Fix 500 error when generating PDF with `include_briefs=true` due to missing ContentBrief fields.

## Problem
`api/routes/pdf_reports.py:671` accesses `brief.current_score` and `brief.executive_summary`
on `ContentBrief` objects. Neither column exists on the `ContentBrief` model in
`api/models/content.py:14`.

This causes `AttributeError` → HTTP 500 whenever a PDF is generated with briefs included.

## Diagnosis
Read `api/models/content.py` to see the actual `ContentBrief` columns.
Read `api/routes/pdf_reports.py` around line 671 to see all fields accessed.

## Fix — two options:

### Option A — Add missing columns to ContentBrief (preferred if fields are genuinely needed)
In `api/models/content.py`:
```python
class ContentBrief(Base):
    # existing columns...
    current_score = Column(Float, nullable=True)
    executive_summary = Column(Text, nullable=True)
```
Generate Alembic migration: `alembic revision --autogenerate -m "add_brief_score_summary"`

### Option B — Use `getattr` with fallback in pdf_reports.py (quick fix)
```python
score = getattr(brief, 'current_score', None)
summary = getattr(brief, 'executive_summary', None) or ""
```

Choose Option A if these fields should be stored; Option B if they're derived/optional.
Read the ContentBrief creation flow to determine which makes sense.

## Files
- `api/routes/pdf_reports.py` line 671
- `api/models/content.py` line 14 (ContentBrief class)
- `migrations/versions/` — new migration if Option A
