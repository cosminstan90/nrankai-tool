# Improvements Backlog

Generated from project audit — April 2026. Sources: Claude audit + Codex audit.

## Security (fix first)

| File | Priority | Source | Description |
|------|----------|--------|-------------|
| [security/01_path_traversal_logo_upload.md](security/01_path_traversal_logo_upload.md) | CRITICAL | Both | Path traversal in logo upload — use UUID filenames |
| [security/06_website_path_traversal_audit_worker.md](security/06_website_path_traversal_audit_worker.md) | CRITICAL | Codex | `website` field unsanitized in `_safe_dir()` + `shutil.rmtree` — arbitrary dir write/delete |
| [security/07_oauth_csrf_gsc_fanout.md](security/07_oauth_csrf_gsc_fanout.md) | CRITICAL | Codex | OAuth state = predictable project_id, no nonce/HMAC — CSRF account binding |
| [security/08_ssrf_audit_fetch_urls.md](security/08_ssrf_audit_fetch_urls.md) | CRITICAL | Codex | SSRF on audit URL fetch (audits.py:142, :421) and optimizer.py:114 |
| [security/02_idor_access_control.md](security/02_idor_access_control.md) | HIGH | Claude | Verify 404 checks exist on all object retrieval endpoints |
| [security/03_file_upload_size_limits.md](security/03_file_upload_size_limits.md) | HIGH | Claude | Add max size validation on all UploadFile handlers |
| [security/04_xss_action_cards_export.md](security/04_xss_action_cards_export.md) | MEDIUM | Claude | html.escape() on all user data in HTML export |
| [security/05_ssrf_webhook_url_validation.md](security/05_ssrf_webhook_url_validation.md) | MEDIUM | Both | Block private IPs in webhook calls (audit_worker + schemas) |

## Bugs & Code Quality

| File | Priority | Source | Description |
|------|----------|--------|-------------|
| [bugs/07_page_optimize_request_undefined.md](bugs/07_page_optimize_request_undefined.md) | CRITICAL | Codex | `PageOptimizeRequest` undefined — breaks OpenAPI schema at startup |
| [bugs/08_missing_json_import_models.md](bugs/08_missing_json_import_models.md) | CRITICAL | Codex | Missing `import json` in content.py + infra.py — NameError in `to_dict()` |
| [bugs/09_content_brief_missing_fields_pdf.md](bugs/09_content_brief_missing_fields_pdf.md) | HIGH | Codex | `brief.current_score` / `brief.executive_summary` don't exist — 500 on PDF with briefs |
| [bugs/10_old_score_overwrite_compare.md](bugs/10_old_score_overwrite_compare.md) | HIGH | Codex | `old_score` read after overwrite in compare.py:615 — always reports wrong value |
| [bugs/11_duplicate_routes_fanout.md](bugs/11_duplicate_routes_fanout.md) | HIGH | Codex | Duplicate routes in fanout.py (lines 899, 1767) — second handler unreachable |
| [bugs/01_requirements_txt_complete.md](bugs/01_requirements_txt_complete.md) | CRITICAL | Claude | Complete missing packages in requirements.txt |
| [bugs/02_datetime_utcnow_fix.md](bugs/02_datetime_utcnow_fix.md) | HIGH | Claude | Replace 152x deprecated datetime.utcnow() |
| [bugs/03_bare_except_fix.md](bugs/03_bare_except_fix.md) | HIGH | Claude | Fix silent except:pass in main.py, database.py, auth.py |
| [bugs/04_pydantic_input_validation.md](bugs/04_pydantic_input_validation.md) | MEDIUM | Claude | Add max_length + URL regex to Pydantic models |
| [bugs/05_rate_limiting_missing_endpoints.md](bugs/05_rate_limiting_missing_endpoints.md) | MEDIUM | Claude | Add @limiter.limit() to fanout, content_iq, geo_monitor, ads, ga4 |
| [bugs/06_alembic_schema_migrations.md](bugs/06_alembic_schema_migrations.md) | MEDIUM | Claude | Move ALTER TABLE from lifespan() to Alembic migrations |

## New Features

| File | Effort | Status | Description |
|------|--------|--------|-------------|
| [features/01_ai_visibility_dashboard.md](features/01_ai_visibility_dashboard.md) | Medium | 📋 Todo | Unified dashboard aggregating GeoMonitor + CitationTracker |
| [features/02_competitor_benchmarking.md](features/02_competitor_benchmarking.md) | Medium | 📋 Todo | Run same queries for competitor brands, side-by-side scores |
| ~~features/03~~ | Small | ✅ Done | Gemini added to GeoMonitor + CitationTracker |
| ~~features/04~~ | Small | ✅ Done | LLM auto-suggest probe queries (`POST /api/ai-visibility/suggest-queries`) |
| ~~features/05~~ | Small | ✅ Done | Visibility drop alerting via webhook (threshold configurabil per proiect) |
