# Faza 5 — Auditul prompturilor

## Rezumat

| ID | Sev | Titlu | Fișier:linie | Verificat prin |
|----|-----|-------|---------------|-----------------|
| F5-01 | — | **CORECTARE ipoteză din plan**: `relevancy_audit`/`gdpr_audit`/`ecommerce_audit`/etc. NU sunt bug-uri — sunt tipuri de audit legacy dintr-un pipeline CLI mort, sau nume interne corect mapate | vezi secțiunea „Investigație" mai jos |
| F5-02 | P3 | `geo_audit.yaml` și `ux_content.yaml` încadrează LLM-ul cu "2024-2025"/"2025" ca "prezent" — cadrul temporal e acum învechit (suntem în 2026-09) | `prompts/geo_audit.yaml:6`, `prompts/ux_content.yaml:8,85` |
| F5-03 | P2 | `content_brief.yaml` și `draft_optimizer.yaml` nu respectă schema standard (`role/task/output_schema`) — dar au loadere proprii dedicate, deci NU e bug | `api/routes/content_briefs.py:116-125`, `api/routes/draft_optimizer.py:83-87` |
| F5-04 | P3 | `api/prompts/meta_generator.txt` — CLAUDE.md descrie `api/prompts/` ca „prompt files pentru fiecare tip de audit"; de fapt conține un singur `.txt` nelegat de sistemul de audit-uri YAML | `api/prompts/meta_generator.txt` + CLAUDE.md secțiunea Structura directoarelor |
| F5-05 | ✅ verificat, fără problemă | Contractul output_schema (YAML) ↔ cheia citită de `core/direct_analyzer.py::get_prefix_for_audit` — **toate cele 18 tipuri "v2.0" se potrivesc exact**, inclusiv cele cu nume de wrapper diferit de fișier (`legal_gdpr.yaml→gdpr_audit`, `e_commerce.yaml→ecommerce_audit`, `competitor_analysis.yaml→competitive_positioning_audit`) | vezi tabelul de mapping de mai jos |

---

## Investigație: ipoteza inițială despre prompturi lipsă

Recon-ul inițial (înainte de verificare) semnalase drept suspecte cheile
`relevancy_audit`, `gdpr_audit`, `ecommerce_audit`, `freshness_audit`,
`brand_voice_audit`, `local_seo_audit`, `ux_content_audit`, `ai_overview_audit`
— pentru că niciun fișier YAML nu se numește exact așa. **După verificare prin
citire și execuție directă, concluzia se schimbă:**

### A. `relevancy_audit`, `greenwashing`, `advertisment`, `kantar` — cod legacy neconectat la aplicația web

Aceste 4 chei apar în `core/determine_score.py`, `core/generate_dashboard.py`,
`core/history_tracker.py` și în ramuri dedicate din `core/direct_analyzer.py`
(`get_prefix_for_audit`, liniile 460-483). **Nu au niciun fișier YAML în
`prompts/`.**

Verificare prin import graph:
```bash
grep -rln "generate_dashboard\|determine_score" --include="*.py" api app
# → niciun rezultat
grep -n "generate_dashboard\|determine_score" main.py
# → main.py:23 from core import determine_score
# → main.py:125 determine_score.perform_full_audit_suite(root_dir, output_file)
```
**Concluzie:** `determine_score.py` și `generate_dashboard.py` sunt folosite
**exclusiv** de orchestratorul CLI legacy de la rădăcină (`main.py`), niciodată
de aplicația FastAPI (`api/`). Mai mult, `perform_full_audit_suite` doar
scanează foldere `output_<tip>/` de pe disc după fișiere JSON deja generate —
nu apelează `PromptLoader`, deci nu ar arunca `PromptNotFoundError` nici dacă
ar rula. Pur și simplu ar găsi zero foldere pentru aceste 4 tipuri și ar
genera rânduri goale în Excel.

**Verdict: P2 (cod mort/legacy), nu P0.** Aceste 4 tipuri de audit nu sunt
accesibile din UI-ul curent (confirmat: nu apar în
`api/routes/pages/_shared.py::_AUDIT_TYPE_LABELS`, nici în
`api/provider_registry.py`, nici în `new_audit.html`). Relevant pentru
**Faza 1** (decizie: se arhivează `main.py` + cei 4 helperi CLI, sau se
păstrează ca tool separat de raportare batch?).

### B. `gdpr_audit`, `ecommerce_audit`, `freshness_audit`, etc. — nume interne corecte, nu chei de fișier

Aceste chei apar în secțiunea „v2.0" din `get_prefix_for_audit`
(`core/direct_analyzer.py:485-608`) ca **numele wrapper-ului JSON din
răspunsul LLM**, nu ca nume de fișier YAML. Fișierul YAML se identifică după
`question_type` (ex. `LEGAL_GDPR` → `legal_gdpr.yaml`), dar conținutul JSON pe
care LLM-ul îl întoarce e împachetat sub o cheie diferită
(`legal_gdpr.yaml` → `{"gdpr_audit": {...}}`).

