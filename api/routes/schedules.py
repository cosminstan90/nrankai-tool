"""
Scheduled Audits Router - Recurring audit execution with cron expressions.

Provides endpoints for creating, managing, and tracking scheduled audits
with automatic execution based on cron schedules.
"""

import uuid
import json
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from api.utils.task_runner import create_tracked_task

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from api.utils.errors import raise_not_found
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel, Field, field_validator

from api.models.database import (
    Audit, ScheduledAudit, AsyncSessionLocal, get_db
)
from api.workers.audit_worker import start_audit_pipeline


router = APIRouter(prefix="/api/schedules", tags=["schedules"])


# ============================================================================
# PYDANTIC MODELS
# ============================================================================

class ScheduleCreateRequest(BaseModel):
    """Request model for creating a scheduled audit."""
    name: str = Field(..., min_length=1, max_length=255)
    website: str = Field(..., min_length=1)
    sitemap_url: Optional[str] = None
    audit_type: str
    provider: str
    model: Optional[str] = None
    language: str = "English"
    use_perplexity: bool = False
    concurrency: int = Field(default=5, ge=1, le=20)
    schedule_cron: str
    summary_provider: Optional[str] = None
    summary_model: Optional[str] = None
    
    @field_validator('schedule_cron')
    @classmethod
    def validate_cron(cls, v):
        """Validate cron expression has 5 fields."""
        parts = v.strip().split()
        if len(parts) != 5:
            raise ValueError('Cron expression must have 5 fields: minute hour day month weekday')
        return v


