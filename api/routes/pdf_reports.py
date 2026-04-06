"""
PDF White-Label Reports Router

Generates professional, branded PDF reports with customizable logos and colors.
Uses ReportLab for server-side PDF generation.
"""

import os
import io
import shutil
from datetime import datetime
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from fastapi.responses import StreamingResponse
from api.utils.errors import raise_not_found, raise_bad_request
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm, cm, inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, Image as RLImage, KeepTogether, HRFlowable
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.colors import HexColor, white, black
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT, TA_JUSTIFY
from reportlab.graphics.shapes import Drawing, Circle, Wedge, String as GraphicsString, Rect
from reportlab.graphics import renderPDF

from api.models.database import (
    get_db, Audit, AuditResult, BrandingConfig, AuditSummary, ContentBrief
)

router = APIRouter(prefix="/api/reports", tags=["PDF Reports"])

# Upload directory for logos
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "uploads", "logos")
os.makedirs(UPLOAD_DIR, exist_ok=True)


# ==================== BRANDING ENDPOINTS ====================

@router.post("/branding")
async def create_branding(
    name: str = Form(...),
    agency_name: str = Form("Website Audit Report"),
    tagline: Optional[str] = Form(None),
    primary_color: str = Form("#1e40af"),
    secondary_color: str = Form("#3b82f6"),
    text_color: str = Form("#1e293b"),
    footer_text: str = Form("Confidential — Prepared exclusively for client use"),
    contact_email: Optional[str] = Form(None),
    contact_website: Optional[str] = Form(None),
    is_default: bool = Form(False),
    logo: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db)
):
    """Create or update branding configuration."""
    
    # Validate logo file type
    logo_path = None
    if logo:
        if not logo.content_type in ["image/png", "image/jpeg", "image/jpg"]:
            raise_bad_request("Logo must be PNG or JPEG")
        
        # Save logo
        ext = logo.filename.split(".")[-1]
        filename = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{name.replace(' ', '_')}.{ext}"
        logo_path = os.path.join(UPLOAD_DIR, filename)
        
        with open(logo_path, "wb") as f:
            content = await logo.read()
            f.write(content)
        
        # Store relative path
        logo_path = f"/static/uploads/logos/{filename}"
    
    # If setting as default, unset all other defaults
    if is_default:
        await db.execute(
            update(BrandingConfig).values(is_default=0)
        )
    
    # Create new branding
    branding = BrandingConfig(
        name=name,
        agency_name=agency_name,
        tagline=tagline,
        primary_color=primary_color,
        secondary_color=secondary_color,
        text_color=text_color,
        footer_text=footer_text,
        contact_email=contact_email,
        contact_website=contact_website,
        is_default=1 if is_default else 0,
        logo_path=logo_path
    )
    
    db.add(branding)
    await db.commit()
    await db.refresh(branding)
    
    return branding.to_dict()


@router.get("/branding")
async def list_brandings(db: AsyncSession = Depends(get_db)):
    """List all branding configurations."""
    result = await db.execute(select(BrandingConfig).order_by(BrandingConfig.is_default.desc()))
    brandings = result.scalars().all()
    return [b.to_dict() for b in brandings]


@router.get("/branding/{branding_id}")
async def get_branding(branding_id: int, db: AsyncSession = Depends(get_db)):
    """Get specific branding configuration."""
    result = await db.execute(
        select(BrandingConfig).where(BrandingConfig.id == branding_id)
    )
    branding = result.scalar_one_or_none()
    
    if not branding:
        raise_not_found("Branding")
    
    return branding.to_dict()


@router.delete("/branding/{branding_id}")
async def delete_branding(branding_id: int, db: AsyncSession = Depends(get_db)):
    """Delete branding configuration."""
    result = await db.execute(
        select(BrandingConfig).where(BrandingConfig.id == branding_id)
    )
    branding = result.scalar_one_or_none()
    
    if not branding:
        raise_not_found("Branding")
    
    # Delete logo file if exists
    if branding.logo_path:
        full_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), 
            "static", 
            branding.logo_path.lstrip("/static/")
        )
        if os.path.exists(full_path):
            os.remove(full_path)
    
    await db.execute(delete(BrandingConfig).where(BrandingConfig.id == branding_id))
    await db.commit()
    
    return {"success": True}


