# GEO Visibility Monitor - Implementation Guide v1.5.0

## Overview

The GEO Visibility Monitor is a powerful feature that tracks your brand's visibility across AI-powered search platforms (ChatGPT, Claude, Perplexity). It provides quantitative data to justify ROI for SEO/GEO consulting services.

## What's New in v1.5.0

### New Components

1. **Database Models** (`api/models/database.py`)
   - `GeoMonitorProject` - Stores monitoring projects with brand keywords and test queries
   - `GeoMonitorScan` - Individual scan runs with visibility scores and detailed results

2. **API Routes** (`api/routes/geo_monitor.py`)
   - POST `/api/geo-monitor/projects` - Create new monitoring project
   - GET `/api/geo-monitor/projects` - List all projects with latest scores
   - GET `/api/geo-monitor/projects/{id}` - Get project details and scan history
   - DELETE `/api/geo-monitor/projects/{id}` - Delete project
   - POST `/api/geo-monitor/projects/{id}/scan` - Start new visibility scan
   - GET `/api/geo-monitor/scans/{scan_id}` - Get detailed scan results
   - GET `/api/geo-monitor/projects/{id}/trend` - Get Chart.js trend data

3. **Web Interface** (`api/templates/geo_monitor.html`)
   - Project creation form with keyword tagging
   - Query suggestion generator
   - Real-time scan progress tracking
   - Visibility score dashboard with gauges
   - Provider breakdown (ChatGPT/Claude/Perplexity)
   - Historical trend charts
   - Query-by-query results matrix
   - Response detail modal with context snippets

4. **Navigation** (`api/templates/base.html`)
   - Added "🌐 GEO Monitor" to main navigation

## Features

### Core Functionality

1. **Multi-Provider Monitoring**
   - Queries ChatGPT (gpt-4o-mini), Claude (Sonnet 4), and Perplexity (Sonar)
   - Concurrent API calls with rate limiting (max 3 simultaneous, 1s delay per provider)
   - Error resilience - one provider failure doesn't block entire scan

2. **Visibility Analysis**
   - **Mention Detection** - Case-insensitive keyword matching
   - **Citation Detection** - URL presence in responses
   - **Context Extraction** - 200 char snippets around mentions
   - **Position Classification** - primary_recommendation, listed, mentioned_in_passing, not_found
   - **Sentiment Analysis** - Basic positive/neutral/negative detection

3. **Metrics & Reporting**
   - **Visibility Score** - Percentage of queries where brand is mentioned (0-100%)
   - **Provider Scores** - Individual scores per AI platform
   - **Mention Count** - Total brand mentions across all checks
   - **Citation Count** - Times brand was cited with URL
   - **Historical Trends** - Chart.js visualization of score evolution

### User Interface

1. **Project Creation**
   - Name, website URL, language selection
   - Tag-based keyword input (add/remove chips)
   - Multi-line query textarea
   - Auto-suggestion generator (10 queries based on domain/language)
   - Provider toggles (only enabled if API key configured)

2. **Project Dashboard**
   - Card-based project list
   - Large visibility score display (color-coded: green ≥70%, yellow ≥40%, red <40%)
   - Last scan date and total scan count
   - Quick "Run Scan" and "Delete" actions

3. **Detailed View**
   - Overall visibility gauge (0-100%)
   - Delta since previous scan (↗/↘)
   - Total mentions and citations cards
   - Provider breakdown with progress bars
   - Multi-line trend chart (overall + per-provider)
   - Query results matrix (✅ mentioned, 🔗 cited, ❌ not found)
   - Clickable cells for full response detail modal

## Cost Estimation

### Per Scan Costs

- 10 queries × 3 providers = 30 API calls
- ~1500 tokens output per call = ~45,000 tokens total

**Using cost-effective models (gpt-4o-mini, claude-haiku):**
- OpenAI gpt-4o-mini: ~$0.01 per scan
- Anthropic claude-haiku: ~$0.02 per scan
- Perplexity Sonar: ~$0.01 per scan
- **Total: ~$0.03-0.04 per scan**

**Using premium models (gpt-4o, claude-sonnet):**
- Total: ~$0.40-0.50 per scan

## Configuration

### Required API Keys

Add to `.env` file (at least one provider required):

```env
# For ChatGPT monitoring
OPENAI_API_KEY=sk-...

# For Claude monitoring
ANTHROPIC_API_KEY=sk-ant-...

# For Perplexity monitoring
PERPLEXITY_API_KEY=pplx-...
```

### Provider Status

The UI automatically detects configured providers:
- ✓ Green badge = Provider configured and available
- Gray badge = Provider not configured (API key missing)

## Usage Workflow

### 1. Create Project

