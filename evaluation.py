"""evaluation.py

Heurísticas simples de avaliação de cobertura e evidências.
"""

from __future__ import annotations

from typing import Dict, List, Tuple
import re
import json
import os
from urllib.parse import urlparse

EQUITY_SECTIONS = ["financials", "valuation", "catalysts", "risks", "recent_news"]
STARTUP_SECTIONS = ["team", "market", "traction", "competitors", "red_flags"]

SECTION_KEYWORDS = {
    "financials": ["receita", "lucro", "ebitda", "margem", "guidance", "results", "earnings"],
    "valuation": ["valuation", "múltipl", "p/l", "ev/ebitda", "target price", "preço-alvo", "fair value", "dcf"],
    "catalysts": ["catalisador", "lançamento", "novidade", "acordo", "parceria", "guidance"],
    "risks": ["risco", "regulação", "alavancagem", "processo", "queda", "competition"],
    "recent_news": ["notícias", "recent", "latest", "today", "hoje", "2024", "2025", "2026"],
    "team": ["founder", "founders", "equipe", "ceo", "cto", "linkedin"],
    "market": ["mercado", "tam", "sam", "som", "market size", "segment"],
    "traction": ["traction", "clientes", "reviews", "case study", "growth", "receita"],
    "competitors": ["competitors", "concorrentes", "alternatives", "rivals"],
    "red_flags": ["lawsuit", "processo", "layoff", "risco", "red flag", "outage"],
}

SECTIONS_REQUIRING_NUMBERS = {"financials", "valuation", "market", "traction"}

PRIMARY_HINTS = [
    "sec.gov",
    "investor",
    "/ir",
    "relations",
    "press",  # press releases
    "annual report",
    "10-k",
    "10q",
    "form 20-f",
]

MARKETING_HINTS = ["blog", "templates", "pricing", "product", "landing", "marketing", "about", "careers"]


_CONFIG_CACHE: Dict = {}


