# Website LLM Analyzer — Master Unified Version

Unified build created from multiple versions/modules (v1.1 → v2.6 + Gemini + gap/content gaps + action cards).

## Included modules
- Core audits/results/summary/compare/benchmarks/schedules
- Geo Monitor
- Content Briefs
- White-label PDF reports
- Gemini / provider registry UI integration
- Schema Generator
- Citation Tracker
- Portfolio Dashboard
- Cost Tracking
- Gap Analysis
- Content Gaps
- Action Cards
- Audit Templates manager

## Run API
```bash
pip install -r requirements.txt
pip install -r api/requirements.txt
uvicorn api.main:app --reload
```

## Notes
This is a merged master baseline. Some modules may need final endpoint/template harmonization after runtime validation.