@router.patch("/branding/{branding_id}/set-default")
async def set_default_branding(branding_id: int, db: AsyncSession = Depends(get_db)):
    """Set a branding as default."""
    # Unset all defaults
    await db.execute(update(BrandingConfig).values(is_default=0))
    
    # Set new default
    result = await db.execute(
        update(BrandingConfig)
        .where(BrandingConfig.id == branding_id)
        .values(is_default=1)
    )
    
    await db.commit()
    
    if result.rowcount == 0:
        raise_not_found("Branding")
    
    return {"success": True}


# ==================== PDF GENERATION ====================

def draw_score_gauge(score: float, size: int = 150) -> Drawing:
    """
    Draw a circular score gauge using ReportLab graphics.
    
    Args:
        score: Score value (0-100)
        size: Size of the drawing in points
    
    Returns:
        ReportLab Drawing object
    """
    d = Drawing(size, size)
    
    # Center coordinates
    cx, cy = size / 2, size / 2
    radius = size / 3
    
    # Background circle (light gray)
    bg_circle = Circle(cx, cy, radius, strokeColor=HexColor("#e5e7eb"), 
                       strokeWidth=12, fillColor=None)
    d.add(bg_circle)
    
    # Colored arc based on score
    if score >= 80:
        color = HexColor("#16a34a")  # green
    elif score >= 60:
        color = HexColor("#2563eb")  # blue
    elif score >= 40:
        color = HexColor("#d97706")  # orange
    else:
        color = HexColor("#dc2626")  # red
    
    # Draw arc (wedge from 90 degrees, counterclockwise)
    angle = (score / 100) * 360
    if angle > 0:
        wedge = Wedge(cx, cy, radius, 90, 90 - angle, 
                      strokeColor=color, strokeWidth=12, fillColor=None)
        d.add(wedge)
    
    # Score text in center
    score_text = GraphicsString(cx, cy, f"{score:.0f}", 
                                fontSize=size / 3, 
                                fillColor=HexColor("#1e293b"),
                                textAnchor="middle")
    d.add(score_text)
    
    return d


def create_score_distribution_chart(score_counts: Dict[str, int], width: int = 400, height: int = 150) -> Drawing:
    """
    Create a horizontal bar chart for score distribution.
    
    Args:
        score_counts: Dict with keys 'excellent', 'good', 'needs_work', 'poor'
        width: Chart width in points
        height: Chart height in points
    
    Returns:
        ReportLab Drawing object
    """
    d = Drawing(width, height)
    
    categories = [
        ("Excellent (80-100)", score_counts.get('excellent', 0), HexColor("#16a34a")),
        ("Good (60-79)", score_counts.get('good', 0), HexColor("#2563eb")),
        ("Needs Work (40-59)", score_counts.get('needs_work', 0), HexColor("#d97706")),
        ("Poor (0-39)", score_counts.get('poor', 0), HexColor("#dc2626")),
    ]
    
    total = sum(c[1] for c in categories)
    if total == 0:
        return d
    
    bar_height = 25
    y_start = height - 40
    max_bar_width = width - 150
    
    for i, (label, count, color) in enumerate(categories):
        y = y_start - (i * 35)
        
        # Label
        label_text = GraphicsString(10, y + bar_height / 3, label, 
                                    fontSize=10, fillColor=black, textAnchor="start")
        d.add(label_text)
        
        # Bar
        bar_width = (count / total) * max_bar_width if total > 0 else 0
        bar = Rect(120, y, bar_width, bar_height, 
                  fillColor=color, strokeColor=None)
        d.add(bar)
        
        # Count text
        count_text = GraphicsString(125 + bar_width, y + bar_height / 3, 
                                   f"{count} ({count/total*100:.0f}%)",
                                   fontSize=10, fillColor=black, textAnchor="start")
        d.add(count_text)
    
    return d


