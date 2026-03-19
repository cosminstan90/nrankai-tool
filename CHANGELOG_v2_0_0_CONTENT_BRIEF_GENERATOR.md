# Website LLM Analyzer v2.0.0 - Content Brief Generator

## 🎯 Major Feature: Content Brief Generator

**Release Date:** February 20, 2026

### Overview
A comprehensive content brief generation system that transforms audit results into actionable, detailed content improvement plans. Each brief provides specific recommendations, SEO requirements, and GEO optimization strategies.

---

## 🆕 New Features

### 1. Content Brief Generation
- **Automated brief generation** for pages with low scores
- **AI-powered analysis** using LLM to create detailed recommendations
- **Multi-page batch generation** (up to 50 pages per request)
- **Smart page selection**: Auto-selects top 10 low-scoring pages if not specified
- **Audit-type specific prompts**: Different strategies for SEO, GEO, and Content Quality audits

### 2. Brief Structure
Each content brief includes:
- **Executive Summary**: One-paragraph explanation of issues and importance
- **Content Changes Table**: Specific sections to modify with before/after recommendations
- **SEO Requirements**:
  - Target keywords
  - Meta title (with character count)
  - Meta description (with character count)
  - H1 recommendation
  - Internal linking suggestions
  - Schema markup recommendations
- **GEO Requirements** (for GEO audits):
  - Entities to mention
  - Citations to add
  - Conversational section suggestions
  - Structured data recommendations
- **Project Management Data**:
  - Word count target
  - Estimated effort (hours)
  - Deadline suggestion (week priority)

### 3. Brief Management UI
- **Dedicated /briefs page** with intuitive interface
- **Audit selector** with dropdown of completed audits
- **Status tracking**: Generated → Approved → In Progress → Completed
- **Priority system**: Critical, High, Medium, Low
- **Collapsible cards** for each brief with full details
- **Workflow buttons**: Approve, Start Work, Complete
- **Statistics dashboard**: Total, approved, in-progress, completed counts

### 4. Integration with Results Page
- **"Generate Brief" button** on each result row
- **"Generate Briefs (Top 10)" button** in page header
- **One-click brief generation** from audit results

### 5. Export & Sharing
- **Export all briefs as JSON** for bulk processing
- **Print-friendly view** for sharing with content teams
- **Direct page URL links** in each brief

---

## 🔧 Technical Implementation

### Database Schema
**New Table: `content_briefs`**
```sql
CREATE TABLE content_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id TEXT NOT NULL REFERENCES audits(id) ON DELETE CASCADE,
    result_id INTEGER NOT NULL REFERENCES audit_results(id) ON DELETE CASCADE,
    page_url TEXT(512) NOT NULL,
    brief_json TEXT NOT NULL,
    status TEXT(20) DEFAULT 'generated',
    priority TEXT(20) DEFAULT 'medium',
    provider TEXT(20),
    model TEXT(100),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_id (audit_id),
    INDEX idx_result_id (result_id)
);
```

### API Endpoints

#### `POST /api/briefs/generate`
Generate content briefs for an audit.

**Request Body:**
```json
{
  "audit_id": "uuid",
  "page_ids": [1, 2, 3],
  "max_pages": 10,
  "provider": "anthropic",
  "model": "claude-haiku-4-5-20251001",
  "language": "Romanian",
  "focus_areas": ["seo", "content_quality", "geo_readiness"]
}
```

**Response:**
```json
{
  "status": "started",
  "audit_id": "uuid",
  "message": "Brief generation started in background",
  "max_pages": 10,
  "provider": "anthropic",
  "model": "claude-haiku-4-5-20251001"
}
```

#### `GET /api/briefs?audit_id={id}`
List all briefs for an audit.

**Response:**
```json
{
  "audit_id": "uuid",
  "total": 10,
  "briefs": [...]
}
```

#### `GET /api/briefs/{brief_id}`
Get a single brief with full details.

#### `PATCH /api/briefs/{brief_id}`
Update brief status.

