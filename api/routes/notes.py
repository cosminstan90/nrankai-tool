"""
Result Notes API — one free-text note per AuditResult row.

GET  /api/notes/{result_id}   → return existing note or {"note": ""}
PUT  /api/notes/{result_id}   → upsert note text ({"note": "..."})
DELETE /api/notes/{result_id} → delete note
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from api.utils.errors import raise_not_found
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from api.models.database import get_db, ResultNote, AuditResult

router = APIRouter(prefix="/api/notes", tags=["notes"])


class NotePayload(BaseModel):
    note: str


@router.get("/{result_id}")
async def get_note(result_id: int, db: AsyncSession = Depends(get_db)):
    """Return the note for a result, or empty string if none exists."""
    row = await db.execute(
        select(ResultNote).where(ResultNote.result_id == result_id)
    )
    note = row.scalar_one_or_none()
    return {"result_id": result_id, "note": note.note if note else "", "updated_at": note.updated_at.isoformat() if note else None}


@router.put("/{result_id}")
async def upsert_note(result_id: int, payload: NotePayload, db: AsyncSession = Depends(get_db)):
    """Create or update the note for a result."""
    # Verify result exists
    res = await db.execute(select(AuditResult.id).where(AuditResult.id == result_id))
    if not res.scalar_one_or_none():
        raise_not_found("Result")

    row = await db.execute(
        select(ResultNote).where(ResultNote.result_id == result_id)
    )
    note = row.scalar_one_or_none()

    if note:
        note.note = payload.note
        note.updated_at = datetime.now(timezone.utc)
    else:
        note = ResultNote(result_id=result_id, note=payload.note)
        db.add(note)

    await db.commit()
    await db.refresh(note)
    return {"success": True, "result_id": result_id, "note": note.note, "updated_at": note.updated_at.isoformat()}


@router.delete("/{result_id}")
async def delete_note(result_id: int, db: AsyncSession = Depends(get_db)):
    """Delete the note for a result."""
    await db.execute(delete(ResultNote).where(ResultNote.result_id == result_id))
    await db.commit()
    return {"success": True, "result_id": result_id}
