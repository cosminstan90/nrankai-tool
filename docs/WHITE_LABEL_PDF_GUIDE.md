# White-Label PDF Reports - Implementation Guide

## 📑 Overview

The **White-Label PDF Reports** feature enables professional, branded PDF report generation for website audits. Replace generic HTML reports with customizable PDF documents featuring your agency's branding, logo, and color scheme.

## 🎯 Key Features

### 1. **Customizable Branding**
- Upload agency logo (PNG/JPEG)
- Configure primary, secondary, and text colors
- Set agency name and tagline
- Customize footer text
- Add contact information (email, website)
- Support multiple brand profiles

### 2. **Professional PDF Layout**
- **Cover Page:** Logo, agency branding, audit metadata
- **Executive Summary:** Score gauge, metrics, distribution chart
- **AI Insights:** Key findings and action plan (optional)
- **Results Table:** Paginated, color-coded scores
- **Content Briefs:** Page-by-page optimization guide (optional)
- **Branded Footer:** Agency info on every page

### 3. **Flexible Export Options**
- Standard PDF (results only)
- PDF with AI Summary (recommended)
- Full PDF Report (summary + content briefs)

## 🚀 Quick Start

### Setup Branding (First Time)

1. Navigate to **Branding** in the navigation menu
2. Click "Create New Branding"
3. Fill in the form:
   ```
   Brand Name: My Agency
   Agency Name: Professional SEO Consulting
   Tagline: Excellence in Digital Optimization
   Primary Color: #1e40af (blue)
   Secondary Color: #3b82f6 (lighter blue)
   Text Color: #1e293b (dark gray)
   Footer: Confidential — Prepared exclusively for client use
   Contact Email: contact@myagency.com
   Website: https://myagency.com
   ```
4. Upload your logo (recommended: 400x200px, transparent background)
5. Check "Set as default branding"
6. Click "Save Branding"

### Generate PDF Report

#### From Audit Detail Page:
1. Complete an audit
2. Go to the audit detail page
3. Click "Download PDF" dropdown
4. Select format:
   - **Standard PDF:** Quick results-only export
   - **PDF with AI Summary:** Best for client presentations
   - **Full PDF Report:** Complete report with optimization details

#### From Results Page:
1. Navigate to results page
2. Click "Download PDF" button
3. PDF downloads automatically

## 📐 Architecture

### Database Model

```python
class BrandingConfig(Base):
    __tablename__ = "branding_configs"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100))                    # Brand identifier
    is_default = Column(Integer, default=0)       # Default brand flag
    agency_name = Column(String(255))             # Display name
    tagline = Column(String(255))                 # Subtitle
    logo_path = Column(String(512))               # Relative path to logo
    primary_color = Column(String(7))             # Hex color
    secondary_color = Column(String(7))           # Hex color
    text_color = Column(String(7))                # Hex color
    footer_text = Column(String(512))             # Page footer
    contact_email = Column(String(255))
    contact_website = Column(String(255))
    created_at = Column(DateTime)
```

### API Endpoints

#### Branding Management

```http
POST /api/reports/branding
Content-Type: multipart/form-data

Create or update branding configuration.
Body: Form data with text fields + optional logo file
Returns: BrandingConfig object
```

```http
GET /api/reports/branding

List all branding configurations.
Returns: Array of BrandingConfig objects
```

```http
GET /api/reports/branding/{id}

Get specific branding configuration.
Returns: BrandingConfig object
```

```http
DELETE /api/reports/branding/{id}

Delete branding configuration.
Returns: Success confirmation
```

```http
PATCH /api/reports/branding/{id}/set-default

Set branding as default.
Returns: Success confirmation
```

#### PDF Generation

```http
GET /api/reports/{audit_id}/pdf

Generate and download PDF report.

Query Parameters:
  - branding_id (optional): Specific brand ID (uses default if omitted)
  - include_summary (bool, default: true): Include AI summary
  - include_briefs (bool, default: false): Include content briefs
  - include_details (bool, default: false): Include full JSON details

Returns: StreamingResponse with PDF file
Content-Type: application/pdf
Content-Disposition: attachment; filename={website}_{type}_{date}.pdf
```

### PDF Generation Flow

