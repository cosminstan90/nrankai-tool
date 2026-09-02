"""
SerpIQ — Verdict Engine
========================
Generates a single, actionable verdict for each SerpIQ analysis alongside
a set of human-readable evidence bullets.

Verdicts
--------
  dominate  — site already at #1-3; maintain position
  optimize  — site at #4-10; on-page / authority improvements needed
  compete   — site at #11-20; major effort required to reach page 1
  gap       — URL given but not found in top 20 for this keyword
  create    — keyword given, no URL from this domain in top 20

Public interface
----------------
  SerpDifficulty         — dataclass returned by analyze_serp_difficulty()
  SerpIQVerdictEngine    — main class
    generate_verdict(...)             → (verdict_str, evidence_list)
    analyze_serp_difficulty(items)    → SerpDifficulty
    generate_serp_summary(items, d)   → str (2-3 sentence narrative)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("serpiq.verdict")

# ---------------------------------------------------------------------------
# Well-known "authority" domains — their presence signals a harder keyword
# ---------------------------------------------------------------------------

_AUTHORITY_DOMAINS: frozenset[str] = frozenset({
    "wikipedia.org", "youtube.com", "reddit.com", "quora.com",
    "amazon.com", "amazon.co.uk", "amazon.de",
    "gov.ro", "anpc.ro", "bnr.ro",
    "forbes.com", "businessinsider.com", "techcrunch.com", "bbc.com", "bbc.co.uk",
    "nytimes.com", "theguardian.com",
    "healthline.com", "webmd.com", "mayoclinic.org",
    "investopedia.com", "nerdwallet.com", "bankrate.com",
})

# Schema types that strongly indicate product / e-commerce pages
_PRODUCT_SCHEMA: frozenset[str] = frozenset({
    "Product", "Offer", "AggregateOffer",
})

# Schema types that strongly indicate articles / informational pages
_ARTICLE_SCHEMA: frozenset[str] = frozenset({
    "Article", "BlogPosting", "NewsArticle", "TechArticle",
    "ScholarlyArticle", "WebPage",
})

# Schema types for tools, apps, SaaS
_TOOL_SCHEMA: frozenset[str] = frozenset({
    "SoftwareApplication", "WebApplication", "MobileApplication",
})


# ---------------------------------------------------------------------------
# SerpDifficulty — result of analyze_serp_difficulty()
# ---------------------------------------------------------------------------

@dataclass
class SerpDifficulty:
    """Aggregated SERP-composition signals."""

    # "high" | "medium" | "low"  — heuristic based on authority presence
    difficulty_signal:    str          = "medium"

    # "informational" | "commercial" | "transactional" | "navigational" | "mixed"
    dominant_content_type: str         = "mixed"

    has_featured_snippet:  bool        = False
    local_pack_present:    bool        = False

    # Extra diagnostic fields (not exposed in summary, used in evidence)
    authority_domains_found: list[str] = field(default_factory=list)
    dominant_domain:         Optional[str] = None   # set when >3 results from one domain
    avg_word_count_top3:     Optional[int] = None   # average estimated WC of pos 1-3


# ---------------------------------------------------------------------------
# Main engine
# ---------------------------------------------------------------------------

class SerpIQVerdictEngine:
    """
    Stateless verdict generator.  All methods are synchronous and pure
    (no I/O, no DB) so they can be called inside async route handlers
    without any await.
    """

    # ------------------------------------------------------------------
    # Primary verdict
    # ------------------------------------------------------------------

    def generate_verdict(
        self,
        input_type:     str,                    # 'url' | 'keyword'
        position_found: Optional[int],          # 1-based rank or None
        serp_items:     list,                   # list[SERPItem] or list[dict]
        url_analysis:   Optional[object] = None,  # URLAnalysis or dict (nullable)
    ) -> tuple[str, list[str]]:
        """
        Determine the verdict and build evidence bullets.

        Parameters
        ----------
        input_type    : 'url' or 'keyword'
        position_found: rank of the input URL in the SERP (None = not found)
        serp_items    : SERP result list (SERPItem dataclasses or dicts)
        url_analysis  : optional on-page metrics for the input URL

        Returns
        -------
        (verdict, evidence)  where evidence is a list of non-empty strings.
        """
        difficulty = self.analyze_serp_difficulty(serp_items)

        if input_type == "url":
            return self._verdict_for_url(position_found, serp_items, url_analysis, difficulty)
        else:
            return self._verdict_for_keyword(serp_items, difficulty)

    # ------------------------------------------------------------------
    # URL branch
    # ------------------------------------------------------------------

    def _verdict_for_url(
        self,
        position_found: Optional[int],
        serp_items:     list,
        url_analysis:   Optional[object],
        difficulty:     SerpDifficulty,
    ) -> tuple[str, list[str]]:

        evidence: list[str] = []

        # ── Position not found → gap ──────────────────────────────────────
        if position_found is None:
            evidence.append(
                "URL-ul nu apare in top 20 pentru acest keyword. "
                "Verifica daca keyword-ul e relevant sau daca pagina e indexata."
            )
            if url_analysis is not None:
                is_indexable = _attr(url_analysis, "is_indexable")
                if is_indexable is False:
                    evidence.append(
                        "Pagina are o directiva noindex activa — Google nu o va indexa "
                        "pana cand aceasta nu este eliminata."
                    )
                wc = _attr(url_analysis, "word_count")
                if wc and wc < 300:
                    evidence.append(
                        f"Continut slab: pagina are aproximativ {wc} cuvinte. "
                        "Paginile thin content rareori rankeaza in top 20."
                    )
            evidence += self._competition_bullets(serp_items, difficulty, top_n=3)
            return "gap", evidence

        pos = position_found

        # ── Position 1-3 → dominate ───────────────────────────────────────
        if 1 <= pos <= 3:
            evidence.append(
                f"URL-ul tau este pe pozitia {pos} pentru acest keyword. "
                "Monitorizez si mentine — esti lider."
            )
            if difficulty.has_featured_snippet:
                fs_item = _first_featured_snippet(serp_items)
                fs_domain = _attr(fs_item, "domain") or "un competitor"
                if pos != 1:
                    evidence.append(
                        f"Featured snippet castigat de {fs_domain}. "
                        "Structureaza raspunsul direct in primele 50 de cuvinte "
                        "pentru a prelua snippet-ul."
                    )
                else:
                    evidence.append(
                        "Ai si featured snippet — pozitie dominanta. "
                        "Actualizeaza regulat continutul pentru a-l mentine."
                    )
            if difficulty.dominant_domain:
                evidence.append(
                    f"Domeniu dominant in SERP: {difficulty.dominant_domain} "
                    "are mai mult de 3 rezultate — monitorizezi riscul de monopolizare."
                )
            return "dominate", evidence

        # ── Position 4-10 → optimize ─────────────────────────────────────
        if 4 <= pos <= 10:
            evidence.append(
                f"Esti pe pagina 1, pozitia {pos}. "
                "Optimizarea title, meta description si structurii interne "
                "poate aduce 2-4 pozitii in sus."
            )

            # Word-count gap vs top 3
            avg_wc = difficulty.avg_word_count_top3
            your_wc = _attr(url_analysis, "word_count") if url_analysis else None
            if avg_wc and your_wc:
                if your_wc < int(avg_wc * 0.75):
                    gap_words = avg_wc - your_wc
                    evidence.append(
                        f"Top 3 au in medie {avg_wc:,} cuvinte vs {your_wc:,} ale tale "
                        f"(diferenta: ~{gap_words:,} cuvinte). "
                        "Extinde continutul cu sectiuni H2/H3 suplimentare."
                    )
                elif your_wc > int(avg_wc * 1.3):
                    evidence.append(
                        f"Ai {your_wc:,} cuvinte — mai mult decat media top 3 ({avg_wc:,}). "
                        "Verifica daca continutul e focusat pe intent sau dispersat."
                    )
            elif avg_wc and not your_wc:
                evidence.append(
                    f"Top 3 au in medie {avg_wc:,} cuvinte estimate. "
                    "Compararea word count-ului tau necesita analiza URL completa."
                )

            # Featured snippet opportunity
            if difficulty.has_featured_snippet:
                evidence.append(
                    "Featured snippet prezent in SERP — "
                    "structureaza continutul cu H2/H3 si liste pentru a-l castiga."
                )

            # Schema gap
            your_schema = _attr(url_analysis, "has_schema") if url_analysis else None
            top3_schema_rate = _schema_rate_top_n(serp_items, n=3)
            if your_schema is False and top3_schema_rate >= 0.67:
                evidence.append(
                    "Top 3 folosesc schema markup — adauga JSON-LD relevant "
                    "(Article, FAQPage, HowTo) pentru rich snippets."
                )

            # Authority competition
            if difficulty.authority_domains_found:
                auth_str = ", ".join(difficulty.authority_domains_found[:3])
                evidence.append(
                    f"Domenii de autoritate prezente ({auth_str}). "
                    "Construieste backlinks si autoritate tematica pentru a concura."
                )

            return "optimize", evidence

        # ── Position 11-20 → compete ──────────────────────────────────────
        # pos is 11-20 at this point
        gap_to_p1 = pos - 10
        evidence.append(
            f"Esti pe pagina 2, pozitia {pos}. "
            f"Distanta fata de pagina 1: {gap_to_p1} pozitii. "
            "Necesita imbunatatiri substantiale de continut si autoritate."
        )

        avg_wc = difficulty.avg_word_count_top3
        your_wc = _attr(url_analysis, "word_count") if url_analysis else None
        if avg_wc and your_wc and your_wc < int(avg_wc * 0.6):
            evidence.append(
                f"Continut insuficient: {your_wc:,} cuvinte vs media top 3 "
                f"de {avg_wc:,}. Rescrie pagina ca resursa comprehensiva."
            )

        if difficulty.has_featured_snippet:
            evidence.append(
                "Featured snippet prezent — o reescriere focusata pe raspuns direct "
                "poate aduce un salt de 5-8 pozitii."
            )

        evidence += self._competition_bullets(serp_items, difficulty, top_n=3)
        return "compete", evidence

    # ------------------------------------------------------------------
    # Keyword branch
    # ------------------------------------------------------------------

    def _verdict_for_keyword(
        self,
        serp_items: list,
        difficulty: SerpDifficulty,
    ) -> tuple[str, list[str]]:

        evidence: list[str] = []

        # Top 3 domains
        evidence.append(
            self._top3_domains_bullet(serp_items)
        )

        # Featured snippet guidance
        if difficulty.has_featured_snippet:
            evidence.append(
                "Featured snippet prezent in SERP — "
                "castigatorul trebuie sa raspunda direct in primele 50 de cuvinte, "
                "urmat de detalii structurate cu H2/H3 si liste."
            )

        # Content type briefing
        ctype = difficulty.dominant_content_type
        ctype_map = {
            "informational":  "Articole informationale domina top 10 — "
                              "creeaza o resursa de tip ghid/tutorial comprehensiv.",
            "commercial":     "Pagini comerciale/produs domina top 10 — "
                              "creeaza o pagina de produs sau comparatie detaliata.",
            "transactional":  "Pagini tranzactionale domina (shop, buy, pret) — "
                              "creeaza o pagina optimizata pentru conversie cu pret si CTA clar.",
            "navigational":   "SERP dominat de branduri cunoscute — "
                              "diferentiaza-te cu continut unic si autoritate tematica.",
            "mixed":          "Mix de tipuri de continut in top 10 — "
                              "alege unghiul (informational sau comercial) cel mai potrivit intentiei.",
        }
        evidence.append(
            f"Continut dominant in top 10: {ctype}. "
            + ctype_map.get(ctype, "Analizeaza competitorii pentru a determina unghiul optim.")
        )

        # Authority warning
        if difficulty.authority_domains_found:
            auth_str = ", ".join(difficulty.authority_domains_found[:4])
            evidence.append(
                f"Domenii de autoritate in top 20: {auth_str}. "
                "Keyword cu competitivitate ridicata — construieste autoritate tematica "
                "inainte de a tinti top 3."
            )

        # Domain domination
        if difficulty.dominant_domain:
            evidence.append(
                f"{difficulty.dominant_domain} are mai mult de 3 rezultate in top 20. "
                "SERP monopolizat — diferentiaza-te cu un unghi unic."
            )

        # Local pack note
        if difficulty.local_pack_present:
            evidence.append(
                "Local pack prezent in rezultate — keyword cu intentie locala. "
                "Optimizeaza Google Business Profile daca ai prezenta fizica."
            )

        return "create", evidence

    # ------------------------------------------------------------------
    # SERP difficulty analysis
    # ------------------------------------------------------------------

    def analyze_serp_difficulty(self, serp_items: list) -> SerpDifficulty:
        """
        Derive structural signals from the SERP item list.

        Returns a SerpDifficulty dataclass with:
          - difficulty_signal       : 'high' | 'medium' | 'low'
          - dominant_content_type  : 'informational' | 'commercial' |
                                     'transactional' | 'navigational' | 'mixed'
          - has_featured_snippet   : bool
          - local_pack_present     : bool
          - authority_domains_found: list[str]
          - dominant_domain        : str | None  (>3 results from same domain)
          - avg_word_count_top3    : int | None
        """
        d = SerpDifficulty()
        if not serp_items:
            return d

        # ── Special result flags ──────────────────────────────────────────
        for item in serp_items:
            if _attr(item, "is_featured_snippet"):
                d.has_featured_snippet = True
            if _attr(item, "is_local_pack"):
                d.local_pack_present = True

        # ── Authority domain detection ────────────────────────────────────
        for item in serp_items:
            domain = (_attr(item, "domain") or "").lower()
            for auth in _AUTHORITY_DOMAINS:
                if domain == auth or domain.endswith("." + auth):
                    if auth not in d.authority_domains_found:
                        d.authority_domains_found.append(auth)
                    break

        # ── Domain domination (>3 results from same domain) ───────────────
        domain_counts: dict[str, int] = {}
        for item in serp_items:
            dom = (_attr(item, "domain") or "").lower()
            if dom:
                domain_counts[dom] = domain_counts.get(dom, 0) + 1
        for dom, cnt in domain_counts.items():
            if cnt > 3:
                d.dominant_domain = dom
                break

        # ── Content type detection ────────────────────────────────────────
        d.dominant_content_type = self._detect_content_type(serp_items)

        # ── Word count average for top 3 ──────────────────────────────────
        # "Top 3" means the first 3 items by list order, not literal
        # position in (1, 2, 3): SERP feature blocks (AI Overview, People
        # Also Ask, etc.) commonly occupy those exact position numbers
        # without being ranked pages, which would otherwise starve this of
        # real data on a large fraction of real-world queries.
        top3_wcs = [
            _attr(item, "word_count_estimated")
            for item in serp_items[:3]
            if _attr(item, "word_count_estimated")
        ]
        if top3_wcs:
            d.avg_word_count_top3 = int(sum(top3_wcs) / len(top3_wcs))

        # ── Difficulty signal ─────────────────────────────────────────────
        authority_count = len(d.authority_domains_found)
        if authority_count >= 3 or (authority_count >= 1 and d.dominant_domain):
            d.difficulty_signal = "high"
        elif authority_count >= 1 or d.has_featured_snippet:
            d.difficulty_signal = "medium"
        else:
            d.difficulty_signal = "low"

        return d

    # ------------------------------------------------------------------
    # SERP narrative summary
    # ------------------------------------------------------------------

    def generate_serp_summary(self, serp_items: list, difficulty: SerpDifficulty) -> str:
        """
        Return a 2-3 sentence plain-text description of the SERP landscape.

        Example output:
            "SERP-ul e dominat de articole informationale. Featured snippet prezent.
             Wikipedia si un forum apar in top 5 — keyword cu intentie informationale pura."
        """
        parts: list[str] = []

        # Content type sentence
        ctype = difficulty.dominant_content_type
        ctype_phrases = {
            "informational":  "SERP-ul este dominat de articole informationale si ghiduri.",
            "commercial":     "SERP-ul este dominat de pagini comerciale si de comparatie.",
            "transactional":  "SERP-ul este dominat de pagini tranzactionale (shop, pret, cumpar).",
            "navigational":   "SERP-ul este dominat de branduri si pagini de brand.",
            "mixed":          "SERP-ul are un mix de tipuri de continut (informational si comercial).",
        }
        parts.append(ctype_phrases.get(ctype, "Mix de tipuri de continut in SERP."))

        # Featured snippet
        if difficulty.has_featured_snippet:
            fs_item = _first_featured_snippet(serp_items)
            fs_domain = _attr(fs_item, "domain") if fs_item else None
            if fs_domain:
                parts.append(f"Featured snippet prezent, castigat de {fs_domain}.")
            else:
                parts.append("Featured snippet prezent in SERP.")

        # Local pack
        if difficulty.local_pack_present:
            parts.append("Local pack prezent — keyword cu intentie geografica locala.")

        # Authority / competition signal
        if difficulty.authority_domains_found:
            auth_list = difficulty.authority_domains_found[:3]
            plural = "apar" if len(auth_list) > 1 else "apare"
            auth_str = " si ".join(auth_list) if len(auth_list) <= 2 else (
                ", ".join(auth_list[:-1]) + f" si {auth_list[-1]}"
            )
            intent_hint = (
                "keyword cu intentie informationale pura"
                if ctype == "informational"
                else "keyword competitiv"
            )
            parts.append(
                f"{auth_str} {plural} in top 20 — {intent_hint}."
            )
        elif difficulty.dominant_domain:
            parts.append(
                f"{difficulty.dominant_domain} domina SERP-ul cu mai mult de 3 rezultate."
            )

        # Difficulty wrap-up
        diff_map = {
            "high":   "Competitivitate ridicata — necesita strategie pe termen lung.",
            "medium": "Competitivitate medie — rezultate posibile in 3-6 luni.",
            "low":    "Competitivitate scazuta — oportunitate buna de rankat rapid.",
        }
        diff_sentence = diff_map.get(difficulty.difficulty_signal, "")
        if diff_sentence:
            parts.append(diff_sentence)

        return " ".join(parts)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_content_type(serp_items: list) -> str:
        """
        Classify dominant intent by inspecting schema types, titles, and URLs.

        Priority order: transactional → commercial → informational → navigational → mixed.
        """
        scores = {"informational": 0, "commercial": 0, "transactional": 0, "navigational": 0}

        for item in serp_items:
            schema_types: list[str] = _attr(item, "schema_types") or []
            title:   str = (_attr(item, "title")   or "").lower()
            url:     str = (_attr(item, "url")      or "").lower()
            domain:  str = (_attr(item, "domain")   or "").lower()

            # Schema-based classification
            for st in schema_types:
                if st in _PRODUCT_SCHEMA:
                    scores["commercial"] += 2
                    scores["transactional"] += 1
                elif st in _ARTICLE_SCHEMA:
                    scores["informational"] += 2
                elif st in _TOOL_SCHEMA:
                    scores["commercial"] += 1

            # URL pattern signals
            if re.search(r"/(shop|buy|order|cos|cart|checkout|pret|price)", url):
                scores["transactional"] += 2
            if re.search(r"/(product|produse|produs|categori)", url):
                scores["commercial"] += 1
            if re.search(r"/(blog|ghid|guide|tutorial|article|articol|stiri|news|wiki)", url):
                scores["informational"] += 1

            # Title signals
            transact_words = ("cumpara", "buy", "pret", "price", "oferta", "discount",
                              "comanda", "order", "promo", "promotie")
            commercial_words = ("cel mai bun", "best", "top", "comparatie", "compare",
                                "review", "recenzie", "vs")
            info_words = ("ce este", "ce inseamna", "cum", "ghid", "tutorial",
                          "what is", "how to", "guide", "explained", "meaning")

            if any(w in title for w in transact_words):
                scores["transactional"] += 2
            if any(w in title for w in commercial_words):
                scores["commercial"] += 1
            if any(w in title for w in info_words):
                scores["informational"] += 2

            # Navigational: brand-heavy domains in top results
            if domain in _AUTHORITY_DOMAINS or re.search(r"\.(gov|edu|org)\.", domain):
                scores["navigational"] += 1

        # Pick winner; fall back to 'mixed' if tied at top
        max_score = max(scores.values())
        if max_score == 0:
            return "mixed"

        winners = [k for k, v in scores.items() if v == max_score]
        if len(winners) == 1:
            return winners[0]

        # Tie-break: transactional > commercial > informational > navigational
        for preferred in ("transactional", "commercial", "informational", "navigational"):
            if preferred in winners:
                return preferred

        return "mixed"

    @staticmethod
    def _top3_domains_bullet(serp_items: list) -> str:
        """Build 'Top 3 rezultate: domain1 (pos1), …' bullet.

        "Top 3" is the first 3 items by list order, not literal position in
        (1, 2, 3) -- an AI Overview or People Also Ask block commonly sits
        at one of those exact position numbers without being a ranked page.
        """
        top3 = serp_items[:3]

        if not top3:
            return "Nu exista rezultate organice in top 3."

        frags = []
        for item in top3:
            dom = _attr(item, "domain") or _attr(item, "url") or "?"
            pos = _attr(item, "position") or "?"
            frags.append(f"{dom} (pos {pos})")

        return "Top 3 rezultate: " + ", ".join(frags) + "."

    @staticmethod
    def _competition_bullets(
        serp_items: list,
        difficulty: SerpDifficulty,
        top_n: int = 3,
    ) -> list[str]:
        """Shared helper for competition context bullets."""
        bullets: list[str] = []

        # Top N domains
        top_items = sorted(
            [i for i in serp_items if (_attr(i, "position") or 99) <= top_n],
            key=lambda i: _attr(i, "position") or 99,
        )
        if top_items:
            frags = []
            for item in top_items:
                dom = _attr(item, "domain") or "?"
                pos = _attr(item, "position") or "?"
                frags.append(f"{dom} (pos {pos})")
            bullets.append("Top " + str(len(frags)) + " rezultate: " + ", ".join(frags) + ".")

        if difficulty.authority_domains_found:
            auth_str = ", ".join(difficulty.authority_domains_found[:3])
            bullets.append(
                f"Domenii de autoritate prezente: {auth_str}. "
                "Keyword cu competitivitate ridicata."
            )

        return bullets


# ---------------------------------------------------------------------------
# Module-level utility functions
# ---------------------------------------------------------------------------

def _attr(obj, name: str):
    """
    Unified attribute accessor for both dataclasses/Pydantic models and dicts.
    Returns None silently if the attribute/key is absent.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _first_featured_snippet(serp_items: list):
    """Return the first SERPItem with is_featured_snippet=True, or None."""
    for item in serp_items:
        if _attr(item, "is_featured_snippet"):
            return item
    return None


def _schema_rate_top_n(serp_items: list, n: int = 3) -> float:
    """
    Fraction of the top-N ranked items (by list order) that have schema
    markup (0.0-1.0). Uses list order rather than literal position <= n,
    since SERP feature blocks (AI Overview, People Also Ask) can occupy
    those position numbers without being ranked pages.
    """
    top = serp_items[:n]
    if not top:
        return 0.0
    return sum(1 for i in top if _attr(i, "has_schema")) / len(top)