**Request Body:**
```json
{
  "status": "approved"
}
```

#### `POST /api/briefs/{brief_id}/regenerate`
Regenerate a brief with different parameters.

**Request Body:**
```json
{
  "provider": "openai",
  "model": "gpt-4o",
  "language": "English",
  "focus_areas": ["seo"]
}
```

#### `GET /api/briefs/export/{audit_id}`
Export all briefs for an audit as JSON.

### LLM Integration
- **Reuses existing LLM infrastructure** from `api/routes/summary.py`
- **Custom system prompts** per audit type (SEO, GEO, Content Quality)
- **Structured JSON output** with validation
- **Default to cheapest models**: `claude-haiku-4-5-20251001` for Anthropic, `gpt-4o-mini` for OpenAI
- **Multi-language support**: All recommendations in target language
- **Error handling**: Failed briefs marked as "failed" status

### Smart Page Selection Algorithm
```python
1. If page_ids provided: Use those pages (up to max_pages)
2. Otherwise:
   a. Select pages with score < 70, sorted ascending (worst first)
   b. If fewer than max_pages, fill with scores 70-85
   c. Exclude pages without valid result_json
```

### Frontend Technologies
- **AlpineJS** for reactive UI state management
- **TailwindCSS** for styling
- **Collapsible cards** with smooth transitions
- **Priority-based sorting** (critical → high → medium → low)
- **Print-optimized CSS** for physical handoffs

---

## 📁 Modified Files

### Backend (Python)
1. **`api/models/database.py`**
   - Added `ContentBrief` model with full schema
   - Includes `to_dict()` method with JSON parsing

2. **`api/routes/content_briefs.py`** (NEW)
   - Complete router with 6 endpoints
   - Background task processing
   - Smart page selection logic
   - Audit-type specific prompt generation
   - Multi-language support

3. **`api/routes/__init__.py`**
   - Export `content_briefs_router`

4. **`api/main.py`**
   - Import `ContentBrief` model
   - Import and include `content_briefs_router`
   - Added `/briefs` template route

### Frontend (Templates)
5. **`api/templates/base.html`**
   - Added "📝 Briefs" navigation link

6. **`api/templates/results.html`**
   - Added "Generate Briefs (Top 10)" button in header
   - Added "Brief" button on each result row
   - Added JavaScript functions: `generateBrief()`, `generateBriefsTop10()`

7. **`api/templates/briefs.html`** (NEW)
   - Full content briefs management UI
   - AlpineJS app for state management
   - Audit selector with dropdown
   - Statistics dashboard
   - Collapsible brief cards
   - Content changes table
   - SEO requirements panel
   - GEO requirements panel
   - Status workflow buttons
   - Export and print functionality

---

## 🎨 UI/UX Highlights

### Color-Coded System
- **Priority Badges**:
  - 🔴 Critical: Red
  - 🟠 High: Orange
  - 🟡 Medium: Yellow
  - ⚪ Low: Gray

- **Status Badges**:
  - ⚪ Generated: Gray
  - 🔵 Approved: Blue
  - 🟣 In Progress: Purple
  - 🟢 Completed: Green
  - 🔴 Failed: Red

- **Score Badges**:
  - 🔴 < 50: Red/Poor
  - 🟡 50-69: Yellow/Needs Work
  - 🔵 70-84: Blue/Good
  - 🟢 85+: Green/Excellent

### Workflow States
```
Generated → Approved → In Progress → Completed
   ↓           ↓            ↓            ↓
 [Approve]  [Start Work]  [Complete]   [Done]
```

---

## 💡 Usage Examples

### Example 1: Generate Briefs from Results Page
1. Go to audit results page
2. Click "Generate Briefs (Top 10)"
3. System auto-selects 10 lowest-scoring pages
4. Briefs generated in background (takes 1-2 minutes)
5. Check `/briefs` page to view results

### Example 2: Generate Single Brief
1. In results table, find page with low score
2. Click "Brief" button on that row
3. Confirm generation
4. Brief appears on `/briefs` page

