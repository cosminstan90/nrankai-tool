# CHANGELOG v2.1.0 - WHITE-LABEL PDF REPORTS

## 🎨 White-Label PDF Reports Feature

**Release Date:** February 20, 2026
**Version:** 2.1.0

### 📋 Overview

This release introduces professional **White-Label PDF Reports** with customizable branding, replacing the previous HTML-only printable reports with server-side generated PDF documents using ReportLab.

### ✨ New Features

#### 1. **BrandingConfig Model**
- New database model for storing branding configurations
- Supports multiple brand profiles with one default
- Fields:
  - Agency name and tagline
  - Logo upload (PNG/JPEG)
  - Color customization (primary, secondary, text)
  - Footer text and contact information
  - Default brand selection

#### 2. **PDF Generation Engine**
- Server-side PDF generation using ReportLab
- Professional layout with branded cover page
- Multiple page sections:
  - **Cover Page:** Logo, agency name, tagline, audit info
  - **Executive Summary:** Score gauge, metrics, distribution chart
  - **AI Insights:** Key findings and action plan (if available)
  - **Detailed Results:** Paginated table with color-coded scores
  - **Content Briefs:** Optional optimization recommendations
- Custom footer on every page with agency branding

#### 3. **Branding Management UI**
- Dedicated `/branding` page for brand configuration
- Live preview of PDF cover page
- Color picker with hex code sync
- Logo upload with instant preview
- List of saved branding configurations
- Set default brand for automatic use

#### 4. **PDF Export Options**
- Multiple export formats available:
  - **Standard PDF:** Results only
  - **PDF with AI Summary:** Results + executive insights
  - **Full PDF Report:** Summary + content briefs
- Dropdown menu in audit detail page
- Direct download button in results page

#### 5. **Visual Enhancements**
- Score gauge drawing (circular with colored arc)
- Score distribution horizontal bar chart
- Color-coded results table
- Professional typography and spacing
- Branded color scheme throughout PDF

### 🔧 Technical Implementation

#### New Files

1. **`api/routes/pdf_reports.py`**
   - Full PDF generation router
   - Branding CRUD endpoints
   - ReportLab-based PDF engine

2. **`api/templates/branding.html`**
   - Branding management interface
   - Live preview functionality
   - Color picker integration

3. **`api/static/uploads/logos/`**
   - Directory for uploaded brand logos

#### Modified Files

1. **`api/models/database.py`**
   - Added `BrandingConfig` model

2. **`api/routes/__init__.py`**
   - Exported `pdf_reports_router`

3. **`api/main.py`**
   - Included PDF reports router
   - Added `/branding` template route

4. **`api/templates/base.html`**
   - Added "Branding" navigation link

5. **`api/templates/audit_detail.html`**
   - Added PDF download dropdown with 3 options

6. **`api/templates/results.html`**
   - Added "Download PDF" button

### 🎯 API Endpoints

#### Branding Management
- `POST /api/reports/branding` - Create/update branding config
- `GET /api/reports/branding` - List all branding configs
- `GET /api/reports/branding/{id}` - Get specific branding
- `DELETE /api/reports/branding/{id}` - Delete branding
- `PATCH /api/reports/branding/{id}/set-default` - Set as default

#### PDF Generation
- `GET /api/reports/{audit_id}/pdf` - Generate and download PDF
  - Query params:
    - `branding_id`: Optional branding config ID
    - `include_summary`: Include AI summary (default: true)
    - `include_briefs`: Include content briefs (default: false)
    - `include_details`: Include full JSON details (default: false)

### 🎨 Design Features

#### PDF Cover Page
- Optional logo placement (centered, top)
- Agency name in primary color
- Tagline in italic secondary color
- Horizontal separator line
- "WEBSITE AUDIT REPORT" title
- Audit metadata (type, website, date, pages)
- "Prepared by" section with contact info