```
User Request
    ↓
Fetch Audit + Results
    ↓
Fetch/Create Default Branding
    ↓
Optional: Fetch AI Summary
    ↓
Optional: Fetch Content Briefs
    ↓
Generate PDF with ReportLab
    ↓
Stream PDF to User
```

## 🎨 PDF Layout Details

### Cover Page
```
┌─────────────────────────────────┐
│         [Agency Logo]           │
│                                 │
│    [Agency Name - Primary]      │
│    [Tagline - Secondary]        │
│    ────────────────────         │
│                                 │
│   WEBSITE AUDIT REPORT          │
│                                 │
│   Audit Type: SEO               │
│   Website: example.com          │
│   Generated: Feb 20, 2026       │
│   Pages Analyzed: 50            │
│                                 │
│   Prepared by: Agency Name      │
│   Contact: email@agency.com     │
│   Website: agency.com           │
└─────────────────────────────────┘
```

### Executive Summary Page
```
┌─────────────────────────────────┐
│   Executive Summary             │
│                                 │
│        Overall Score            │
│           ┌───┐                 │
│         │  85  │                │
│           └───┘                 │
│                                 │
│ ┌────────────────────────────┐  │
│ │ Pages | Avg | Good | Poor │  │
│ │  50   | 85  |  40  |  2   │  │
│ └────────────────────────────┘  │
│                                 │
│   Score Distribution            │
│   Excellent (80-100) ████ 35    │
│   Good (60-79)       ██ 12      │
│   Needs Work (40-59) █ 3        │
│   Poor (0-39)        ▌ 0        │
└─────────────────────────────────┘
```

### Results Table
```
┌─────────────────────────────────┐
│   Detailed Results              │
│                                 │
│ ┌─────────────┬────┬──────────┐ │
│ │ Page URL    │Scr │ Class    │ │
│ ├─────────────┼────┼──────────┤ │
│ │ /page1      │ 92 │Excellent │ │
│ │ /page2      │ 88 │Excellent │ │
│ │ /page3      │ 75 │Good      │ │
│ │ ...         │... │...       │ │
│ └─────────────┴────┴──────────┘ │
│                                 │
│ [Auto page break every 30 rows] │
└─────────────────────────────────┘
```

## 🔧 Technical Implementation

### ReportLab Integration

```python
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor
from reportlab.graphics.shapes import Drawing, Circle, Wedge

# Create PDF document
buffer = io.BytesIO()
doc = SimpleDocTemplate(
    buffer,
    pagesize=A4,
    rightMargin=2*cm,
    leftMargin=2*cm,
    topMargin=2.5*cm,
    bottomMargin=2.5*cm
)

# Custom styles with branding colors
primary_color = HexColor(branding.primary_color)
title_style = ParagraphStyle(
    'CustomTitle',
    fontSize=28,
    textColor=primary_color,
    alignment=TA_CENTER
)

# Build story (content elements)
story = []
story.append(Paragraph("REPORT TITLE", title_style))
story.append(Table(data, colWidths=[10*cm, 2*cm, 4*cm]))

# Generate PDF
doc.build(story, onFirstPage=add_footer, onLaterPages=add_footer)
```

### Score Gauge Drawing

```python
def draw_score_gauge(score: float, size: int = 150) -> Drawing:
    d = Drawing(size, size)
    cx, cy = size / 2, size / 2
    radius = size / 3
    
    # Background circle
    bg_circle = Circle(cx, cy, radius, strokeColor=HexColor("#e5e7eb"), 
                       strokeWidth=12, fillColor=None)
    d.add(bg_circle)
    
    # Colored arc based on score
    color = get_score_color(score)
    angle = (score / 100) * 360
    wedge = Wedge(cx, cy, radius, 90, 90 - angle, 
                  strokeColor=color, strokeWidth=12, fillColor=None)
    d.add(wedge)
    
    # Score text in center
    score_text = GraphicsString(cx, cy, f"{score:.0f}", 
                                fontSize=size / 3, 
                                textAnchor="middle")
    d.add(score_text)
    
    return d
```

## 📊 Use Cases

### 1. **Agency White-Label Reports**
Create branded reports for each client with their logo removed and your agency branding.

