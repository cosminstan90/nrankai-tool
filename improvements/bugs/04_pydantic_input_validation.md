Add proper input validation (max_length, URL patterns) to Pydantic request models.

## Problem
Request models in `api/models/schemas.py` and inline in routes lack basic constraints:
- `website` field accepts strings of unlimited length
- `sitemap_url` accepts any string (not validated as URL)
- No regex patterns on domain/URL fields
- Allows injection of huge strings causing memory issues

## Fix

### schemas.py — AuditCreate and similar models
```python
from pydantic import Field, field_validator
import re

class AuditCreate(BaseModel):
    website: str = Field(..., min_length=1, max_length=255)
    sitemap_url: Optional[str] = Field(None, max_length=2048)
    notes: Optional[str] = Field(None, max_length=5000)

    @field_validator("website")
    @classmethod
    def validate_website(cls, v: str) -> str:
        v = v.strip().lower()
        # Accept bare domain or https:// URL
        if not re.match(r'^(https?://)?[a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?(\.[a-z]{2,})+', v):
            raise ValueError("Invalid website domain")
        return v
```

### All webhook_url fields
```python
webhook_url: Optional[str] = Field(None, max_length=500)

@field_validator("webhook_url")
@classmethod
def validate_webhook_url(cls, v):
    if v and not v.startswith(("http://", "https://")):
        raise ValueError("Webhook URL must start with http:// or https://")
    return v
```

### Query string inputs (keyword_research, fanout)
```python
query: str = Field(..., min_length=1, max_length=500)
```

## Files
- `api/models/schemas.py`
- Any route file with inline Pydantic models using `website`, `url`, `query` fields