#### Executive Summary Page
- Circular score gauge with colored arc
- 4-metric overview table
- Horizontal bar chart for score distribution
- AI-generated executive summary text (if available)

#### Results Section
- Paginated table (30 rows per page)
- Columns: Page URL | Score | Classification
- Color-coded scores (green/blue/yellow/red)
- Automatic page breaks

#### Optional Sections
- **Key Findings Table:** Finding | Impact | Category
- **Action Plan:** Weekly breakdown with tasks
- **Content Briefs:** Per-page optimization details

### 💡 Usage Guide

#### Setting Up Branding
1. Navigate to `/branding` in the app
2. Fill in brand details (name, agency name, tagline)
3. Upload logo (PNG/JPEG recommended)
4. Customize colors using color pickers
5. Set footer text and contact information
6. Check "Set as default" for automatic use
7. Click "Save Branding"

#### Generating PDF Reports
1. Complete an audit
2. Go to audit detail page or results page
3. Click "Download PDF" (or dropdown for options)
4. Select desired PDF format:
   - Standard (results only)
   - With AI Summary (recommended)
   - Full Report (summary + briefs)
5. PDF downloads automatically with branded styling

#### Managing Multiple Brands
- Create separate branding configs for different clients
- Each config stores its own logo and color scheme
- Set one as default for quick exports
- Select specific branding via API parameter

### 📊 Benefits

1. **Professional Presentation**
   - White-label reports with client branding
   - Consistent, high-quality PDF output
   - Suitable for client delivery

2. **Time Savings**
   - Automated report generation
   - No manual formatting needed
   - Instant branded PDFs

3. **Flexibility**
   - Multiple brand profiles
   - Customizable report sections
   - Different export options

4. **Client-Ready**
   - Confidential footer text
   - Contact information included
   - Professional typography

### 🔐 Security & Storage

- Logos stored in `/api/static/uploads/logos/`
- Filename format: `{timestamp}_{brand_name}.{ext}`
- Automatic cleanup on branding deletion
- File type validation (PNG/JPEG only)
- SQLite storage for branding metadata

### 📝 Notes

- Default branding auto-created if none exists
- Uses ReportLab (already in requirements.txt)
- Backward compatible with existing features
- HTML report still available at `/audits/{id}/report`
- PDF filename format: `{website}_{type}_{date}.pdf`

### 🚀 Future Enhancements (Roadmap)

- Multi-page logo support (header/footer variations)
- Custom fonts upload
- Advanced chart customization
- Report templates library
- Email delivery integration
- Batch PDF generation
- Watermark support

### 🐛 Known Issues

- Large logos may exceed recommended dimensions (inform user)
- PDF generation time increases with content briefs (expected)
- Color picker may show slight differences between browsers

### 📚 Dependencies

- **ReportLab** (>=4.0.0) - Already in requirements.txt
- No additional packages required

---

## Migration Notes

### For Existing Installations

No manual migration needed. New `branding_configs` table created automatically on startup via SQLAlchemy's `create_all()`.

### For Developers

```python
# Example: Programmatic branding creation
from api.models.database import BrandingConfig, get_db_session

branding = BrandingConfig(
    name="My Agency",
    agency_name="Digital Marketing Excellence",
    tagline="Your Success, Our Mission",
    primary_color="#1e40af",
    secondary_color="#3b82f6",
    text_color="#1e293b",
    is_default=1
)

async with get_db_session() as db:
    db.add(branding)
    await db.commit()
```

### Testing

```bash
# Test PDF generation
curl "http://localhost:8000/api/reports/{audit_id}/pdf?include_summary=true" -o test.pdf

# Test branding creation
curl -X POST "http://localhost:8000/api/reports/branding" \
  -F "name=Test Brand" \
  -F "agency_name=Test Agency" \
  -F "is_default=true"
```

---

**Contributors:** Claude (AI Assistant)
**Documentation:** Complete implementation guide included