**Setup:**
- Create brand profile: "Client A Deliverables"
- Use client's color scheme
- Add your agency contact info
- Generate PDF with `include_summary=true`

### 2. **Multi-Client Management**
Maintain separate branding for different service tiers.

**Example:**
```python
# Premium clients
branding_premium = BrandingConfig(
    name="Premium Tier",
    agency_name="Elite Digital Consulting",
    primary_color="#1e40af",
    footer_text="Confidential — Executive Report"
)

# Standard clients
branding_standard = BrandingConfig(
    name="Standard Tier",
    agency_name="Professional SEO Services",
    primary_color="#059669",
    footer_text="Professional Website Analysis"
)
```

### 3. **Internal Reporting**
Generate unbranded reports for internal analysis.

**Setup:**
- Create brand: "Internal Reports"
- Minimal branding
- Clear, data-focused footer
- Use `include_details=true` for full data

## 🎯 Best Practices

### Logo Design
- **Recommended size:** 400x200px
- **Format:** PNG with transparent background
- **Aspect ratio:** 2:1 (width:height)
- **File size:** < 500KB
- **Colors:** Match primary/secondary colors

### Color Selection
```
High Contrast (Recommended):
  Primary:   #1e40af (Dark Blue)
  Secondary: #3b82f6 (Blue)
  Text:      #1e293b (Dark Gray)

Professional Green:
  Primary:   #059669 (Green)
  Secondary: #10b981 (Light Green)
  Text:      #1e293b (Dark Gray)

Corporate Red:
  Primary:   #dc2626 (Red)
  Secondary: #ef4444 (Light Red)
  Text:      #1e293b (Dark Gray)
```

### Footer Text Examples
```
For Clients:
"Confidential — Prepared exclusively for client use"
"Professional Website Analysis — [Company Name]"
"© 2026 [Agency Name] — All Rights Reserved"

For Internal:
"Internal Use Only — Do Not Distribute"
"Website Performance Analysis — [Date]"
"Automated Audit Report — [Project Name]"
```

## 🔍 Troubleshooting

### Logo Not Appearing
- Check file format (PNG/JPEG only)
- Verify file upload was successful
- Ensure logo_path is correctly stored
- Check file permissions in `/api/static/uploads/logos/`

### Colors Not Matching Preview
- Use exact hex codes (#RRGGBB format)
- Clear browser cache
- Regenerate PDF after branding changes

### PDF Generation Slow
- Expected behavior for large audits (100+ pages)
- Consider excluding content briefs for faster generation
- Use `include_summary=false` for quick exports

### Branding Not Applied
- Verify branding is set as default OR
- Pass `branding_id` parameter explicitly
- Check branding config exists in database

## 📈 Performance Notes

**PDF Generation Time:**
- Small audit (10-20 pages): ~2-3 seconds
- Medium audit (50-100 pages): ~5-8 seconds
- Large audit (200+ pages): ~15-20 seconds
- With content briefs: +5-10 seconds

**File Sizes:**
- Standard PDF: ~50-200 KB
- With AI Summary: ~200-500 KB
- Full Report (with briefs): ~500 KB - 2 MB
- Logo adds: ~50-200 KB

## 🔐 Security Considerations

1. **Logo Upload Validation**
   - File type whitelist: PNG, JPEG only
   - File size limit (handled by web server)
   - Filename sanitization

2. **SQL Injection Protection**
   - All queries use SQLAlchemy ORM
   - Parameterized queries throughout

3. **File Storage**
   - Logos stored outside web root initially
   - Served via FastAPI static files
   - Automatic cleanup on deletion

4. **Authentication**
   - Branding management requires authentication
   - PDF generation inherits audit permissions

## 📚 Additional Resources

- **ReportLab Documentation:** https://www.reportlab.com/docs/reportlab-userguide.pdf
- **Color Picker Tool:** https://htmlcolorcodes.com/
- **Logo Design Tips:** [Internal guidelines]
- **API Reference:** `/docs` endpoint

## 🤝 Support

For issues or questions:
1. Check this guide first
2. Review CHANGELOG_v2_1_0_WHITE_LABEL_PDF.md
3. Check `/docs` API documentation
4. Contact development team

---

**Version:** 2.1.0  
**Last Updated:** February 20, 2026  
**Author:** Development Team
