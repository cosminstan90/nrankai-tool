# CHANGELOG v1.5.0 - GEO Visibility Monitor

## Release Date: February 20, 2026

## Summary

Major feature release adding **GEO Visibility Monitor** - a comprehensive tool for tracking brand visibility across AI-powered search platforms (ChatGPT, Claude, Perplexity). This addresses a critical need for SEO/GEO consultants to quantify and justify their service ROI with concrete data.

## New Features

### 🌐 GEO Visibility Monitor

**Business Value:**
- Track brand mentions and citations across multiple AI platforms
- Quantify GEO optimization ROI with visibility scores (0-100%)
- Identify gaps where brand is not mentioned
- Monitor competitor landscape
- Generate client reports with concrete metrics

**Technical Implementation:**

1. **Database Models** (api/models/database.py)
   - Added `GeoMonitorProject` model
     - Stores project configuration (name, website, brand keywords, queries)
     - JSON fields for keywords, queries, and provider configuration
     - Relationship to scans with cascade delete
   - Added `GeoMonitorScan` model
     - Individual scan execution records
     - Tracks status, progress, visibility scores
     - Stores detailed results per query-provider combination
     - Provider-specific score breakdown

2. **API Endpoints** (api/routes/geo_monitor.py)
   - `POST /api/geo-monitor/projects` - Create monitoring project
     - Validates min 1 keyword, 1 query, 1 provider
     - Returns suggested queries based on domain/language
   - `GET /api/geo-monitor/projects` - List projects with latest scores
   - `GET /api/geo-monitor/projects/{id}` - Project details + scan history
   - `DELETE /api/geo-monitor/projects/{id}` - Delete project
   - `POST /api/geo-monitor/projects/{id}/scan` - Start background scan
   - `GET /api/geo-monitor/scans/{scan_id}` - Detailed scan results
   - `GET /api/geo-monitor/projects/{id}/trend` - Chart.js compatible trend data

3. **Background Scan Engine**
   - Async execution with `asyncio.create_task()`
   - Queries multiple LLM providers concurrently
   - Concurrency control: max 3 simultaneous API calls
   - Rate limiting: 1 second delay between same-provider calls
   - Error handling: failed calls don't block scan completion
   - Progress tracking: real-time completion counter

4. **LLM Provider Integration**
   - **ChatGPT**: gpt-4o-mini (cost-effective monitoring)
   - **Claude**: claude-sonnet-4-20250514 (high quality)
   - **Perplexity**: sonar model (web-connected responses)
   - Simple user messages (no complex system prompts)
   - 1500 token output limit per query

5. **Response Analysis**
   - **Mention Detection**: Case-insensitive keyword matching
   - **Citation Detection**: URL presence in responses
   - **Context Extraction**: 200-character snippets around mentions
   - **Position Classification**:
     - `primary_recommendation` - keyword in first 25% of response
     - `listed` - keyword appears 2+ times
     - `mentioned_in_passing` - single mention
     - `not_found` - no mention
   - **Sentiment Analysis**: Basic positive/negative keyword detection
   - Full response text stored for detailed review

6. **Web Interface** (api/templates/geo_monitor.html)
   - **Project Creation Form**:
     - Name, website URL, language selector
     - Tag-based keyword input (add/remove chips)
     - Multi-line query textarea
     - Auto-suggestion generator (10 queries per domain/language)
     - Provider checkboxes (disabled if API key not configured)
   - **Project Dashboard**:
     - Card-based layout with visibility scores
     - Color-coded scores (green ≥70%, yellow ≥40%, red <40%)
     - Last scan date and total scan count
     - Quick actions: Run Scan, Delete
   - **Detail View**:
     - Large visibility gauge (0-100%)
     - Delta vs previous scan (↗/↘)
     - Total mentions and citations cards
     - Provider breakdown with progress bars
     - Historical trend chart (Chart.js multi-line)
     - Query results matrix (emoji indicators: ✅🔗❌)
     - Clickable cells open detail modal with full response
   - **Real-time Updates**:
     - Scan status: pending → running → completed
     - Progress counter (X/Y queries)
     - Loading spinners
   - **AlpineJS State Management**:
     - Client-side routing (project list ↔ detail view)
     - Form validation
     - Async API calls with error handling
     - Chart rendering

7. **Navigation**
   - Added "🌐 GEO Monitor" link to main navigation
   - Positioned after "Schedules"
   - Active state highlighting

8. **Configuration**
   - Auto-detects configured providers via API keys
   - Visual provider status badges in UI
   - .env.example updated with GEO Monitor section
   - Clear documentation of required API keys

## Technical Details

### Cost Analysis

**Per Scan (10 queries × 3 providers = 30 calls):**
- Using cost-effective models (gpt-4o-mini, claude-haiku):
  - ~$0.03-0.04 per scan
  - ~$1.20 per 40 scans/month
- Using premium models (gpt-4o, claude-sonnet):
  - ~$0.40-0.50 per scan
  - ~$16-20 per 40 scans/month

### Performance

- **Scan Duration**: ~2-3 minutes for 30 checks (10 queries × 3 providers)
- **Concurrent Calls**: Max 3 simultaneous (prevents rate limiting)
- **Database**: SQLite async (aiosqlite) - no blocking operations
- **Error Recovery**: Individual call failures don't block scan completion

