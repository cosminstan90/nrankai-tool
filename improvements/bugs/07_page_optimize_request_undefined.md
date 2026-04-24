Fix `PageOptimizeRequest` undefined class that breaks OpenAPI schema generation.

## Problem
`api/routes/gsc/optimizer.py` uses `PageOptimizeRequest` in function signatures at
lines 93 and 390, but the class is not defined anywhere in that file.
Only `CannibalizationRequest` and `StandaloneOptimizeRequest` are defined.

This causes `app.openapi()` to raise `PydanticUserError`, breaking the entire
OpenAPI/Swagger schema at startup.

## Diagnosis
Run: `python -c "from api.routes.gsc.optimizer import router"` — will fail with NameError.
Or: check the actual function signatures at lines 93 and 390 to see what request body
they expect and what the correct Pydantic model should be.

## Fix

Option A — `PageOptimizeRequest` is the same as `StandaloneOptimizeRequest`:
```python
PageOptimizeRequest = StandaloneOptimizeRequest  # alias
```

Option B — `PageOptimizeRequest` needs its own fields (inspect what the handler uses):
```python
class PageOptimizeRequest(BaseModel):
    url: str
    # ... add whatever fields the handler at line 93 and 390 actually use
```

Determine which option is correct by reading the handler bodies at lines 93 and 390
and checking what fields they access on the request object.

## Verification
After fix: `python -c "from api.main import app; app.openapi()"` must complete without error.

## Files
- `api/routes/gsc/optimizer.py` lines 93, 390