### Example 3: Manage Brief Workflow
1. Go to `/briefs` page
2. Select audit from dropdown
3. Review generated briefs
4. Click "Approve" on good briefs
5. Click "Start Work" when content writer begins
6. Click "Complete" when content is published

### Example 4: Export for Content Team
1. Select audit with completed briefs
2. Click "Export All as JSON"
3. Share JSON file with content management system
4. Or click "Print All Briefs" for PDF handoff

---

## 🔒 Security & Performance

### Rate Limiting
- Brief generation runs in background tasks
- 1-second delay between consecutive LLM calls to avoid rate limits
- Failed briefs don't block other briefs

### Cost Optimization
- **Default to cheapest models**:
  - Anthropic: `claude-haiku-4-5-20251001` (~$0.25 per 1M input tokens)
  - OpenAI: `gpt-4o-mini` (~$0.15 per 1M input tokens)
- Users can override with premium models if needed
- Content truncated to 3000 chars to minimize token usage

### Error Handling
- Individual brief failures don't crash entire batch
- Failed briefs marked with status="failed"
- Error messages stored in brief_json for debugging

---

## 📊 Business Value

### For Consultants
- **Deliverable content briefs** instead of just audit scores
- **Actionable recommendations** that clients can implement immediately
- **Professional documentation** ready to share with content teams
- **Workflow tracking** to monitor implementation progress

### For Content Teams
- **Clear instructions** on what to write/change
- **Specific examples** of current vs. recommended content
- **Effort estimates** for resource planning
- **Priority system** for task management

### For Clients
- **Transparent action plan** showing ROI path
- **Detailed specifications** reducing back-and-forth
- **Timeline suggestions** for realistic expectations
- **Quantified improvements** with score targets

---

## 🚀 Future Enhancements (Roadmap)

### Potential v2.1 Features
- [ ] **Brief templates** by industry (finance, e-commerce, healthcare)
- [ ] **Collaborative editing** for multi-user brief refinement
- [ ] **Integration with project management tools** (Jira, Asana, Trello)
- [ ] **Before/after tracking** to measure implemented changes
- [ ] **A/B testing recommendations** for high-impact pages
- [ ] **Video brief generation** with screen recordings
- [ ] **Bulk regeneration** with improved prompts
- [ ] **Brief versioning** to track iterations

---

## 🛠️ Installation & Migration

### Database Migration
The new `content_briefs` table is automatically created on app startup via SQLAlchemy's `create_all()`.

**No manual migration required** — just restart the app.

### Dependencies
No new dependencies required. Uses existing:
- FastAPI
- SQLAlchemy (async)
- Pydantic v2
- Anthropic/OpenAI/Mistral clients
- AlpineJS (CDN)
- TailwindCSS (CDN)

### Backward Compatibility
✅ **Fully backward compatible** with existing audits and results.
✅ Existing features unchanged.
✅ Optional feature — doesn't affect users who don't use it.

---

## 📖 Documentation

### Quick Start
```bash
# Start the app
python -m uvicorn api.main:app --reload

# Navigate to briefs page
http://localhost:8000/briefs

# Or generate from audit results
http://localhost:8000/audits/{audit_id}/results
```

### API Documentation
Full OpenAPI docs available at:
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## 🙏 Credits

**Author:** Claude (Anthropic)
**Client:** Cosmin - Senior SEO/GEO Specialist @ ING România
**Date:** February 20, 2026
**Version:** 2.0.0

---

## 📝 Notes

- **Language Support**: Prompts are in English, but all content recommendations are generated in the target language (default: Romanian)
- **Model Selection**: Uses cheapest models by default for cost efficiency. Override with `provider` and `model` parameters for premium quality.
- **Page Content**: Loads from `audits/{audit_id}/input_llm/{filename}.txt` if available. Generation works even without page content, using audit results alone.
- **Priority Auto-Detection**: Priority is calculated from score: <50=critical, 50-64=high, 65-79=medium, 80+=low

---

**Ready to deliver actionable content strategies, not just audit scores! 🚀📝**