Verificare: am extras cheia de wrapper efectivă din `output_schema` al
fiecărui YAML și am comparat-o cu cea așteptată de `direct_analyzer.py`:

| Tip audit (`question_type`) | Fișier YAML | Wrapper așteptat în cod | Wrapper real în `output_schema` | Match |
|---|---|---|---|---|
| SEO_AUDIT | seo_audit.yaml | seo_audit | seo_audit | ✅ |
| GEO_AUDIT | geo_audit.yaml | geo_audit | geo_audit | ✅ |
| ACCESSIBILITY_AUDIT | accessibility_audit.yaml | accessibility_audit | accessibility_audit | ✅ |
| UX_CONTENT | ux_content.yaml | ux_content_audit | ux_content_audit | ✅ |
| LEGAL_GDPR | legal_gdpr.yaml | gdpr_audit | gdpr_audit | ✅ |
| CONTENT_QUALITY | content_quality.yaml | content_quality | content_quality | ✅ |
| BRAND_VOICE | brand_voice.yaml | brand_voice_audit | brand_voice_audit | ✅ |
| E_COMMERCE | e_commerce.yaml | ecommerce_audit | ecommerce_audit | ✅ |
| TRANSLATION_QUALITY | translation_quality.yaml | translation_audit | translation_audit | ✅ |
| INTERNAL_LINKING | internal_linking.yaml | internal_linking | internal_linking | ✅ |
| COMPETITOR_ANALYSIS | competitor_analysis.yaml | competitive_positioning_audit | competitive_positioning_audit | ✅ |
| SPELLING_GRAMMAR | spelling_grammar.yaml | spelling_grammar_audit | spelling_grammar_audit | ✅ |
| READABILITY_AUDIT | readability_audit.yaml | readability_audit | readability_audit | ✅ |
| TECHNICAL_SEO | technical_seo.yaml | technical_seo_audit | technical_seo_audit | ✅ |
| CONTENT_FRESHNESS | content_freshness.yaml | freshness_audit | freshness_audit | ✅ |
| LOCAL_SEO | local_seo.yaml | local_seo_audit | local_seo_audit | ✅ |
| SECURITY_CONTENT_AUDIT | security_content_audit.yaml | security_content_audit | security_content_audit | ✅ |
| AI_OVERVIEW_OPTIMIZATION | ai_overview_optimization.yaml | ai_overview_audit | ai_overview_audit | ✅ |

**18/18 potrivire exactă.** Cod inconsecvent ca denumire, dar **corect și
sincronizat**. Codul are chiar comentarii explicite care documentează
mapping-ul (ex. `direct_analyzer.py:542`: `# prompts/translation_quality.yaml
→ "translation_audit": {...}`). Acesta era exact riscul pe care planul îl
semnala drept „cel mai perfid tip de bug" — verificat manual, **nu există**.

### C. Sursa reală a listei de tipuri de audit din UI

`api/routes/audits.py:170-176`: pentru rulare normală, `audit_type` vine direct
din formularul de creare audit și trebuie să corespundă (uppercase) unui
fișier din `prompts/`. Pentru „god mode", lista se generează dinamic din
`list_available_audits()` (`core/prompt_loader.py`) — deci **prin construcție,
lista de tipuri disponibile nu poate diverge de fișierele YAML existente**.
`run_single()` prinde orice excepție de `load_prompt()` și o transformă în
`{"error": ..., "status": "failed"}` per-tip, nu crapă tot request-ul.
**Fără risc de P0 pe acest flux.**

---

## Schema uniformă (validare `_validate_prompt_structure`)

```
required = ['name', 'description', 'version', 'role', 'task', 'output_schema']
```

| Fișier | Câmpuri lipsă | Observație |
|---|---|---|
| `content_brief.yaml` | role, task, output_schema | Nu trece prin `PromptLoader` — are loader propriu (`content_briefs.py:116`, citește `base_prompt`+`json_schema`). **Nu e bug.** |
| `draft_optimizer.yaml` | output_schema | Nu trece prin `PromptLoader` — loader propriu (`draft_optimizer.py:83`). **Nu e bug**, dar output-ul nu are deloc schemă JSON documentată în YAML (vezi F5-06 mai jos). |
| toate celelalte 18 | — | ✅ complete |

### F5-06 (P3) — `draft_optimizer.yaml` nu documentează formatul JSON de output

Spre deosebire de `content_brief.yaml` (are `json_schema` explicit),
`draft_optimizer.yaml` are doar `role`/`task`/`description`/`name`/`version` —
niciun contract de format pentru răspunsul LLM. Formatul e probabil descris
inline în `task`, dar nu poate fi verificat automat/versionat separat. Impact
redus (funcționează dacă `task` conține instrucțiuni suficiente), dar fragil
la modificări viitoare ale promptului.

