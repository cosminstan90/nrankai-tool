"""
Standardized error helpers for geo_tool API routes.

Usage:
    from api.utils.errors import raise_not_found, raise_bad_request

    raise_not_found("Audit", audit_id)
    raise_bad_request("Invalid JSON in request body")
"""

from fastapi import HTTPException


def raise_not_found(entity: str, identifier: str | int | None = None) -> None:
    """Raise a 404 HTTPException with a standardized message."""
    detail = f"{entity} not found"
    if identifier is not None:
        detail = f"{entity} '{identifier}' not found"
    raise HTTPException(status_code=404, detail=detail)


def raise_bad_request(message: str) -> None:
    """Raise a 400 HTTPException."""
    raise HTTPException(status_code=400, detail=message)


def raise_conflict(message: str) -> None:
    """Raise a 409 HTTPException."""
    raise HTTPException(status_code=409, detail=message)
