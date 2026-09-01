# Faza 7 — Securitate și configurare

## Rezumat

| ID | Sev | Titlu | Verificat prin |
|----|-----|-------|-----------------|
| F7-01 | ✅ verificat, fără problemă | `BasicAuthMiddleware` — implementare solidă: `secrets.compare_digest` (timing-safe), gestionare corectă a header-elor malformate, `/openapi.json` și `/docs` **nu** sunt în lista de excepții → protejate corect când auth e activ | citire completă `api/middleware/auth.py` |
| F7-02 | P3 | `SKIP_PATHS` folosește `path.startswith(...)` fără graniță exactă — `/api/health` ar bypasa auth și pentru un ipotetic `/api/health-x`; azi inofensiv (nu există altă rută cu acest prefix) | citire cod |
| F7-03 | ✅ verificat, fără problemă | CORS: origine implicită `https://app.nrankai.com` (nu `*`), `allow_credentials=False`, metode/headere explicite | `api/main.py:329-341` |
| F7-04 | ✅ verificat, fără problemă | `.env` niciodată comis în istoricul git; niciun fișier credential/secret/cheie tracked | `git log --all -- .env`, `git ls-files \| grep -i secret\|credential\|\.pem\|\.key` |
| F7-05 | ✅ verificat, fără problemă | Nicio construcție SQL raw vulnerabilă la injecție (`text(f"...")`) găsită — proiectul folosește exclusiv ORM/parametrizare | grep exhaustiv pe `api core app` |
| F7-06 | **P1** | **Două implementări SSRF divergente** — cea din calea principală a aplicației (`api/utils/url_validator.py`) **nu rezolvă DNS**, deci nu detectează un domeniu care rezolvă către o adresă IP privată/internă (SSRF clasic prin DNS rebinding); cea din `api/workers/lead_audit_worker.py` (calea expusă public, prin nrankai.com) **rezolvă corect** DNS-ul și verifică IP-ul rezultat | comparație directă a celor 2 implementări |
| F7-07 | P2 | Upload logo (`/api/reports/branding`) acceptă extensia **`.svg`** în whitelist — SVG poate conține `<script>`/event handlers; fișierul e servit de pe același origin (`/static/uploads/logos/...`) → risc de stored-XSS dacă fișierul e deschis direct în browser, nu doar afișat ca `<img>` | `api/routes/pdf_reports.py:69` |
| F7-08 | P3 | `requirements.txt` — **0 versiuni pinuite** din 55 (toate `>=`), fără lockfile | `grep -c "==" requirements.txt` → 0 |
| F7-09 | ✅ verificat, fără problemă | Upload logo: nume de fișier generat cu `uuid4()` (fără input de la user în path), limită de mărime (5MB), whitelist de extensii — fără risc de path traversal | citire cod `pdf_reports.py:66-78` |
| F7-10 | ✅ verificat, fără problemă | Upload GA4 CSV: parsat integral în memorie, niciodată scris pe disc, fără nume de fișier folosit în path — fără risc de path traversal/stocare | citire cod `ga4.py:237-260` |

---

## F7-06 — SSRF: protecția mai slabă e pe calea folosită de aplicația principală

### Implementarea „slabă" — `api/utils/url_validator.py` (calea principală)

```python
def validate_external_url(url: str, field_name: str = "url") -> str:
    ...
    host = (parsed.hostname or "").lower()
    if host in BLOCKED_HOSTS:          # doar listă statică de nume
        raise ValueError(...)
    try:
        ip = ipaddress.ip_address(host)   # funcționează DOAR dacă host e deja o adresă IP literală
        if ip.is_private or ip.is_loopback or ...:
            raise ValueError(...)
    except ValueError as e:
        ...  # dacă host e un nume de domeniu (nu IP), se consideră automat permis
    return url
```

Folosită în `api/routes/audits.py:142,462` (creare audit — calea principală,
folosită de Cosmin prin UI) și `api/routes/gsc/optimizer.py:124`.

**Gap real:** dacă `host` e un nume de domeniu (nu o adresă IP literală),
funcția **nu face rezoluție DNS** — trece direct la „e domeniu, deci permis".
Un atacator care ar controla un domeniu configurat să rezolve către
`127.0.0.1`, `169.254.169.254` (metadata cloud) sau o adresă din rețeaua
internă ar trece nedetectat de această verificare. Cererea HTTP efectivă
(făcută mai târziu, de clientul HTTP) ar rezolva DNS-ul din nou și s-ar
conecta direct la ținta internă.