---

## Dimensiune prompturi

```
Total: 230.967 caractere / 20 fișiere YAML (medie ~11.5KB/fișier)
Cel mai mic:  draft_optimizer.yaml    (4.690 caractere)
Cel mai mare: geo_audit.yaml         (16.041 caractere)
              seo_audit.yaml         (15.555 caractere)
              local_seo.yaml         (15.213 caractere)
```
Nimic disproporționat — dimensiunile cresc natural cu complexitatea temei
(geo/seo/local sunt cele mai elaborate, ceea ce e de așteptat). Nu e nevoie de
intervenție pe cost per audit din cauza dimensiunii promptului.

---

## Igiena conținutului

- **Conținut cu cadru temporal învechit** (F5-02): `geo_audit.yaml:6` spune
  explicit LLM-ului că are „deep expertise... in 2024-2025"; `ux_content.yaml:8,85`
  spune „you are current with 2025 UX content considerations" și „powerful
  trust signals in 2025". Data curentă a proiectului e 2026-09 — cadrul e
  cu >1 an în urmă. Nu rupe funcționalitatea (LLM-ul tot poate analiza
  conținut), dar subtil poate face modelul să ancoreze răspunsuri la un
  context temporal care nu mai e „acum". Recomandare: fie eliminați anul
  hardcodat (formulare atemporală: „you have deep expertise in how LLMs
  currently select and cite content"), fie actualizați-l periodic.
- **Nicio instrucțiune imposibilă pentru un LLM fără tool-uri** — verificat
  cu grep pe pattern-uri de tipul „measure the actual load speed" / „verify
  real Core Web Vitals" → 0 rezultate. Prompturile cer analiză de conținut
  text, nu măsurători live.
- **Nicio contaminare de limbă** — toate cele 20 YAML sunt integral în
  engleză (verificat cu regex pe diacritice românești) — consecvent cu restul
  bazei de prompturi.
- **Fără instrucțiuni contradictorii evidente** în interiorul aceluiași fișier
  (verificare manuală pe `geo_audit.yaml`, `seo_audit.yaml`, `ux_content.yaml`
  — cele mai mari/complexe). Structura `role → task → criteria → scoring →
  output_schema` e respectată consecvent.

---

## Prompturi orfane / neconectate din UI

Toate cele 18 YAML „standard" sunt conectate (confirmat prin
`_AUDIT_TYPE_LABELS` + `list_available_audits()`). `content_brief.yaml` și
`draft_optimizer.yaml` au propriile rute dedicate (`/api/briefs`,
`/api/draft-optimizer`) — active, nu orfane.
**Niciun prompt orfan găsit.**

## `api/prompts/` vs `prompts/` — clarificare pentru CLAUDE.md

CLAUDE.md descrie `api/prompts/` generic ca parte a structurii de prompturi
per tip de audit, ceea ce sugerează că e o continuare/duplicat al
`prompts/`. În realitate conține **un singur fișier**,
`meta_generator.txt`, folosit exclusiv de funcția separată „Meta Generator"
(`api/routes/meta_generator.py`) — un generator de title/meta description/H1,
nu un tip de audit. E o resursă legitimă, corect folosită, dar denumirea/
locația e derutantă. **Recomandare (Faza 9): fie mutați `meta_generator.txt`
lângă celelalte prompturi din `prompts/` (cu un subfolder `prompts/features/`
sau similar), fie actualizați CLAUDE.md ca să reflecte corect scopul
`api/prompts/`.**

---

## Descoperire colaterală relevantă pentru Faza 4 (model IDs)

În timpul verificării loaderelor de prompturi, am găsit în
`api/routes/draft_optimizer.py:101` un fallback de model **`"claude-sonnet-4-6"`**
— ID inexistent în `api/provider_registry.py` (care folosește peste tot
`claude-sonnet-4-20250514`). Dacă un apel către `/api/draft-optimizer` nu
specifică explicit provider+model și doar `ANTHROPIC_API_KEY` e setat, cererea
către Anthropic va folosi acest ID inventat. **Detaliat complet în
`04-integrations.md` (F4-01)** — nu re-documentat aici ca să evit duplicarea.

---

## Concluzie Faza 5

Sistemul de prompturi e **mai solid decât sugera recon-ul inițial**: contract
output↔cod verificat 18/18, fără prompturi orfane, fără instrucțiuni
imposibile, fără mixaj de limbă. Findings reale sunt de igienă (P2/P3):
cod legacy neconectat (4 tipuri CLI-only), doi prompturi cu schemă
nestandard dar intenționat (content_brief, draft_optimizer — al doilea fără
schemă JSON documentată), cadru temporal învechit în 2 prompturi, și o
denumire derutantă pentru `api/prompts/`.
