# Competitor Benchmarking Implementation Guide v1.3.0

## Overview
This implementation adds a complete **Competitor Benchmarking** module that enables grouping 2-5 audits (target + competitors) for AI-powered competitive analysis. The feature provides comprehensive comparison metrics, visualizations, and strategic insights.

## What's New

### Database Layer
- **New Model:** `BenchmarkProject` in `api/models/database.py`
  - Stores benchmark project metadata
  - Links target + competitor audits
  - Stores AI-generated competitive analysis
  - Automatically created at `init_db()`

### API Layer
- **New Router:** `api/routes/benchmarks.py`
  - `POST /api/benchmarks` — Create benchmark, start AI analysis
  - `GET /api/benchmarks` — List all benchmarks with summary
  - `GET /api/benchmarks/{id}` — Detailed view with full analysis
  - `DELETE /api/benchmarks/{id}` — Delete benchmark
  - `POST /api/benchmarks/{id}/regenerate` — Regenerate AI analysis

### Frontend
- **New Template:** `api/templates/benchmarks.html`
  - Split-screen layout: Create/List (left) + Detail View (right)
  - Dynamic audit filtering by type
  - Multi-select competitor picker
  - Chart.js visualizations (bar charts for scores and distributions)
  - Full AI analysis display (summary, strengths, weaknesses, opportunities, threat level)
  - Real-time polling for analysis completion
  - Model override for regeneration

### Navigation
- Added "Benchmarks" link to `base.html` navigation
- Added template route in `main.py`

## Architecture

### Data Flow
```
1. User creates benchmark (selects target + competitors)
   ↓
2. API validates all audits (exist, completed, same type)
   ↓
3. Background task starts AI competitive analysis
   ↓
4. System loads audit summaries for target + competitors
   ↓
5. Constructs comparative payload with scores, distributions, issues
   ↓
6. Calls LLM (reuses call_llm_for_summary from summary.py)
   ↓
7. Parses JSON response with competitive insights
   ↓
8. Saves to benchmark_summary field
   ↓
9. Frontend polls and displays when ready
```

### Helper Functions

**`_load_audit_summary(audit_id, db)`** — Aggregates audit data:
- Website, avg_score, pages_analyzed
- Distribution (excellent/good/needs_work/poor counts)
- Top 10 issues from all pages

**`_build_benchmark_system_prompt()`** — Structures LLM request for:
- Competitive summary (2-3 paragraphs)
- Strengths array (3-5 items with scores + insights)
- Weaknesses array (3-5 items with scores + insights)
- Opportunities array (3-5 items with priority + rationale)
- Threat level (low/medium/high)

**`_build_benchmark_data_payload()`** — Constructs data for LLM:
- Target performance metrics
- Competitor performance metrics
- Comparative statistics (avg, best, rank, delta)

## UI Components

### Create Form
- Name, description, audit type selector
- Target audit dropdown (filtered by type)
- Competitor multi-select (1-4, excludes target)
- Validation: same audit type, all completed

### Benchmarks List
- Card view with: name, type, target score, competitor count
- Status badge: "Analyzed" (green) or "Processing" (yellow)
- Click to select and view details

### Detail View (Right Panel)

**Scoreboard:**
- 4 metric cards: Target Score (highlighted), Competitor Avg, Best Competitor, Rank

**Score Comparison Chart:**
- Horizontal bar chart showing all sites side-by-side

**Distribution Comparison Chart:**
- Stacked bar chart with 4 quality levels per site

**AI Analysis:**
- Loading state while generating
- Regenerate button with model override dropdown
- Competitive Summary (blue highlighted box)
- Strengths (green left border cards)
- Weaknesses (red left border cards)
- Opportunities (yellow background cards with priority badges)
- Threat Level (colored badge: green/yellow/red)

## AI Analysis Schema

The LLM returns JSON with this structure:

