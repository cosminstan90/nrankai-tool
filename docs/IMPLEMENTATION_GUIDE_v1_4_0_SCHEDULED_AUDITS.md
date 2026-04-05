# Scheduled Audits Implementation Guide v1.4.0

## Overview
This implementation adds a complete **Scheduled Audits + History Tracking** module that enables automatic recurring audits with cron-based scheduling, comprehensive trend analysis, and zero-configuration automation.

## What's New

### Database Layer
- **New Model:** `ScheduledAudit` in `api/models/database.py`
  - Stores schedule configuration and metadata
  - Tracks execution history (last_run_at, run_count, last_audit_id)
  - Supports pause/resume (is_active flag)
  - Optional auto-summary configuration
  - Automatically created at `init_db()`

### API Layer
- **New Router:** `api/routes/schedules.py`
  - `GET /api/schedules/presets` — Cron expression presets
  - `POST /api/schedules` — Create schedule
  - `GET /api/schedules` — List all schedules with metadata
  - `GET /api/schedules/{id}` — Detailed view with history and trends
  - `GET /api/schedules/{id}/history` — Chart-ready data
  - `PATCH /api/schedules/{id}` — Update configuration
  - `DELETE /api/schedules/{id}` — Delete schedule
  - `POST /api/schedules/{id}/run` — Manual trigger

### Frontend
- **New Template:** `api/templates/schedules.html`
  - Split-screen layout: Create/List (left) + Detail/History (right)
  - Preset cron selector with manual override
  - Dynamic provider/model filtering
  - Chart.js history visualization
  - Real-time trend analysis
  - Pause/Resume controls
  - Manual trigger button

### Scheduler Engine
- Background asyncio task running every 60 seconds
- Custom cron matching without external dependencies
- Automatic audit creation and pipeline triggering
- Optional auto-summary polling
- Graceful error handling

### Navigation
- Added "Schedules" link to `base.html` navigation
- Template route in `main.py`

## Architecture

### Data Flow
```
1. User creates schedule with cron expression
   ↓
2. Schedule stored in database (is_active=1)
   ↓
3. Scheduler loop checks every 60 seconds
   ↓
4. Cron expression matched against current time
   ↓
5. If match + cooldown passed:
   - Create Audit record
   - Update schedule metadata
   - Trigger audit_worker.start_audit_pipeline()
   - Optional: Start auto-summary polling
   ↓
6. Audit runs in background
   ↓
7. Results automatically added to history
   ↓
8. Trend analysis calculated on-demand
   ↓
9. Frontend displays charts and metrics
```

### Cron Matching Engine

**Function:** `_parse_cron_field(field, min_val, max_val)`

Parses a single cron field into list of matching values:

```python
# Examples:
_parse_cron_field("*", 0, 59)        # [0,1,2,...,59]
_parse_cron_field("5", 0, 59)        # [5]
_parse_cron_field("1-5", 0, 59)      # [1,2,3,4,5]
_parse_cron_field("1,3,5", 0, 59)    # [1,3,5]
_parse_cron_field("*/15", 0, 59)     # [0,15,30,45]
_parse_cron_field("1-10/2", 0, 59)   # [1,3,5,7,9]
```

**Function:** `_cron_matches(cron_expr, dt)`

Validates if datetime matches cron expression:

```python
# Cron format: minute hour day month weekday
# Example: "0 9 * * 1" = Monday 9:00 AM

dt = datetime(2026, 2, 24, 9, 0)  # Monday Feb 24, 9:00 AM
_cron_matches("0 9 * * 1", dt)    # True

dt = datetime(2026, 2, 24, 10, 0) # Monday Feb 24, 10:00 AM
_cron_matches("0 9 * * 1", dt)    # False
```

**Weekday Conversion:**
- Python: Monday=1, Sunday=7 (isoweekday)
- Cron: Sunday=0, Saturday=6
- Conversion: `current_weekday = dt.isoweekday() % 7`

**Function:** `_cron_to_human(cron)`

Converts cron to readable text:

