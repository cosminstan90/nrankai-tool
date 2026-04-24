Fix stored XSS in HTML export function in `api/routes/action_cards.py`.

## Problem
Lines 1098-1150: User-controlled data (page URLs, action text, LLM-generated reasons) is
interpolated directly into HTML strings using f-strings without escaping:

```python
html += f"<div class=\"card-url\">{card.page_url}</div>"
html += f"<div class=\"action-title\">{action.get('action')}</div>"
```

If `card.page_url` or LLM response contains `<script>alert(1)</script>`, the exported HTML
file will execute JavaScript when opened in a browser.

## Fix
Import and apply `html.escape()` to all user-controlled values before interpolation:

```python
from html import escape

html += f"<div class=\"card-url\">{escape(card.page_url)}</div>"
html += f"<div class=\"action-title\">{escape(action.get('action', ''))}</div>"
html += f"<div class=\"reason\">{escape(action.get('reason', ''))}</div>"
```

Apply `escape()` to every variable inside an f-string that produces HTML.
Do not escape static string literals.

## Files
- `api/routes/action_cards.py` lines 1098-1150