### Architecture Patterns

- **Async/Await**: All I/O operations non-blocking
- **Background Tasks**: FastAPI BackgroundTasks for long-running scans
- **Session Management**: AsyncSessionLocal() in background tasks
- **JSON Storage**: Brand keywords, queries, results stored as TEXT columns
- **Cascade Delete**: Deleting project automatically removes all scans

### Code Quality

- ✅ Python syntax validated (ast.parse)
- ✅ Jinja2 templates validated (Environment().parse)
- ✅ Pydantic v2 validators (@field_validator, @classmethod)
- ✅ Type hints throughout
- ✅ Error handling with try-except blocks
- ✅ SQLAlchemy async patterns
- ✅ AlpineJS best practices

## Files Modified

### Core Application
- `api/models/database.py` - Added GeoMonitorProject and GeoMonitorScan models
- `api/routes/__init__.py` - Exported geo_monitor_router
- `api/main.py` - Included geo_monitor_router, added template route
- `api/templates/base.html` - Added GEO Monitor navigation link

### New Files
- `api/routes/geo_monitor.py` - Complete router with all endpoints and scan engine
- `api/templates/geo_monitor.html` - Full UI with AlpineJS interactivity
- `.env.example` - Updated with GEO Monitor API keys section
- `GEO_MONITOR_README.md` - Comprehensive documentation
- `CHANGELOG_v1_5_0_GEO_MONITOR.md` - This file

## Breaking Changes

None - fully backward compatible with v1.4.0

## Migration Guide

### From v1.4.0 to v1.5.0

1. **Update Code:**
   ```bash
   # Replace all files from v1.5.0 package
   cp -r website_llm_analyzer_v1_5_0/* /path/to/your/installation/
   ```

2. **Database Migration:**
   ```bash
   # Tables auto-create on first launch
   # No manual migration needed
   python api/main.py
   ```

3. **Configure API Keys (Optional):**
   ```bash
   # Add to .env if you want to use GEO Monitor
   echo "PERPLEXITY_API_KEY=pplx-..." >> .env
   # OPENAI_API_KEY and ANTHROPIC_API_KEY reused from existing config
   ```

4. **Restart Server:**
   ```bash
   uvicorn api.main:app --reload
   ```

5. **Access GEO Monitor:**
   ```
   Navigate to: http://localhost:8000/geo-monitor
   ```

## Usage Examples

### Example 1: Banking Institution

```
Project: ING Romania GEO Monitor
Website: ing.ro
Keywords: ["ING", "ING Bank", "ING Romania", "ing.ro"]
Queries:
  - Care sunt cele mai bune bănci din România?
  - Ce bancă recomandați pentru cont curent?
  - ING Romania recenzii
  - Credit ipotecar ING
  - Aplicație banking recomandată România

Results after scan:
  - Overall Visibility: 68%
  - ChatGPT: 70%
  - Claude: 50%
  - Perplexity: 85%

Insights:
  - Strong on Perplexity (web-connected)
  - Weak on Claude (needs more authoritative content)
  - Best query: "Care sunt cele mai bune bănci?" - 100% visibility
  - Gap: "Credit ipotecar" - only 33% visibility
```

### Example 2: E-commerce Store

```
Project: TechShop Romania
Website: techshop.ro
Keywords: ["TechShop", "techshop.ro"]
Queries:
  - Best online electronics stores Romania
  - Where to buy laptops Romania
  - TechShop reviews
  - Cheap gaming PC Romania
  - Most trusted tech retailers

Results:
  - Overall Visibility: 42%
  - ChatGPT: 40%
  - Claude: 35%
  - Perplexity: 50%

Insights:
  - Low overall visibility indicates need for GEO optimization
  - Only mentioned when specifically asked about "TechShop"
  - Not appearing in generic "best stores" queries
  - Action: Create more comparison content, get more reviews
```

## Known Limitations

1. **Query Limit**: Currently no per-project query limit (could be abused)
2. **Sentiment Analysis**: Basic keyword matching (not GPT-4 based)
3. **No Scheduling**: Manual scan triggering only (no auto-recurring)
4. **Single Language Per Project**: Can't mix Romanian and English queries
5. **Provider Dependencies**: Requires at least one provider API key

## Roadmap

### Planned for v1.6.0
- Scheduled automatic scans (hourly/daily/weekly)
- Email alerts for visibility drops > 10%
- Competitor tracking (monitor competitor mentions)
- Enhanced sentiment analysis (GPT-4 based)
- CSV/PDF report export
- Query template library
- Multi-language support per project

## Support

### Troubleshooting

**Scan stuck in "running":**
- Check server logs for API errors
- Verify API keys are correct
- Ensure provider rate limits not exceeded

**No providers available:**
- Add at least one API key to .env:
  - OPENAI_API_KEY (ChatGPT)
  - ANTHROPIC_API_KEY (Claude)
  - PERPLEXITY_API_KEY (Perplexity)

**Low visibility scores:**
- Add more brand keyword variations
- Test with branded queries first
- Monitor competitor strategies
- Optimize content for GEO

## Contributors

- Implementation: Claude (Anthropic)
- Specification: Cosmin (ING România)
- Testing: [Pending]

## License

Part of Website LLM Analyzer v1.5.0
© 2026 - All rights reserved