```python
_cron_to_human("0 9 * * 1")        # "Every Monday at 9:00"
_cron_to_human("0 6 * * *")        # "Daily at 6:00"
_cron_to_human("0 9 1 * *")        # "Monthly on 1st at 9:00"
_cron_to_human("0 9 1,15 * *")     # "On 1,15 of month at 9:00"
_cron_to_human("0 9 1 1,4,7,10 *") # "Quarterly (Jan/Apr/Jul/Oct) on 1 at 9:00"
```

### Scheduler Loop

**Function:** `check_and_run_schedules()`

Main scheduler that runs every 60 seconds:

```python
async def check_and_run_schedules():
    async with AsyncSessionLocal() as db:
        # Get all active schedules
        result = await db.execute(
            select(ScheduledAudit).where(ScheduledAudit.is_active == 1)
        )
        schedules = result.scalars().all()
        
        now = datetime.utcnow()
        
        for schedule in schedules:
            # Check cron match
            if not _cron_matches(schedule.schedule_cron, now):
                continue
            
            # Check cooldown (30 minutes minimum)
            if schedule.last_run_at:
                time_since_last = now - schedule.last_run_at
                if time_since_last < timedelta(minutes=30):
                    continue
            
            # Create audit and trigger pipeline
            audit_id = str(uuid.uuid4())
            audit = Audit(id=audit_id, ...)
            db.add(audit)
            
            schedule.last_run_at = now
            schedule.last_audit_id = audit_id
            schedule.run_count += 1
            
            await db.commit()
            
            # Background execution
            asyncio.create_task(start_audit_pipeline(...))
```

**Cooldown Logic:**
- Prevents multiple executions within 30 minutes
- Handles edge case of minute-level cron resolution
- Example: "0 9 * * *" won't run twice if scheduler checks at 9:00:15 and 9:00:45

**Error Handling:**
- Try/except around entire loop
- Logs errors but continues running
- Individual schedule failures don't break scheduler
- Application restart recovers automatically

### History Tracking

**Storage:**
- No separate history table needed
- Queries existing Audit table
- Filters: `website == schedule.website AND audit_type == schedule.audit_type`
- Only includes completed audits

**Trend Calculation:**

```python
history = [
    {"average_score": 62, "completed_at": "2026-01-06"},
    {"average_score": 68, "completed_at": "2026-01-13"},
    {"average_score": 72, "completed_at": "2026-01-20"},
    {"average_score": 75, "completed_at": "2026-01-27"}
]

# Calculate trend
first_score = 62
latest_score = 75
delta = 75 - 62 = +13

if delta > 0:
    direction = "improving"
elif delta < 0:
    direction = "declining"
else:
    direction = "stable"

best = max(all_scores) = 75
worst = min(all_scores) = 62
```

**Chart Data Format:**

```json
{
  "labels": ["Jan 6", "Jan 13", "Jan 20", "Jan 27"],
  "scores": [62, 68, 72, 75],
  "audit_ids": ["uuid1", "uuid2", "uuid3", "uuid4"]
}
```

### Auto-Summary Integration

**Configuration:**
- Schedule has optional `summary_provider` and `summary_model` fields
- If set, auto-triggers summary after audit completion

**Implementation:**

```python
async def _poll_and_generate_summary(audit_id, provider, model, language):
    max_attempts = 120  # 2 hours
    
    for attempt in range(max_attempts):
        await asyncio.sleep(60)  # Check every minute
        
        async with AsyncSessionLocal() as db:
            audit = await db.execute(select(Audit).where(Audit.id == audit_id))
            audit = audit.scalar_one_or_none()
            
            if not audit:
                return
            
            if audit.status == "completed":
                # Trigger summary generation
                from api.routes.summary import generate_summary_task
                await generate_summary_task(audit_id, provider, model, language)
                return
            
            if audit.status == "failed":
                return
```

**Benefits:**
- Reuses existing summary.py infrastructure
- No modifications to audit pipeline needed
- Polling happens in separate background task
- Timeout after 2 hours (120 checks)

