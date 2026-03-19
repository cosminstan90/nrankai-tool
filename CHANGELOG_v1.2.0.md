# CHANGELOG — Website LLM Analyzer v1.2.0

## New Features (February 2026)

### 1. 🤖 AI Executive Summary & Action Plan
- **Files:** `api/routes/summary.py` (API), `api/models/database.py` (model), `api/templates/audit_detail.html` (UI), `api/templates/report.html` (print view)
- **Feature:** Generates comprehensive AI-powered executive summaries and prioritized action plans after audit completion
- **Components:**
  - **Executive Summary:** 3-4 paragraph narrative for C-level stakeholders
  - **Key Findings:** 5-8 most important issues with impact levels (high/medium/low) and categorization
  - **6-Week Action Plan:** Week-by-week prioritized tasks with expected impact and page counts
  - **Competitive Position:** Strategic evaluation paragraph
- **Model Selection:** Choose different LLM provider/model than audit (e.g., use cheaper Haiku for summary after expensive Opus audit)
- **Presets Available:**
  - Same as audit (default)
  - Cheap options: Claude Haiku 4, GPT-4o Mini, Mistral Small
  - Balanced options: Claude Sonnet 4, GPT-4o
- **Language Support:** Generate summaries in any language (default: English)
- **Background Processing:** Summary generation runs asynchronously with automatic polling
- **Integration:**
  - Widget on audit detail page with model selector dropdown
  - Included in printable PDF report
  - Real-time status updates via polling

### API Endpoints Added
```
POST /api/audits/{audit_id}/summary    → Generate AI summary (background task)
  Query params: language, provider, model
  
GET  /api/audits/{audit_id}/summary    → Retrieve generated summary or status
```

### Database Changes
```sql
-- New table: audit_summaries
CREATE TABLE audit_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT UNIQUE NOT NULL,
    executive_summary TEXT,
    key_findings TEXT,              -- JSON array
    action_plan TEXT,                -- JSON array
    competitive_position TEXT,
    language TEXT DEFAULT 'English',
    provider TEXT,                   -- Provider used for summary
    model TEXT,                      -- Model used for summary
    generated_at DATETIME,
    FOREIGN KEY (audit_id) REFERENCES audits(id) ON DELETE CASCADE
);
```

### Technical Implementation
- **Async LLM Calls:** Uses AsyncAnthropic, AsyncOpenAI, Mistral async clients
- **JSON Parsing:** Handles code fence stripping from LLM responses
- **Data Aggregation:** Analyzes all audit results to build comprehensive payload:
  - Score distribution statistics
  - Top 10 best/worst performing pages
  - Top 30 optimization opportunities across all pages
- **System Prompt Engineering:** Structured prompt for consistent JSON output with required keys
- **Multi-language Support:** Language instruction injection when non-English requested
- **Error Handling:** Graceful fallbacks and user feedback for generation failures

### UI/UX Features
- **Alpine.js Widget:** Reactive component with state management
- **Model Preset Dropdown:** User-friendly selection of LLM options
- **Loading States:** Spinner animation during generation
- **Auto-polling:** Checks for completion every 2 seconds with 2-minute timeout
- **Regenerate Option:** Re-run summary with different model/settings
- **Visual Hierarchy:**
  - Impact badges (red=high, yellow=medium, green=low)
  - Priority badges (red=critical, orange=high, yellow=medium)
  - Week-by-week timeline layout
  - Color-coded action items
- **Print-Optimized:** Full integration in PDF-ready report template

## Files Added
```
api/routes/summary.py               — Summary generation API router (370 lines)
CHANGELOG_v1.2.0.md                 — This file
```

## Files Modified
```
api/models/database.py              — Added AuditSummary model with to_dict() method
api/routes/__init__.py              — Added summary_router export
api/main.py                         — Included summary_router, updated report route with summary context
api/templates/audit_detail.html     — Added AI Summary widget section (200+ lines)
api/templates/report.html           — Added AI Summary section in printable report
```

## Migration Notes
- **Automatic database migration:** AuditSummary table created on next startup via init_db()
- **Backward compatible:** Existing audits work without summaries; feature is opt-in
- **No new dependencies:** Uses existing LLM clients (anthropic, openai, mistralai)
- **Optional provider override:** Can use different/cheaper model for summary vs audit

## Usage Example
1. Complete an audit (any type, any provider)
2. Navigate to audit detail page
3. Select model preset from dropdown (or keep "Same as audit")
4. Click "Generate AI Summary"
5. Wait 10-30 seconds for generation
6. View narrative summary, findings, and action plan
7. Generate printable report with summary included
8. Regenerate with different model if needed

## Cost Optimization Tips
- Use Haiku/GPT-4o-mini for summaries after expensive Opus audits
- Summary generation typically uses 4,000-6,000 input tokens + 2,000-3,000 output tokens
- Claude Haiku cost: ~$0.01 per summary
- GPT-4o Mini cost: ~$0.002 per summary

## Future Enhancements (Potential)
- Export summary as standalone Word/PDF document
- Email delivery of completed summaries
- Summary comparison across multiple audits
- Custom summary templates (executive vs technical focus)
- Integration with project management tools (Jira, Asana)