```
Navigate to: /geo-monitor
Fill form:
  - Name: "ING Romania GEO Monitor"
  - Website: "ing.ro"
  - Keywords: ["ING", "ING Bank", "ing.ro", "ING Romania"]
  - Language: Romanian
  - Queries: (use generator or enter manually)
  - Providers: [x] ChatGPT [x] Claude [x] Perplexity
Click: Create Project
```

### 2. Run Scan

```
From dashboard: Click "Run Scan" on project card
Or from detail view: Click "Run New Scan" button

Background task starts:
  - Status: pending → running → completed
  - Progress: Real-time counter (X/Y queries)
  - Duration: ~2-3 minutes for 10 queries × 3 providers
```

### 3. Analyze Results

```
View overall visibility score (0-100%)
Compare provider scores (which AI platforms mention you most?)
Check trend over time (improving or declining?)
Drill down into query-by-query results
Click cells to see full AI responses
Identify gaps (queries where you're not mentioned)
```

### 4. Export & Report

```
Use screenshots or browser print (PDF) for client reports
Show visibility score trend to justify GEO optimization ROI
Highlight strong performers (Perplexity 80%) vs weak (Claude 40%)
Recommend optimization strategies based on gaps
```

## Technical Implementation

### Background Scan Engine

```python
async def _run_geo_scan(scan_id, project_id, providers_override):
    1. Load project and create scan record (status="running")
    2. For each query × provider combination:
       a. Query the LLM with conversational prompt
       b. Analyze response for mentions/citations
       c. Extract context snippets and classify position
       d. Store result per check
    3. Calculate aggregates:
       - visibility_score = (mentions / total_checks) × 100
       - provider_scores = per-provider breakdown
       - mention_count, citation_count
    4. Update scan (status="completed")
```

### Concurrency Control

- **Semaphore**: Max 3 concurrent API calls
- **Rate Limiting**: 1 second delay between calls to same provider
- **Error Handling**: Failed calls marked as errors, don't block scan
- **Total Duration**: ~2-3 minutes for 30 checks (10 queries × 3 providers)

### Analysis Algorithm

```python
def _analyze_response(response_text, brand_keywords, website):
    1. Keyword Matching (case-insensitive)
    2. URL Detection (cited = True if website found)
    3. Context Extraction (200 chars around first mention)
    4. Position Classification:
       - primary_recommendation: keyword in first 25% of response
       - listed: keyword appears 2+ times
       - mentioned_in_passing: keyword appears once
       - not_found: no keyword match
    5. Sentiment Detection (basic positive/negative keyword matching)
```

## Database Schema

### GeoMonitorProject

```sql
CREATE TABLE geo_monitor_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT NOT NULL,
    brand_keywords TEXT NOT NULL,      -- JSON array
    target_queries TEXT NOT NULL,       -- JSON array
    providers_config TEXT NOT NULL,     -- JSON object
    language TEXT DEFAULT 'English',
    created_at DATETIME,
    updated_at DATETIME
);
```

### GeoMonitorScan

```sql
CREATE TABLE geo_monitor_scans (
    id TEXT PRIMARY KEY,
    project_id TEXT REFERENCES geo_monitor_projects(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'pending',
    total_checks INTEGER DEFAULT 0,
    completed_checks INTEGER DEFAULT 0,
    visibility_score REAL,
    mention_count INTEGER DEFAULT 0,
    citation_count INTEGER DEFAULT 0,
    results_json TEXT,                  -- JSON array
    provider_scores TEXT,                -- JSON object
    started_at DATETIME,
    completed_at DATETIME,
    created_at DATETIME
);
```

## Validation

All files validated:
- ✅ Python syntax (ast.parse)
- ✅ Jinja2 templates (Environment().parse)
- ✅ Pydantic v2 validators (@field_validator)
- ✅ SQLAlchemy async patterns
- ✅ AlpineJS x-data bindings

## Browser Compatibility

- Chrome/Edge: ✅ Full support
- Firefox: ✅ Full support
- Safari: ✅ Full support (requires modern version for AlpineJS)
- Mobile: ✅ Responsive design with TailwindCSS

## Future Enhancements

Potential additions for v1.6.0:
- Scheduled automatic scans (hourly/daily/weekly)
- Email alerts for visibility drops
- Competitor comparison (track competitor mentions)
- More advanced sentiment analysis (GPT-4 based)
- Export to CSV/PDF reports
- Custom prompt templates per query
- Integration with Google Analytics/Search Console
- A/B testing different query variations

## Support

For issues or questions:
1. Check console logs (browser DevTools)
2. Check server logs (terminal running uvicorn)
3. Verify API keys are configured in `.env`
4. Ensure at least one provider is enabled
5. Check network tab for failed API calls

## License

Part of Website LLM Analyzer v1.5.0
© 2026 - All rights reserved
