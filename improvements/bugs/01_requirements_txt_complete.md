Complete requirements.txt with all packages actually used by the application.

## Problem
`requirements.txt` is missing core framework dependencies. The app cannot be installed
on a new machine with just `pip install -r requirements.txt`.

## Fix
Run on a working environment:
```bash
pip freeze > requirements_current.txt
```

Then audit the output against actual imports in `api/` and add missing packages.
Minimum packages to verify are present:

- fastapi
- uvicorn[standard]
- starlette
- slowapi
- sse-starlette
- sqlalchemy[asyncio]
- aiosqlite
- pydantic
- pydantic-settings
- jinja2
- python-multipart        # for UploadFile / Form
- httpx                   # async HTTP client
- aiofiles                # async file operations
- python-dotenv
- alembic
- anthropic
- openai
- google-generativeai
- mistralai
- weasyprint              # or reportlab — for PDF generation
- html2text
- undetected-chromedriver # or playwright — for web scraping

Pin major versions (e.g. `fastapi>=0.115,<1.0`) not exact patch versions.
Do not pin to exact hashes unless using a lockfile strategy.

## Files
- `requirements.txt`