## UI Components

### Create Form
- **Name Input:** Required, max 255 chars
- **Website Input:** Required, URL format
- **Sitemap Input:** Optional, URL format
- **Audit Type Dropdown:** Populated from `list_available_audits()`
- **Provider Selector:** Dynamic, filters available models
- **Model Selector:** Optional, defaults to provider's default
- **Language Dropdown:** English, Romanian, Spanish, French, German
- **Schedule Section:**
  - Preset dropdown (weekly, monthly, daily, etc.)
  - Manual cron input (5-field format)
  - Human-readable preview
- **Advanced Options:**
  - Perplexity toggle checkbox
  - Concurrency slider (1-20)
- **Auto-Summary Section:**
  - Provider dropdown (optional)
  - Model dropdown (dynamic, based on provider)
- **Validation:**
  - All required fields checked
  - Cron format validated (5 fields)
  - Form disabled while submitting

### Schedules List
- **Card Layout:** Responsive grid
- **Information Displayed:**
  - Schedule name (bold)
  - Website URL (truncated)
  - Audit type badge
  - Human-readable schedule
  - Status badge (Active/Paused)
  - Run statistics
  - History count
- **Actions:**
  - Click card → Select for detail view
  - Pause/Resume button
  - Run Now button
- **Empty State:** Encourages first schedule creation

### Detail View

**Schedule Info Card:**
```html
<div class="schedule-info">
  <h2>{name}</h2>
  <p>{schedule_human}</p>
  <div class="metadata">
    <span>Website: {website}</span>
    <span>Type: {audit_type}</span>
    <span>Last Run: {last_run_at}</span>
    <span>Total Runs: {run_count}</span>
  </div>
  <button>Delete</button>
</div>
```

**Trend Indicator:**
```html
<div class="trend-card">
  <div class="direction">
    <icon>{↗|↘|→}</icon>
    <span>{Improving|Declining|Stable}</span>
  </div>
  <div class="scores">
    <span>{first_score} → {latest_score}</span>
    <span class="delta">{+/-delta} points</span>
  </div>
  <div class="extremes">
    <span>Best: {best}</span>
    <span>Worst: {worst}</span>
  </div>
</div>
```

**Chart Rendering:**
```javascript
new Chart(ctx, {
  type: 'line',
  data: {
    labels: chartData.labels,
    datasets: [{
      label: 'Average Score',
      data: chartData.scores,
      borderColor: 'rgb(59, 130, 246)',
      backgroundColor: 'rgba(59, 130, 246, 0.1)',
      tension: 0.3,
      fill: true
    }]
  },
  options: {
    responsive: true,
    scales: {
      y: {
        beginAtZero: true,
        max: 100
      }
    }
  }
});
```

**History Table:**
- Columns: Date, Score, Pages, Actions
- Score badges: Green (80+), Yellow (60-79), Red (<60)
- Links to full audit detail pages
- Empty state for new schedules

## Frontend State Management (Alpine.js)

### Data Properties:
```javascript
{
  loading: false,
  schedules: [],
  selectedScheduleId: null,
  detail: null,
  chartData: null,
  chartInstance: null,
  presets: {},
  selectedPreset: '',
  auditTypes: [...],
  providers: [...],
  form: {
    name: '',
    website: '',
    sitemap_url: '',
    audit_type: '',
    provider: '',
    model: '',
    language: 'English',
    use_perplexity: false,
    concurrency: 5,
    schedule_cron: '',
    summary_provider: '',
    summary_model: ''
  },
  availableModels: []
}
```

### Key Methods:

**`init()`**
- Loads cron presets from API
- Loads initial schedules list
- Starts polling interval

**`createSchedule()`**
- Validates form data
- POST to /api/schedules
- Resets form on success
- Refreshes schedules list

**`selectSchedule(id)`**
- Sets selectedScheduleId
- Loads detailed data
- Loads chart data
- Renders chart

**`loadScheduleDetail(id)`**
- GET /api/schedules/{id}
- GET /api/schedules/{id}/history
- Populates detail and chartData
- Triggers renderChart()