def _config_dir() -> str:
    return os.getenv("CORTIQ_CONFIG_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "config")))


def _load_json(name: str, default: Dict) -> Dict:
    path = os.path.join(_config_dir(), name)
    if path in _CONFIG_CACHE:
        return _CONFIG_CACHE[path]
    if not os.path.exists(path):
        _CONFIG_CACHE[path] = default
        return default
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    _CONFIG_CACHE[path] = data
    return data


def _normalize_text(results: List[dict]) -> str:
    parts: List[str] = []
    for block in results:
        if not block:
            continue
        answer = (block or {}).get("answer") or ""
        parts.append(answer)
        for item in (block or {}).get("results", []):
            title = (item or {}).get("title") or ""
            snippet = (item or {}).get("content") or ""
            url = (item or {}).get("url") or ""
            parts.append(" ".join([title, snippet, url]))
    return " ".join(parts).lower()


def _extract_urls(results: List[dict]) -> List[str]:
    urls: List[str] = []
    for block in results:
        for item in (block or {}).get("results", []):
            url = (item or {}).get("url") or ""
            if url:
                urls.append(url)
    return urls


def _is_primary_source(url: str) -> bool:
    lower = url.lower()
    if any(h in lower for h in PRIMARY_HINTS):
        return True
    domain = urlparse(lower).netloc
    if domain.endswith(".gov") or domain.endswith(".edu"):
        return True
    return False


def _is_marketing_source(url: str, company_domain: str | None) -> bool:
    lower = url.lower()
    domain = urlparse(lower).netloc
    if company_domain and company_domain in domain:
        return True
    if any(h in lower for h in MARKETING_HINTS):
        return True
    return False


def _has_numbers(text: str) -> bool:
    return bool(re.search(r"\b\d+[\d.,]*\b", text))


def _section_evidence(
    sec: str,
    items: List[Dict],
    company_domain: str | None,
    require_primary_for_quant: bool = False,
) -> Tuple[bool, bool]:
    """Retorna (strong, weak)."""
    keywords = SECTION_KEYWORDS.get(sec, [])
    matched = []
    for it in items:
        text = (it.get("text") or "").lower()
        if any(k in text for k in keywords):
            matched.append(it)

    if not matched:
        return (False, False)

    # marketing-only -> weak
    if all(_is_marketing_source(it.get("url", ""), company_domain) for it in matched):
        return (False, True)

    # require numbers for quantitative sections AND non-marketing evidence
    if sec in SECTIONS_REQUIRING_NUMBERS:
        strong_candidates = [
            it
            for it in matched
            if _has_numbers(it.get("text", "")) and not _is_marketing_source(it.get("url", ""), company_domain)
        ]
        if require_primary_for_quant:
            strong_candidates = [it for it in strong_candidates if _is_primary_source(it.get("url", ""))]
        if strong_candidates:
            return (True, False)
        return (False, True)

    # for non-quant sections, require at least one non-marketing source
    if any(not _is_marketing_source(it.get("url", ""), company_domain) for it in matched):
        return (True, False)

    return (False, True)


def evaluate_results(mode: str, results: List[dict], company_domain: str | None = None) -> Dict:
    sections = EQUITY_SECTIONS if mode == "stock" else STARTUP_SECTIONS

    covered: List[str] = []
    weakly_covered: List[str] = []
    missing: List[str] = []

    items: List[Dict] = []
    for item in results:
        if not item:
            continue
        # Support both flat list (from researcher.py) and nested block format
        if "results" in item:
            for sub in (item.get("results") or []):
                text = " ".join([(sub or {}).get("title") or "", (sub or {}).get("content") or ""])
                items.append({"url": (sub or {}).get("url") or "", "text": text})
        else:
            text = " ".join([(item or {}).get("title") or "", (item or {}).get("content") or ""])
            items.append({"url": (item or {}).get("url") or "", "text": text})

    urls = [it["url"] for it in items if it.get("url")]
    unique_urls = list(dict.fromkeys(urls))
    unique_count = len(unique_urls)
    primary_count = sum(1 for u in unique_urls if _is_primary_source(u))
    primary_ratio = round(primary_count / max(1, unique_count), 2)

    eval_rules = _load_json("evaluation_rules.json", {})
    require_primary_for_quant = primary_ratio < eval_rules.get("primary_min_for_quant", 0.12)

    for sec in sections:
        strong, weak = _section_evidence(sec, items, company_domain, require_primary_for_quant=require_primary_for_quant)
        if strong:
            covered.append(sec)
        elif weak:
            weakly_covered.append(sec)
        else:
            missing.append(sec)

    eval_rules = _load_json("evaluation_rules.json", {
        "weak_coverage_weight": 0.4,
        "primary_ratio_hard_cap_1": 0.05,
        "primary_ratio_hard_cap_2": 0.10,
        "evidence_cap_1": 0.5,
        "evidence_cap_2": 0.6,
        "primary_min_for_quant": 0.12,
        "marketing_penalty_threshold": 0.6,
        "marketing_penalty": 0.15,
    })

    # Penalize if only marketing sources
    if unique_urls:
        marketing_count = sum(1 for u in unique_urls if _is_marketing_source(u, company_domain))
        marketing_ratio = marketing_count / max(1, unique_count)
    else:
        marketing_ratio = 1.0

    coverage_score = round((len(covered) + eval_rules.get("weak_coverage_weight", 0.4) * len(weakly_covered)) / max(1, len(sections)), 2)

    recency_text = " ".join(it.get("text", "") for it in items).lower()
    recency_hits = re.findall(r"\b(2024|2025|2026)\b", recency_text)
    recency_score = 1.0 if recency_hits else 0.4

    source_score = min(1.0, unique_count / 12)
    evidence_score = round(
        min(1.0, 0.35 * coverage_score + 0.15 * source_score + 0.5 * primary_ratio),
        2,
    )

    # Teto por baixa fonte primária
    if primary_ratio < eval_rules.get("primary_ratio_hard_cap_1", 0.05):
        evidence_score = min(evidence_score, eval_rules.get("evidence_cap_1", 0.5))
    elif primary_ratio < eval_rules.get("primary_ratio_hard_cap_2", 0.1):
        evidence_score = min(evidence_score, eval_rules.get("evidence_cap_2", 0.6))

    # Penalizar se predominância de marketing
    if marketing_ratio > eval_rules.get("marketing_penalty_threshold", 0.6):
        evidence_score = round(max(0.0, evidence_score - eval_rules.get("marketing_penalty", 0.15)), 2)

    primary_backed_sections = covered if primary_ratio >= eval_rules.get("primary_min_for_quant", 0.12) else []

    return {
        "coverage_score": coverage_score,
        "evidence_score": evidence_score,
        "primary_source_ratio": primary_ratio,
        "covered_sections": covered,
        "weakly_covered_sections": weakly_covered,
        "missing_sections": missing,
        "primary_backed_sections": primary_backed_sections,
        "source_count": unique_count,
        "recency_score": recency_score,
    }


def build_followup_queries(mode: str, subject: str, missing_sections: List[str]) -> List[str]:
    strategy = _load_json("query_strategy.json", {"max_queries": 3})
    priority = strategy.get("priority_stock", []) if mode == "stock" else strategy.get("priority_startup", [])
    ordered = [s for s in priority if s in missing_sections] + [s for s in missing_sections if s not in priority]
    queries: List[str] = []
    for sec in ordered:
        if mode == "stock":
            if sec == "financials":
                queries.append(f"{subject} resultados financeiros receita lucro ebitda 2024 2025")
            elif sec == "valuation":
                queries.append(f"{subject} valuation múltiplos p/l ev/ebitda preço-alvo")
            elif sec == "catalysts":
                queries.append(f"{subject} catalisadores próximos 90 dias guidance eventos")
            elif sec == "risks":
                queries.append(f"{subject} riscos principais alavancagem regulação concorrência")
            elif sec == "recent_news":
                queries.append(f"{subject} notícias recentes últimos 30 dias")
        else:
            if sec == "team":
                queries.append(f"{subject} founders equipe liderança linkedin")
            elif sec == "market":
                queries.append(f"{subject} mercado TAM SAM SOM concorrentes")
            elif sec == "traction":
                queries.append(f"{subject} traction clientes reviews case study")
            elif sec == "competitors":
                queries.append(f"{subject} concorrentes alternatives market")
            elif sec == "red_flags":
                queries.append(f"{subject} riscos processos layoffs red flags")
    return queries[:strategy.get("max_queries", 3)]