class ScheduleUpdateRequest(BaseModel):
    """Request model for updating a scheduled audit."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    schedule_cron: Optional[str] = None
    is_active: Optional[bool] = None
    language: Optional[str] = None
    concurrency: Optional[int] = Field(None, ge=1, le=20)
    
    @field_validator('schedule_cron')
    @classmethod
    def validate_cron(cls, v):
        """Validate cron expression has 5 fields."""
        if v is not None:
            parts = v.strip().split()
            if len(parts) != 5:
                raise ValueError('Cron expression must have 5 fields: minute hour day month weekday')
        return v


# ============================================================================
# CRON UTILITIES
# ============================================================================

def _parse_cron_field(field: str, min_val: int, max_val: int) -> List[int]:
    """
    Parse a single cron field into list of matching values.
    
    Supports:
    - * (all values)
    - exact match (5)
    - ranges (1-5)
    - lists (1,3,5)
    - steps (*/5, 1-10/2)
    """
    values = []
    
    # Handle wildcard
    if field == '*':
        return list(range(min_val, max_val + 1))
    
    # Handle step values (*/5 or 1-10/2)
    if '/' in field:
        range_part, step = field.split('/')
        step = int(step)
        
        if range_part == '*':
            start, end = min_val, max_val
        elif '-' in range_part:
            start, end = map(int, range_part.split('-'))
        else:
            start = end = int(range_part)
        
        values = list(range(start, end + 1, step))
        return values
    
    # Handle lists (1,3,5)
    if ',' in field:
        for part in field.split(','):
            if '-' in part:
                start, end = map(int, part.split('-'))
                values.extend(range(start, end + 1))
            else:
                values.append(int(part))
        return sorted(set(values))
    
    # Handle ranges (1-5)
    if '-' in field:
        start, end = map(int, field.split('-'))
        return list(range(start, end + 1))
    
    # Exact match
    return [int(field)]


def _cron_matches(cron_expr: str, dt: datetime) -> bool:
    """
    Check if a datetime matches a cron expression.
    
    Cron format: minute hour day month weekday
    - minute: 0-59
    - hour: 0-23
    - day: 1-31
    - month: 1-12
    - weekday: 0-6 (0=Sunday)
    """
    try:
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            return False
        
        minute_field, hour_field, day_field, month_field, weekday_field = parts
        
        # Parse each field
        minutes = _parse_cron_field(minute_field, 0, 59)
        hours = _parse_cron_field(hour_field, 0, 23)
        days = _parse_cron_field(day_field, 1, 31)
        months = _parse_cron_field(month_field, 1, 12)
        weekdays = _parse_cron_field(weekday_field, 0, 6)
        
        # Convert Python weekday (Monday=0) to cron weekday (Sunday=0)
        current_weekday = dt.isoweekday() % 7
        
        # Check all fields match
        return (
            dt.minute in minutes and
            dt.hour in hours and
            dt.day in days and
            dt.month in months and
            current_weekday in weekdays
        )
    except Exception as e:
        print(f"Error parsing cron expression '{cron_expr}': {e}")
        return False


def _cron_to_human(cron: str) -> str:
    """
    Convert cron expression to human-readable text.
    
    Examples:
    - "0 9 * * 1" → "Every Monday at 9:00"
    - "0 6 * * *" → "Daily at 6:00"
    - "0 9 1 * *" → "Monthly on 1st at 9:00"
    - "0 9 1,15 * *" → "On 1st and 15th at 9:00"
    """
    try:
        parts = cron.strip().split()
        if len(parts) != 5:
            return cron
        
        minute, hour, day, month, weekday = parts
        
        # Build time string
        hour_int = int(hour) if hour != '*' else 0
        minute_int = int(minute) if minute != '*' else 0
        time_str = f"{hour_int:02d}:{minute_int:02d}"
        
        # Daily pattern
        if day == '*' and month == '*' and weekday == '*':
            return f"Daily at {time_str}"
        
        # Weekly pattern
        if day == '*' and month == '*' and weekday != '*':
            weekday_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 
                           'Thursday', 'Friday', 'Saturday']
            if ',' in weekday:
                days = [weekday_names[int(d)] for d in weekday.split(',')]
                return f"Every {', '.join(days)} at {time_str}"
            else:
                return f"Every {weekday_names[int(weekday)]} at {time_str}"
        
        # Monthly pattern
        if day != '*' and month == '*' and weekday == '*':
            if ',' in day:
                return f"On {day} of month at {time_str}"
            else:
                ordinal = f"{day}{'st' if day == '1' else 'nd' if day == '2' else 'rd' if day == '3' else 'th'}"
                return f"Monthly on {ordinal} at {time_str}"
        
        # Quarterly or specific months
        if month != '*':
            month_names = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
            if month == '1,4,7,10':
                return f"Quarterly (Jan/Apr/Jul/Oct) on {day} at {time_str}"
            elif ',' in month:
                months = [month_names[int(m)-1] for m in month.split(',')]
                return f"In {'/'.join(months)} on {day} at {time_str}"
        
        return cron
    except Exception:
        return cron


# ============================================================================
# SCHEDULER ENGINE
# ============================================================================

async def check_and_run_schedules():
    """
    Main scheduler loop - checks all active schedules and runs matching ones.
    
    Called every minute by the background scheduler task.
    Prevents double-runs by checking last_run_at timestamp.
    """
    async with AsyncSessionLocal() as db:
        try:
            # Get all active schedules
            result = await db.execute(
                select(ScheduledAudit).where(ScheduledAudit.is_active == 1)
            )
            schedules = result.scalars().all()
            
            now = datetime.now(timezone.utc)
            
            for schedule in schedules:
                # Check if cron matches current time
                if not _cron_matches(schedule.schedule_cron, now):
                    continue
                
                # Prevent double-run (must be at least 30 minutes since last run)
                if schedule.last_run_at:
                    time_since_last = now - schedule.last_run_at
                    if time_since_last < timedelta(minutes=30):
                        continue
                
                # Create new audit
                audit_id = str(uuid.uuid4())
                audit = Audit(
                    id=audit_id,
                    website=schedule.website,
                    sitemap_url=schedule.sitemap_url,
                    audit_type=schedule.audit_type,
                    provider=schedule.provider,
                    model=schedule.model or "default",
                    status="pending",
                    current_step=f"scheduled_{schedule.id}"
                )
                db.add(audit)
                
                # Update schedule metadata
                schedule.last_run_at = now
                schedule.last_audit_id = audit_id
                schedule.run_count += 1
                
                await db.commit()
                
                print(f"🕐 Scheduler: Running '{schedule.name}' (audit {audit_id})")
                
                # Start audit pipeline in background
                create_tracked_task(
                    start_audit_pipeline(
                        audit_id=audit_id,
                        website=schedule.website,
                        sitemap_url=schedule.sitemap_url,
                        audit_type=schedule.audit_type,
                        provider=schedule.provider,
                        model=schedule.model or "default",
                        max_chars=30000,
                        use_direct_mode=True,
                        concurrency=schedule.concurrency,
                        use_perplexity=bool(schedule.use_perplexity),
                        language=schedule.language
                    ),
                    name=f"schedule-audit-pipeline-{audit_id}",
                    timeout=14400,
                )

                # If auto-summary is enabled, start polling task
                if schedule.summary_provider and schedule.summary_model:
                    create_tracked_task(
                        _poll_and_generate_summary(
                            audit_id,
                            schedule.summary_provider,
                            schedule.summary_model,
                            schedule.language
                        ),
                        name=f"schedule-summary-{audit_id}",
                        timeout=14400,
                    )
                
        except Exception as e:
            print(f"❌ Scheduler error: {e}")


async def _poll_and_generate_summary(
    audit_id: str,
    provider: str,
    model: str,
    language: str
):
    """
    Poll audit completion and auto-generate summary.
    
    Checks every 60 seconds for up to 2 hours.
    """
    from api.routes.summary import call_llm_for_summary
    
    max_attempts = 120  # 2 hours
    for attempt in range(max_attempts):
        await asyncio.sleep(60)  # Wait 1 minute
        
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(Audit).where(Audit.id == audit_id))
            audit = result.scalar_one_or_none()
            
            if not audit:
                return
            
            if audit.status == "completed":
                print(f"✅ Auto-generating summary for scheduled audit {audit_id}")
                try:
                    # Import here to avoid circular dependency
                    from api.routes.summary import generate_summary_task
                    await generate_summary_task(audit_id, language, provider, model)
                except Exception as e:
                    print(f"❌ Failed to auto-generate summary: {e}")
                return
            
            if audit.status == "failed":
                return


# ============================================================================
# API ENDPOINTS
# ============================================================================

@router.get("/presets")
async def get_schedule_presets():
    """Get common cron expression presets."""
    return {
        "weekly_monday": {
            "cron": "0 9 * * 1",
            "label": "Every Monday at 9:00 AM"
        },
        "weekly_friday": {
            "cron": "0 9 * * 5",
            "label": "Every Friday at 9:00 AM"
        },
        "biweekly": {
            "cron": "0 9 1,15 * *",
            "label": "1st & 15th of month at 9:00 AM"
        },
        "monthly": {
            "cron": "0 9 1 * *",
            "label": "Monthly (1st) at 9:00 AM"
        },
        "quarterly": {
            "cron": "0 9 1 1,4,7,10 *",
            "label": "Quarterly (Jan/Apr/Jul/Oct 1st) at 9:00 AM"
        },
        "daily": {
            "cron": "0 6 * * *",
            "label": "Daily at 6:00 AM"
        },
        "daily_evening": {
            "cron": "0 18 * * *",
            "label": "Daily at 6:00 PM"
        }
    }


@router.post("")
async def create_schedule(
    request: ScheduleCreateRequest,
    db: AsyncSession = Depends(get_db)
):
    """Create a new scheduled audit."""
    schedule_id = str(uuid.uuid4())
    
    schedule = ScheduledAudit(
        id=schedule_id,
        name=request.name,
        website=request.website,
        sitemap_url=request.sitemap_url,
        audit_type=request.audit_type,
        provider=request.provider,
        model=request.model,
        language=request.language,
        use_perplexity=1 if request.use_perplexity else 0,
        concurrency=request.concurrency,
        schedule_cron=request.schedule_cron,
        is_active=1,
        summary_provider=request.summary_provider,
        summary_model=request.summary_model
    )
    
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    
    result = schedule.to_dict()
    result['schedule_human'] = _cron_to_human(schedule.schedule_cron)
    
    return result


@router.get("")
async def list_schedules(db: AsyncSession = Depends(get_db)):
    """List all scheduled audits with summary info."""
    result = await db.execute(select(ScheduledAudit).order_by(ScheduledAudit.created_at.desc()))
    schedules = result.scalars().all()
    
    output = []
    for schedule in schedules:
        # Count history
        history_result = await db.execute(
            select(func.count(Audit.id)).where(
                and_(
                    Audit.website == schedule.website,
                    Audit.audit_type == schedule.audit_type,
                    Audit.status == "completed"
                )
            )
        )
        history_count = history_result.scalar() or 0
        
        data = schedule.to_dict()
        data['schedule_human'] = _cron_to_human(schedule.schedule_cron)
        data['history_count'] = history_count
        
        output.append(data)
    
    return output


@router.get("/{schedule_id}")
async def get_schedule_detail(
    schedule_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get detailed schedule info with audit history and trend analysis."""
    result = await db.execute(
        select(ScheduledAudit).where(ScheduledAudit.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    
    if not schedule:
        raise_not_found("Schedule")
    
    # Get audit history (completed audits only)
    history_result = await db.execute(
        select(Audit).where(
            and_(
                Audit.website == schedule.website,
                Audit.audit_type == schedule.audit_type,
                Audit.status == "completed"
            )
        ).order_by(Audit.completed_at.asc())
    )
    history_audits = history_result.scalars().all()
    
    # Build history array
    history = []
    for audit in history_audits:
        history.append({
            "id": audit.id,
            "average_score": audit.average_score,
            "pages_analyzed": audit.pages_analyzed,
            "completed_at": audit.completed_at.isoformat() if audit.completed_at else None
        })
    
    # Calculate trend
    trend = None
    if len(history) >= 2:
        first_score = history[0]['average_score'] or 0
        latest_score = history[-1]['average_score'] or 0
        delta = latest_score - first_score
        
        all_scores = [h['average_score'] for h in history if h['average_score'] is not None]
        best_score = max(all_scores) if all_scores else 0
        worst_score = min(all_scores) if all_scores else 0
        
        if delta > 0:
            direction = "improving"
        elif delta < 0:
            direction = "declining"
        else:
            direction = "stable"
        
        trend = {
            "direction": direction,
            "first_score": first_score,
            "latest_score": latest_score,
            "delta": round(delta, 2),
            "best": best_score,
            "worst": worst_score
        }
    
    data = schedule.to_dict()
    data['schedule_human'] = _cron_to_human(schedule.schedule_cron)
    data['history'] = history
    data['trend'] = trend
    
    return data


@router.get("/{schedule_id}/history")
async def get_schedule_history_chart(
    schedule_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Get chart-ready history data for visualization."""
    result = await db.execute(
        select(ScheduledAudit).where(ScheduledAudit.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    
    if not schedule:
        raise_not_found("Schedule")
    
    # Get completed audits
    history_result = await db.execute(
        select(Audit).where(
            and_(
                Audit.website == schedule.website,
                Audit.audit_type == schedule.audit_type,
                Audit.status == "completed"
            )
        ).order_by(Audit.completed_at.asc())
    )
    audits = history_result.scalars().all()
    
    # Format for Chart.js
    labels = []
    scores = []
    audit_ids = []
    
    for audit in audits:
        if audit.completed_at and audit.average_score is not None:
            # Format date as "Jan 15"
            date_str = audit.completed_at.strftime("%b %d")
            labels.append(date_str)
            scores.append(round(audit.average_score, 1))
            audit_ids.append(audit.id)
    
    return {
        "labels": labels,
        "scores": scores,
        "audit_ids": audit_ids
    }


@router.patch("/{schedule_id}")
async def update_schedule(
    schedule_id: str,
    request: ScheduleUpdateRequest,
    db: AsyncSession = Depends(get_db)
):
    """Update schedule configuration."""
    result = await db.execute(
        select(ScheduledAudit).where(ScheduledAudit.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    
    if not schedule:
        raise_not_found("Schedule")
    
    # Update fields
    if request.name is not None:
        schedule.name = request.name
    if request.schedule_cron is not None:
        schedule.schedule_cron = request.schedule_cron
    if request.is_active is not None:
        schedule.is_active = 1 if request.is_active else 0
    if request.language is not None:
        schedule.language = request.language
    if request.concurrency is not None:
        schedule.concurrency = request.concurrency
    
    schedule.updated_at = datetime.now(timezone.utc)
    
    await db.commit()
    await db.refresh(schedule)
    
    data = schedule.to_dict()
    data['schedule_human'] = _cron_to_human(schedule.schedule_cron)
    
    return data


@router.delete("/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Delete a scheduled audit."""
    result = await db.execute(
        select(ScheduledAudit).where(ScheduledAudit.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    
    if not schedule:
        raise_not_found("Schedule")
    
    await db.delete(schedule)
    await db.commit()
    
    return {"message": "Schedule deleted successfully"}


@router.post("/{schedule_id}/run")
async def trigger_schedule_run(
    schedule_id: str,
    db: AsyncSession = Depends(get_db)
):
    """Manually trigger a scheduled audit run."""
    result = await db.execute(
        select(ScheduledAudit).where(ScheduledAudit.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    
    if not schedule:
        raise_not_found("Schedule")
    
    # Create new audit
    audit_id = str(uuid.uuid4())
    audit = Audit(
        id=audit_id,
        website=schedule.website,
        sitemap_url=schedule.sitemap_url,
        audit_type=schedule.audit_type,
        provider=schedule.provider,
        model=schedule.model or "default",
        status="pending",
        current_step=f"manual_trigger_{schedule.id}"
    )
    db.add(audit)
    
    # Update schedule
    schedule.last_run_at = datetime.now(timezone.utc)
    schedule.last_audit_id = audit_id
    schedule.run_count += 1
    
    await db.commit()
    
    # Start pipeline
    create_tracked_task(
        start_audit_pipeline(
            audit_id=audit_id,
            website=schedule.website,
            sitemap_url=schedule.sitemap_url,
            audit_type=schedule.audit_type,
            provider=schedule.provider,
            model=schedule.model or "default",
            max_chars=30000,
            use_direct_mode=True,
            concurrency=schedule.concurrency,
            use_perplexity=bool(schedule.use_perplexity),
            language=schedule.language
        ),
        name=f"schedule-trigger-pipeline-{audit_id}",
        timeout=14400,
    )

    # Auto-summary if enabled
    if schedule.summary_provider and schedule.summary_model:
        create_tracked_task(
            _poll_and_generate_summary(
                audit_id,
                schedule.summary_provider,
                schedule.summary_model,
                schedule.language
            ),
            name=f"schedule-trigger-summary-{audit_id}",
            timeout=14400,
        )
    
    return {
        "message": "Schedule triggered successfully",
        "audit_id": audit_id
    }