```json
{
  "competitive_summary": "2-3 paragraph narrative...",
  "strengths": [
    {
      "area": "SEO Optimization",
      "target_score_range": "85-90",
      "competitor_avg": "72",
      "insight": "Target significantly outperforms..."
    }
  ],
  "weaknesses": [
    {
      "area": "Content Quality",
      "target_score_range": "65-70",
      "competitor_avg": "78",
      "insight": "Target lags behind competitors..."
    }
  ],
  "opportunities": [
    {
      "opportunity": "Improve meta descriptions",
      "priority": "high",
      "rationale": "Quick win with 15-20 point potential gain..."
    }
  ],
  "threat_level": "medium"
}
```

## Frontend State Management (Alpine.js)

### Data Properties:
- `audits` — All completed audits from server
- `benchmarks` — List of benchmark projects
- `selectedBenchmarkId` — Currently viewing
- `detail` — Full benchmark data with AI analysis
- `form` — Create form state

### Computed Properties:
- `availableTypes` — Unique audit types for dropdown
- `filteredAudits` — Audits matching selected type

### Methods:
- `createBenchmark()` — POST new benchmark
- `loadBenchmarks()` — GET list (polled every 5s)
- `selectBenchmark(id)` — Load detail view
- `loadBenchmarkDetail(id)` — GET full data + charts
- `deleteBenchmark(id)` — DELETE with confirmation
- `regenerateAnalysis()` — POST with model override
- `renderCharts()` — Creates Chart.js visualizations

## Model Presets for Regeneration

Frontend includes 6 model presets:
1. **same** — Use original audit's provider/model
2. **cheap_anthropic** — Claude Haiku 4 ($0.01/summary)
3. **cheap_openai** — GPT-4o Mini ($0.002/summary)
4. **cheap_mistral** — Mistral Small ($0.01/summary)
5. **balanced_anthropic** — Claude Sonnet 4 ($0.03/summary)
6. **balanced_openai** — GPT-4o ($0.025/summary)

## Cost Considerations

**Input Tokens per Benchmark:** ~5,000-8,000 tokens
- Target audit summary: 1,500-2,500 tokens
- Each competitor summary: 1,000-1,500 tokens
- Comparative statistics: 500-1,000 tokens

**Output Tokens:** ~2,500-4,000 tokens
- Competitive summary: 600-900 tokens
- Strengths: 400-700 tokens
- Weaknesses: 400-700 tokens
- Opportunities: 400-700 tokens
- Threat level: 100-200 tokens

**Typical Costs (target + 3 competitors):**
- Claude Haiku 4: ~$0.015
- GPT-4o Mini: ~$0.003
- Claude Sonnet 4: ~$0.04
- GPT-4o: ~$0.03

## Database Schema

```sql
CREATE TABLE benchmark_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    audit_type TEXT NOT NULL,
    target_audit_id TEXT,
    competitor_audit_ids TEXT,  -- JSON array
    benchmark_summary TEXT,      -- JSON object
    created_at DATETIME,
    updated_at DATETIME,
    FOREIGN KEY (target_audit_id) REFERENCES audits(id) ON DELETE SET NULL
);
```

## Validation Rules

1. **Audit Count:** 2-5 total (1 target + 1-4 competitors)
2. **Audit Status:** All must be "completed"
3. **Audit Type:** All must match the specified type
4. **Uniqueness:** Target cannot be in competitors list

## Error Handling

**Backend:**
- Missing audits → 404
- Non-completed audits → 400 with status
- Type mismatch → 400 with details
- LLM errors → Logged, no crash, can regenerate

**Frontend:**
- Loading states with spinners
- Alert modals for errors
- Polling timeout after 2 minutes
- Delete confirmation

## Real-Time Updates

Frontend polls every 5 seconds:
- Refreshes benchmark list (checks for new analysis)
- Refreshes detail view if selected
- Updates charts when new data arrives

Processing indicator shown until `benchmark_summary` is populated.

## Testing Checklist