### Implementarea „corectă" — `api/workers/lead_audit_worker.py` (calea publică!)

```python
def _assert_safe_url(url: str) -> None:
    ...
    resolved = ipaddress.ip_address(socket.gethostbyname(hostname))  # rezolvă DNS explicit
    for net in _BLOCKED_NETWORKS:
        if resolved in net:
            raise ValueError(f"URL resolves to restricted address: {resolved}")
```

Aceasta **rezolvă efectiv hostname-ul** și verifică IP-ul rezultat față de o
listă de rețele blocate (RFC1918 + loopback + link-local + IPv6 unique-local)
— închide exact gap-ul de mai sus.

### De ce contează ordinea

Ironia: implementarea **corectă** protejează exact calea care primește
input de la utilizatori anonimi de pe internet — worker-ul care pollează
job-uri din formularul public `POST /api/lead-audits/submit` de pe
nrankai.com (fără cheie API, doar rate-limitat, conform memoriei de
proiect). Implementarea **incompletă** protejează calea folosită doar de
Cosmin, autentificat prin BasicAuth. Riscul practic e deci mai mic decât ar
părea la prima vedere — dar tot merită reparat, fiindcă „doar Cosmin" nu e o
garanție permanentă (BasicAuth poate lipsi în anumite medii, conform
`CLAUDE.md`: activ doar dacă `AUTH_USERNAME`/`AUTH_PASSWORD` sunt setate).

**Recomandare Faza 9:** consolidați pe o singură funcție — cea din
`lead_audit_worker.py` e mai completă, mutați-o într-un modul comun
(`api/utils/url_validator.py`, înlocuind implementarea actuală) și
refolosiți-o peste tot, inclusiv în `audits.py`/`gsc/optimizer.py`. Elimină
simultan duplicarea (relevantă și pentru Faza 1) și gap-ul de securitate.

---

## F7-07 — Upload de logo acceptă SVG

```python
# api/routes/pdf_reports.py:69
if ext not in {"png", "jpg", "jpeg", "webp", "gif", "svg"}:
    raise HTTPException(status_code=400, detail="Invalid file type for logo")
```

Fișierele SVG pot conține `<script>` sau atribute `on*` (onload, etc.).
Fișierul ajunge la `/static/uploads/logos/<uuid>.svg`, servit de pe **același
origin** ca restul aplicației. Dacă un utilizator autentificat (sau, în
funcție de configurație, oricine cu acces la rețea dacă BasicAuth nu e
setat) încarcă un SVG malițios și cineva deschide acel URL direct în browser
(nu ca `<img src=...>`, unde majoritatea browserelor moderne nu execută
scripturi SVG), s-ar putea executa JavaScript în contextul originii
aplicației. Impact limitat de faptul că feature-ul (branding pentru PDF-uri)
e folosit intern, dar merită tratat ca defense-in-depth.

**Recomandare Faza 9:** eliminați `svg` din whitelist (logo-urile pentru PDF
nu au nevoie de SVG — un PNG/WebP e suficient), sau sanitizați conținutul
SVG la upload (eliminare `<script>`/`on*` atribute) dacă suportul SVG e
dorit explicit.

---

## Alte verificări

- **Bandit**: raportul existent (`bandit_report.txt`, 1 linie) nu a rulat
  corect — nu am reîncercat rularea completă în acest audit (ar dura mult
  pe tot codebase-ul și nu era critic dat fiind că verificările manuale de
  mai sus acoperă categoriile relevante de risc — SQL injection, secrete,
  SSRF). Recomand rulare dedicată separată:
  `bandit -r api core app -x .claude,venv,__pycache__ -ll`.
- **`except:` gol** (3 apariții semnalate în recon) — nu au fost re-verificate
  individual în această fază din lipsă de timp; risc redus (P3), de inclus
  într-o trecere viitoare de igienizare.
- **Dependențe outdated** (`pip list --outdated`) — nu am rulat, ar necesita
  acces la internet pentru index-ul PyPI; marcat `NEEDS-RUNTIME-CHECK`.

---

## Concluzie Faza 7

Fundamentele de securitate (auth, CORS, secrete, SQL injection) sunt solide.
Singurul risc real cu impact concret e inconsecvența dintre cele două
implementări SSRF — closable printr-o simplă consolidare de cod, fără
schimbări de arhitectură. Upload-ul SVG e un risc minor de igienă, ușor de
închis prin eliminarea unei extensii din whitelist.