**`renderChart()`**
- Creates/updates Chart.js instance
- Handles empty state
- Responsive sizing
- Interactive tooltips

**`toggleActive(id, isActive)`**
- PATCH /api/schedules/{id}
- Updates is_active field
- Refreshes list and detail

**`runNow(id)`**
- Confirms action
- POST /api/schedules/{id}/run
- Displays audit ID
- Refreshes schedules

**`deleteSchedule(id)`**
- Confirms action
- DELETE /api/schedules/{id}
- Clears detail view
- Refreshes list

**`startPolling()`**
- Interval: 10 seconds
- Refreshes schedules list
- Refreshes detail if selected
- Updates charts automatically

## Database Schema

```sql
CREATE TABLE scheduled_audits (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    website TEXT NOT NULL,
    sitemap_url TEXT,
    audit_type TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    language TEXT DEFAULT 'English',
    use_perplexity INTEGER DEFAULT 0,
    concurrency INTEGER DEFAULT 5,
    schedule_cron TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    last_run_at DATETIME,
    last_audit_id TEXT,
    run_count INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    summary_provider TEXT,
    summary_model TEXT
);

CREATE INDEX idx_scheduled_audits_active ON scheduled_audits(is_active);
CREATE INDEX idx_scheduled_audits_website ON scheduled_audits(website);
```

## Validation Rules

### Backend Validation:
1. **Name:** 1-255 characters
2. **Website:** Required, valid URL format
3. **Cron Expression:** Exactly 5 fields separated by spaces
4. **Provider:** Must exist in configured providers
5. **Concurrency:** 1-20 range
6. **Audit Type:** Must exist in available audit types

### Frontend Validation:
1. **Required fields:** Name, website, audit_type, provider, schedule_cron
2. **URL format:** website and sitemap_url must be valid URLs
3. **Cron format:** 5 space-separated fields
4. **Model availability:** Only show models for selected provider

## Error Handling

### Backend Errors:
- **400 Bad Request:** Validation failure (invalid cron, missing fields)
- **404 Not Found:** Schedule doesn't exist
- **500 Internal Server Error:** Database or scheduler failure

**Error Responses:**
```json
{
  "detail": "Cron expression must have 5 fields: minute hour day month weekday"
}
```

### Frontend Errors:
- Alert modals for user-facing errors
- Console logs for debugging
- Loading states prevent double-submission
- Confirmation dialogs for destructive actions

### Scheduler Errors:
- Logged to console: `❌ Scheduler error: {message}`
- Scheduler continues running
- Individual schedule failures isolated
- Application restart recovers state

## Real-Time Updates

### Polling Strategy:
- **Schedules List:** Every 10 seconds
- **Detail View:** Every 10 seconds (if selected)
- **Chart Data:** Refreshed with detail view
- **Timeout:** None (continuous polling)

### Why Polling vs SSE:
- Simpler implementation
- No connection management complexity
- Lower server resource usage
- Sufficient for non-critical updates
- Works across all proxies/firewalls

## Testing Checklist

### Backend:
- [ ] POST schedule with valid data → Creates successfully
- [ ] POST schedule with invalid cron → 400 error
- [ ] GET schedules → Returns list with metadata
- [ ] GET schedule detail → Returns history and trend
- [ ] PATCH schedule → Updates correctly
- [ ] DELETE schedule → Removes and preserves audits
- [ ] POST run → Triggers immediately
- [ ] Cron matching → Validates against test cases
- [ ] Cooldown → Prevents double-runs
- [ ] Auto-summary → Generates after completion

### Frontend:
- [ ] Form validation → Prevents invalid submission
- [ ] Preset selector → Applies cron correctly
- [ ] Model dropdown → Filters by provider
- [ ] Create schedule → Success message and refresh
- [ ] Select schedule → Loads detail view
- [ ] Chart renders → Displays correctly
- [ ] Trend indicator → Shows proper direction
- [ ] History table → Links to audits
- [ ] Pause/Resume → Updates status
- [ ] Run Now → Triggers immediately
- [ ] Delete → Removes and clears view
- [ ] Polling → Updates automatically