async def _generate_audit_pdf(
    audit: Audit,
    results: List[AuditResult],
    branding: BrandingConfig,
    include_summary: bool = True,
    include_briefs: bool = False,
    include_details: bool = False,
    ai_summary: Optional[AuditSummary] = None,
    briefs: Optional[List[ContentBrief]] = None,
) -> io.BytesIO:
    """
    Generate PDF report for an audit.
    
    Args:
        audit: Audit object
        results: List of AuditResult objects
        branding: BrandingConfig object
        include_summary: Include AI executive summary
        include_briefs: Include content briefs
        include_details: Include full JSON details per page
        ai_summary: Optional AISummary object
        briefs: Optional list of ContentBrief objects
    
    Returns:
        BytesIO buffer containing PDF
    """
    buffer = io.BytesIO()
    
    # Create PDF document
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=2*cm,
        leftMargin=2*cm,
        topMargin=2.5*cm,
        bottomMargin=2.5*cm,
        title=f"{audit.website} - Audit Report"
    )
    
    # Styles
    styles = getSampleStyleSheet()
    
    # Custom styles with branding colors
    primary_color = HexColor(branding.primary_color)
    secondary_color = HexColor(branding.secondary_color)
    text_color = HexColor(branding.text_color)
    
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=28,
        textColor=primary_color,
        spaceAfter=12,
        alignment=TA_CENTER,
        fontName='Helvetica-Bold'
    )
    
    heading1_style = ParagraphStyle(
        'CustomHeading1',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=primary_color,
        spaceAfter=12,
        spaceBefore=16,
        fontName='Helvetica-Bold'
    )
    
    heading2_style = ParagraphStyle(
        'CustomHeading2',
        parent=styles['Heading2'],
        fontSize=14,
        textColor=secondary_color,
        spaceAfter=10,
        spaceBefore=12,
        fontName='Helvetica-Bold'
    )
    
    body_style = ParagraphStyle(
        'CustomBody',
        parent=styles['Normal'],
        fontSize=10,
        textColor=text_color,
        spaceAfter=6,
        alignment=TA_JUSTIFY
    )
    
    center_style = ParagraphStyle(
        'Center',
        parent=body_style,
        alignment=TA_CENTER
    )
    
    # Story (content elements)
    story = []
    
    # ===== PAGE 1: COVER =====
    
    # Logo (if exists)
    if branding.logo_path:
        logo_full_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "static",
            branding.logo_path.lstrip("/static/")
        )
        if os.path.exists(logo_full_path):
            try:
                logo = RLImage(logo_full_path, width=2*inch, height=1*inch, kind='proportional')
                logo.hAlign = 'CENTER'
                story.append(logo)
                story.append(Spacer(1, 0.5*cm))
            except Exception as _ex:
                print(f"[pdf_reports] Warning: failed to load logo image '{logo_full_path}': {_ex}")
    
    # Agency name
    story.append(Paragraph(branding.agency_name, title_style))
    
    # Tagline
    if branding.tagline:
        tagline_style = ParagraphStyle(
            'Tagline',
            parent=body_style,
            fontSize=12,
            alignment=TA_CENTER,
            textColor=secondary_color,
            fontName='Helvetica-Oblique'
        )
        story.append(Paragraph(branding.tagline, tagline_style))
    
    story.append(Spacer(1, 1*cm))
    
    # Separator line
    story.append(HRFlowable(width="100%", thickness=1, color=HexColor("#e5e7eb"), spaceAfter=0))
    story.append(Spacer(1, 1*cm))
    
    # Report title
    story.append(Paragraph("WEBSITE AUDIT REPORT", title_style))
    story.append(Spacer(1, 0.5*cm))
    
    # Audit info
    audit_info = [
        f"<b>Audit Type:</b> {audit.audit_type}",
        f"<b>Website:</b> {audit.website}",
        f"<b>Generated:</b> {datetime.utcnow().strftime('%B %d, %Y')}",
        f"<b>Pages Analyzed:</b> {audit.pages_analyzed}",
    ]
    
    for info in audit_info:
        story.append(Paragraph(info, center_style))
        story.append(Spacer(1, 0.3*cm))
    
    story.append(Spacer(1, 1.5*cm))
    
    # Prepared by
    prepared_style = ParagraphStyle(
        'Prepared',
        parent=body_style,
        fontSize=11,
        alignment=TA_CENTER,
        textColor=text_color
    )
    story.append(Paragraph(f"<b>Prepared by:</b> {branding.agency_name}", prepared_style))
    
    if branding.contact_email:
        story.append(Paragraph(f"<b>Contact:</b> {branding.contact_email}", prepared_style))
    
    if branding.contact_website:
        story.append(Paragraph(f"<b>Website:</b> {branding.contact_website}", prepared_style))
    
    story.append(PageBreak())
    
    # ===== PAGE 2: EXECUTIVE SUMMARY =====
    
    story.append(Paragraph("Executive Summary", heading1_style))
    story.append(Spacer(1, 0.5*cm))
    
    # Calculate metrics
    avg_score = audit.average_score or 0
    score_counts = {'excellent': 0, 'good': 0, 'needs_work': 0, 'poor': 0}
    
    for result in results:
        score = result.score or 0
        if score >= 80:
            score_counts['excellent'] += 1
        elif score >= 60:
            score_counts['good'] += 1
        elif score >= 40:
            score_counts['needs_work'] += 1
        else:
            score_counts['poor'] += 1
    
    # Score gauge
    gauge = draw_score_gauge(avg_score, size=120)
    story.append(KeepTogether([
        Paragraph("<b>Overall Score</b>", center_style),
        Spacer(1, 0.3*cm),
        gauge
    ]))
    story.append(Spacer(1, 1*cm))
    
    # Metrics boxes (as table)
    metrics_data = [
        ['Pages Analyzed', 'Average Score', 'Excellent', 'Poor'],
        [
            str(audit.pages_analyzed),
            f"{avg_score:.1f}",
            str(score_counts['excellent']),
            str(score_counts['poor'])
        ]
    ]
    
    metrics_table = Table(metrics_data, colWidths=[4*cm, 4*cm, 4*cm, 4*cm])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), secondary_color),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('FONTSIZE', (0, 1), (-1, 1), 14),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('TOPPADDING', (0, 1), (-1, 1), 12),
        ('GRID', (0, 0), (-1, -1), 1, HexColor("#e5e7eb")),
    ]))
    story.append(metrics_table)
    story.append(Spacer(1, 1*cm))
    
    # Score distribution chart
    story.append(Paragraph("<b>Score Distribution</b>", heading2_style))
    chart = create_score_distribution_chart(score_counts)
    story.append(chart)
    story.append(Spacer(1, 1*cm))
    
    # AI Summary (if available and requested)
    if include_summary and ai_summary and ai_summary.executive_summary:
        story.append(PageBreak())
        story.append(Paragraph("AI-Generated Insights", heading1_style))
        story.append(Spacer(1, 0.5*cm))
        
        # Executive summary
        story.append(Paragraph("<b>Executive Summary</b>", heading2_style))
        summary_paras = ai_summary.executive_summary.split('\n')
        for para in summary_paras:
            if para.strip():
                story.append(Paragraph(para.strip(), body_style))
        
        story.append(Spacer(1, 0.5*cm))
    
    # Key Findings table (if available)
    if include_summary and ai_summary and ai_summary.key_findings:
        import json
        try:
            findings = json.loads(ai_summary.key_findings) if isinstance(ai_summary.key_findings, str) else ai_summary.key_findings
            
            if findings and isinstance(findings, list):
                story.append(Paragraph("<b>Key Findings</b>", heading2_style))
                
                findings_data = [['Finding', 'Impact', 'Category']]
                for finding in findings[:10]:  # Limit to 10
                    findings_data.append([
                        finding.get('finding', '')[:80],
                        finding.get('impact', '')[:60],
                        finding.get('category', '')[:30]
                    ])
                
                findings_table = Table(findings_data, colWidths=[7*cm, 5*cm, 4*cm])
                findings_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, 0), primary_color),
                    ('TEXTCOLOR', (0, 0), (-1, 0), white),
                    ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                    ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                    ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                    ('FONTSIZE', (0, 0), (-1, 0), 10),
                    ('FONTSIZE', (0, 1), (-1, -1), 9),
                    ('GRID', (0, 0), (-1, -1), 0.5, HexColor("#e5e7eb")),
                    ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor("#f9fafb")]),
                ]))
                story.append(findings_table)
                story.append(Spacer(1, 1*cm))
        except Exception as _ex:
            print(f"[pdf_reports] Warning: failed to render key findings table: {_ex}")

    # Action Plan (if available)
    if include_summary and ai_summary and ai_summary.action_plan:
        try:
            import json
            action_plan = json.loads(ai_summary.action_plan) if isinstance(ai_summary.action_plan, str) else ai_summary.action_plan

            if action_plan and isinstance(action_plan, dict):
                story.append(Paragraph("<b>Action Plan</b>", heading2_style))

                for week_key in sorted(action_plan.keys())[:4]:  # First 4 weeks
                    week_actions = action_plan[week_key]
                    if isinstance(week_actions, list) and week_actions:
                        story.append(Paragraph(f"<b>{week_key}</b>", body_style))

                        for action in week_actions:
                            action_text = f"• {action.get('action', '')}"
                            if action.get('pages_affected'):
                                action_text += f" ({action['pages_affected']} pages)"
                            story.append(Paragraph(action_text, body_style))

                        story.append(Spacer(1, 0.3*cm))
        except Exception as _ex:
            print(f"[pdf_reports] Warning: failed to render action plan: {_ex}")
    
    # ===== RESULTS TABLE =====

    is_single_page = audit.audit_type.startswith('SINGLE_')

    story.append(PageBreak())
    story.append(Paragraph("Detailed Results", heading1_style))
    if is_single_page:
        story.append(Paragraph(f"<b>URL:</b> {audit.website}", body_style))
    story.append(Spacer(1, 0.5*cm))

    first_col_header = 'Audit Type' if is_single_page else 'Page URL'
    results_data = [[first_col_header, 'Score', 'Classification']]

    def _build_table_style(data, primary_color):
        tbl = Table(data, colWidths=[10*cm, 2*cm, 4*cm])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), primary_color),
            ('TEXTCOLOR', (0, 0), (-1, 0), white),
            ('ALIGN', (1, 0), (1, -1), 'CENTER'),
            ('ALIGN', (2, 0), (2, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -1), 0.5, HexColor("#e5e7eb")),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [white, HexColor("#f9fafb")]),
        ]))
        for i in range(1, len(data)):
            try:
                score_val = float(data[i][1])
            except (ValueError, TypeError):
                continue
            if score_val >= 80:
                color = HexColor("#16a34a")
            elif score_val >= 60:
                color = HexColor("#2563eb")
            elif score_val >= 40:
                color = HexColor("#d97706")
            else:
                color = HexColor("#dc2626")
            tbl.setStyle(TableStyle([
                ('TEXTCOLOR', (1, i), (1, i), color),
                ('FONTNAME', (1, i), (1, i), 'Helvetica-Bold'),
            ]))
        return tbl

    for result in results:
        score = result.score or 0

        # Classification
        if score >= 80:
            classification = "Excellent"
        elif score >= 60:
            classification = "Good"
        elif score >= 40:
            classification = "Needs Work"
        else:
            classification = "Poor"

        # First column: audit type label for single-page, URL for multi-page
        if is_single_page:
            label = result.filename.replace('.json', '').replace('_', ' ').title()
        else:
            label = result.page_url
            if len(label) > 60:
                label = label[:57] + "..."

        results_data.append([label, f"{score:.0f}", classification])

        # Page break every 30 rows
        if len(results_data) > 30:
            story.append(_build_table_style(results_data, primary_color))
            story.append(PageBreak())
            results_data = [[first_col_header, 'Score', 'Classification']]
    
    # Add remaining results
    if len(results_data) > 1:
        story.append(_build_table_style(results_data, primary_color))
    
    # ===== CONTENT BRIEFS (OPTIONAL) =====
    
    if include_briefs and briefs:
        story.append(PageBreak())
        story.append(Paragraph("Content Optimization Briefs", heading1_style))
        story.append(Spacer(1, 0.5*cm))
        
        for brief in briefs[:20]:  # Limit to 20
            story.append(Paragraph(f"<b>{brief.page_url}</b>", heading2_style))
            story.append(Paragraph(f"Score: {brief.current_score:.0f} | Priority: {brief.priority}", body_style))
            story.append(Spacer(1, 0.3*cm))
            
            if brief.executive_summary:
                story.append(Paragraph("<b>Summary:</b>", body_style))
                story.append(Paragraph(brief.executive_summary[:500], body_style))
                story.append(Spacer(1, 0.3*cm))
            
            story.append(Spacer(1, 0.5*cm))
    
    # Build PDF with custom footer
    def add_page_number(canvas, doc):
        """Add footer to each page."""
        page_num = canvas.getPageNumber()
        text = f"{branding.agency_name} — {branding.footer_text}"
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(HexColor("#6b7280"))
        canvas.drawString(2*cm, 1.5*cm, text[:80])
        canvas.drawRightString(A4[0] - 2*cm, 1.5*cm, f"Page {page_num}")
        canvas.restoreState()
    
    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
    
    buffer.seek(0)
    return buffer