### Backend:
- [ ] POST benchmark with valid data → Creates successfully
- [ ] POST benchmark with incomplete audits → 400 error
- [ ] POST benchmark with mismatched types → 400 error
- [ ] GET benchmarks → Returns list with summary info
- [ ] GET benchmark detail → Returns full data + analysis
- [ ] DELETE benchmark → Removes successfully
- [ ] POST regenerate with provider override → Uses new model
- [ ] Background task generates valid JSON analysis

### Frontend:
- [ ] Audit type dropdown populates from data
- [ ] Target dropdown filters by selected type
- [ ] Competitor checkboxes exclude target
- [ ] Create button disabled until valid form
- [ ] Benchmarks list shows status badges correctly
- [ ] Click benchmark loads detail view
- [ ] Score chart renders with correct colors
- [ ] Distribution chart shows stacked bars
- [ ] AI analysis sections display when ready
- [ ] Regenerate updates analysis
- [ ] Delete removes from list and clears detail
- [ ] Polling updates status automatically

## Integration Points

**Reuses from v1.2.0:**
- `call_llm_for_summary()` from `summary.py`
- `clean_json_response()` for parsing
- `AsyncSessionLocal` for background tasks
- Same LLM provider structure

**Compatible with:**
- Existing audit workflow
- Summary generation feature
- Compare feature (separate use case)

## Migration

No manual migration needed. On next startup:
```python
init_db()  # Creates benchmark_projects table automatically
```

SQLite schema addition is backwards compatible.

## Deployment Steps

1. **Backup database:**
   ```bash
   cp api/data/analyzer.db api/data/analyzer.db.backup
   ```

2. **Extract updated code:**
   ```bash
   unzip website_llm_analyzer_v1_3_0_benchmarking.zip
   cd website_llm_analyzer_v1_3_0_benchmarking
   ```

3. **No new dependencies** (uses existing packages)

4. **Start server:**
   ```bash
   cd api
   python main.py
   ```

5. **Verify:**
   - Check logs for "✓ Database initialized"
   - Navigate to http://localhost:8000/benchmarks
   - Create test benchmark with completed audits

## Usage Example

1. User has completed 4 SEO audits: own site + 3 competitors
2. Navigate to Benchmarks
3. Fill form:
   - Name: "Q1 2024 SEO Battle"
   - Type: "SEO"
   - Target: Own site
   - Competitors: Check 3 competitors
4. Click "Create Benchmark"
5. Wait 10-30s for AI analysis
6. Review:
   - Scoreboard shows rank
   - Charts visualize performance gaps
   - AI identifies specific strengths/weaknesses
   - Opportunities prioritized by impact
7. Share report with stakeholders
8. Regenerate with different model if needed

## Troubleshooting

**Analysis won't generate:**
- Check API keys in `.env`
- Verify all audits are completed
- Check server logs for LLM errors

**Charts don't render:**
- Verify Chart.js loaded (check browser console)
- Ensure data structure matches expected format
- Check canvas elements exist in DOM

**Polling timeout:**
- Analysis may still complete in background
- Refresh page to check
- Check server logs for stuck tasks

## Future Enhancements

1. **Export Options:**
   - PDF report generation
   - PowerPoint slide deck
   - Excel data export

2. **Historical Tracking:**
   - Compare benchmarks over time
   - Trend analysis
   - Competitive position evolution

3. **Advanced Filters:**
   - Filter by date range
   - Group by industry
   - Custom competitor sets

4. **Alerts:**
   - Email when competitor overtakes
   - Slack notifications
   - Weekly digest

## Performance Notes

- Benchmark analysis runs in background (non-blocking)
- Supports concurrent analysis for multiple benchmarks
- Chart rendering uses Canvas API (hardware accelerated)
- Polling is lightweight (just checks for completion)

## Support

For issues or questions:
1. Check server logs in terminal
2. Verify network requests in browser DevTools
3. Review `api/routes/benchmarks.py` for logic
4. Test individual API endpoints with curl/Postman

**Module is fully backward compatible** — existing features continue to work unchanged.