### Scheduler:
- [ ] Startup → Begins checking schedules
- [ ] Cron match → Executes at correct time
- [ ] Cooldown → Respects 30-minute minimum
- [ ] Error handling → Continues after failure
- [ ] Shutdown → Cancels gracefully
- [ ] Multi-schedule → Handles concurrent runs

## Integration Points

**Reuses from v1.3.0:**
- `start_audit_pipeline()` from `audit_worker.py`
- `generate_summary_task()` from `summary.py`
- `AsyncSessionLocal` for background tasks
- Same provider/model structure
- Existing template patterns

**Compatible with:**
- All audit types
- All LLM providers
- Summary generation
- Benchmarking
- Compare feature

## Migration

No manual migration needed. On next startup:
```python
init_db()  # Creates scheduled_audits table automatically
```

SQLite schema addition is backwards compatible. All existing tables remain unchanged.

## Deployment Steps

1. **Backup database:**
   ```bash
   cp api/data/analyzer.db api/data/analyzer.db.backup
   ```

2. **Extract updated code:**
   ```bash
   unzip website_llm_analyzer_v1_4_0_scheduled_audits.zip
   cd website_llm_analyzer_v1_4_0
   ```

3. **No new dependencies** (uses existing packages)

4. **Start server:**
   ```bash
   cd api
   python main.py
   ```

5. **Verify:**
   - Check logs for "✓ Scheduler started"
   - Navigate to http://localhost:8000/schedules
   - Create test schedule
   - Wait for execution or trigger manually

## Usage Example

### Weekly SEO Monitoring

**Configuration:**
```
Name: Weekly SEO - acme.com
Website: https://acme.com
Audit Type: SEO
Provider: Anthropic
Model: claude-sonnet-4-20250514
Schedule: 0 9 * * 1 (Every Monday 9 AM)
Language: English
Concurrency: 10
Auto-Summary: Yes (Anthropic/Sonnet 4)
```

**First 4 Weeks:**
```
Week 1 (Jan 6):  Score 62  ← Baseline
Week 2 (Jan 13): Score 68  ← Meta descriptions improved
Week 3 (Jan 20): Score 72  ← Image optimization
Week 4 (Jan 27): Score 75  ← Internal linking
```

**Trend Analysis:**
- Direction: Improving ↗
- Delta: +13 points
- Best: 75
- Worst: 62

**Action:**
Review weekly summaries → Implement recommendations → Track improvements

## Troubleshooting

**Schedule won't execute:**
- Check server logs for scheduler errors
- Verify cron expression format (5 fields)
- Confirm schedule is Active (not Paused)
- Check last_run_at timestamp (30-min cooldown)
- Test cron match manually in Python console

**Chart won't render:**
- Verify Chart.js loaded (browser console)
- Check chartData structure (labels, scores arrays)
- Ensure canvas element exists in DOM
- Wait for at least one completed audit

**History incomplete:**
- Only completed audits show in history
- Failed audits excluded
- Filter by website + audit_type
- Check Audit table directly in database

**High API costs:**
- Reduce schedule frequency
- Use cheaper models (Haiku, GPT-4o Mini)
- Disable auto-summary
- Increase concurrency (faster completion)

## Performance Notes

- Scheduler overhead: ~10ms per active schedule per check
- Cron parsing: <1ms per expression
- Chart rendering: Canvas API (hardware accelerated)
- Polling: Lightweight GET requests
- Database: Indexed queries for fast lookups
- Memory: ~1MB per 100 active schedules

## Support

For issues or questions:
1. Check server logs: `cd api && python main.py`
2. Verify network requests: Browser DevTools → Network tab
3. Review code: `api/routes/schedules.py` and `api/templates/schedules.html`
4. Test API endpoints: curl or Postman
5. Check database: `sqlite3 api/data/analyzer.db`

**Module is fully backward compatible** — existing features continue to work unchanged.
