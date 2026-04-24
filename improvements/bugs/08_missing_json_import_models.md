Fix missing `import json` in model files causing NameError at runtime.

## Problem
Several `to_dict()` methods call `json.loads()` or `json.dumps()` without `import json`
at the top of the file. This causes `NameError: name 'json' is not defined` at runtime
when these methods are called.

## Affected locations
- `api/models/content.py` lines 38, 74, 236, 283, 336
- `api/models/infra.py` line 377

## Fix
Add `import json` at the top of each affected file if not already present:

```python
import json  # add this line near the top, with other stdlib imports
```

## Verification
```bash
python -c "from api.models.content import ContentBrief; b = ContentBrief(); b.to_dict()"
python -c "from api.models.infra import BenchmarkProject; b = BenchmarkProject(); b.to_dict()"
```
Neither should raise NameError.

## Files
- `api/models/content.py` — add `import json`
- `api/models/infra.py` — add `import json`