@router.get("/{audit_id}/pdf")
async def generate_pdf_report(
    audit_id: str,
    branding_id: Optional[int] = Query(None, description="Branding config ID (uses default if not set)"),
    include_summary: bool = Query(True, description="Include AI summary if available"),
    include_briefs: bool = Query(False, description="Include content briefs if available"),
    include_details: bool = Query(False, description="Include full JSON details per page"),
    db: AsyncSession = Depends(get_db)
):
    """Generate and download PDF report for an audit."""
    
    # Get audit
    result = await db.execute(
        select(Audit).where(Audit.id == audit_id)
    )
    audit = result.scalar_one_or_none()
    
    if not audit:
        raise_not_found("Audit")
    
    if audit.status != "completed":
        raise_bad_request("Audit is not completed yet")
    
    # Get results
    result = await db.execute(
        select(AuditResult).where(AuditResult.audit_id == audit_id).order_by(AuditResult.page_url)
    )
    results = result.scalars().all()
    
    if not results:
        raise HTTPException(404, "No results found for this audit")
    
    # Get branding config
    if branding_id:
        result = await db.execute(
            select(BrandingConfig).where(BrandingConfig.id == branding_id)
        )
        branding = result.scalar_one_or_none()
    else:
        # Get default branding
        result = await db.execute(
            select(BrandingConfig).where(BrandingConfig.is_default == 1)
        )
        branding = result.scalar_one_or_none()
    
    # If no branding found, create default
    if not branding:
        branding = BrandingConfig(
            name="Default",
            is_default=1,
            agency_name="Website Audit Report",
            primary_color="#1e40af",
            secondary_color="#3b82f6",
            text_color="#1e293b",
            footer_text="Confidential — Prepared exclusively for client use"
        )
        db.add(branding)
        await db.commit()
        await db.refresh(branding)
    
    # Get AI summary (if requested)
    ai_summary = None
    if include_summary:
        result = await db.execute(
            select(AuditSummary).where(AuditSummary.audit_id == audit_id)
        )
        ai_summary = result.scalar_one_or_none()
    
    # Get content briefs (if requested)
    briefs = None
    if include_briefs:
        result = await db.execute(
            select(ContentBrief).where(ContentBrief.audit_id == audit_id).order_by(ContentBrief.priority.desc())
        )
        briefs = result.scalars().all()
    
    # Generate PDF
    pdf_buffer = await _generate_audit_pdf(
        audit=audit,
        results=results,
        branding=branding,
        include_summary=include_summary,
        include_briefs=include_briefs,
        include_details=include_details,
        ai_summary=ai_summary,
        briefs=briefs
    )
    
    # Generate filename
    website_clean = audit.website.replace("https://", "").replace("http://", "").replace("/", "_")
    date_str = datetime.utcnow().strftime("%Y%m%d")
    filename = f"{website_clean}_{audit.audit_type}_{date_str}.pdf"
    
    # Return as streaming response
    return StreamingResponse(
        pdf_buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={filename}"
        }
    )
