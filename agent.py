"""agent.py — Cortiq Decision Copilot v2
Orchestrates multi-step research with parallel execution, gap detection, and follow-up queries.
"""
import asyncio
import json
import os
import time
import re
from datetime import datetime
from typing import AsyncGenerator, Tuple, List, Dict

from researcher import search_topic, deduplicate_results
from reporter import stream_equity_report, stream_startup_report, generate_critic_notes
from equity_data import get_equity_data, format_market_data
from evaluation import evaluate_results, build_followup_queries
from artifact import save_analysis_artifact


def build_equity_queries(ticker: str, thesis: str) -> list[str]:
    return [
        f"{ticker} resultado financeiro receita lucro EBITDA 2025 2026",
        f"{ticker} valuation múltiplos P/L EV/EBITDA target price analistas",
        f"{ticker} catalisadores crescimento perspectivas setor mercado",
        f"{ticker} riscos ameaças regulatório competição headwinds",
        f"{ticker} notícias recentes eventos corporativos dividendos 2025",
        f"{thesis or ticker} tese investimento análise fundamentalista",
    ]


def build_startup_queries(name: str, url: str, thesis: str) -> list[str]:
    return [
        f"{name} founders CEO CTO fundadores experiência background exits anteriores",
        f"{name} startup rodada investimento funding captação valuação série seed",
        f"{name} mercado endereçável TAM crescimento setor tendência",
        f"{name} concorrentes competidores alternativas comparação market share",
        f"{name} tração clientes receita ARR crescimento produto métricas",
        f"{name} notícias lançamentos parcerias expansão 2025 2026",
        f"{name} problemas críticas desafios riscos {thesis or ''}".strip(),
    ]


async def _search_async(query: str) -> List[Dict]:
    """Run search_topic in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, search_topic, query)


async def _run_queries_parallel(queries: List[str]) -> List[Dict]:
    """Execute all queries concurrently and flatten results."""
    results_nested = await asyncio.gather(*[_search_async(q) for q in queries])
    return [r for results in results_nested for r in results]


async def _collect_from_generator(gen: AsyncGenerator[Tuple[str, str], None]) -> Dict:
    data = {
        "queries": [],
        "followup_queries": [],
        "evaluation": {},
        "critic_notes": "",
        "sources": [],
        "artifact_path": "",
    }
    async for event, payload in gen:
        if event == "queries":
            try:
                data["queries"] = json.loads(payload)
            except Exception:
                pass
        elif event == "followup_queries":
            try:
                data["followup_queries"].extend(json.loads(payload))
            except Exception:
                pass
        elif event == "evaluation":
            try:
                data["evaluation"] = json.loads(payload)
            except Exception:
                pass
        elif event == "critic":
            data["critic_notes"] = payload
        elif event == "sources":
            try:
                data["sources"] = json.loads(payload)
            except Exception:
                pass
        elif event == "artifact":
            data["artifact_path"] = payload
    return data


def run_equity_pipeline_sync(ticker: str, thesis: str = "", mandate: str = "") -> Dict:
    return asyncio.run(_collect_from_generator(run_equity_analysis(ticker, thesis, mandate)))


def run_startup_pipeline_sync(name: str, url: str = "", thesis: str = "") -> Dict:
    return asyncio.run(_collect_from_generator(run_startup_analysis(name, url, thesis)))


async def _gap_check(topic: str, mode: str, results: List[Dict]) -> List[str]:
    """Use Claude Haiku to identify research gaps and return follow-up queries."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return []

    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic(api_key=api_key)

        found = "\n".join(
            f"- {r['title']}: {r['content'][:80]}"
            for r in results[:12]
        )

        if mode == "equity":
            gaps_to_check = "financials, valuation, catalysts, risks, management changes"
        else:
            gaps_to_check = "team experience, funding/traction, market size, competitors, red flags"

        prompt = (
            f"Research topic: {topic} ({mode} analysis).\n"
            f"Coverage needed: {gaps_to_check}\n"
            f"Evidence found so far:\n{found}\n\n"
            f"Identify 2 specific research gaps and generate targeted follow-up queries in Portuguese.\n"
            f"Respond ONLY with a JSON array of 2 strings: [\"query1\", \"query2\"]"
        )

        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        match = re.search(r'\[.*?\]', text, re.DOTALL)
        if match:
            queries = json.loads(match.group())
            if isinstance(queries, list):
                return [q for q in queries if isinstance(q, str)][:2]
    except Exception:
        pass
    return []


def _save_artifact(data: dict) -> str:
    """Save analysis artifact as JSON."""
    try:
        return save_analysis_artifact(data, base_dir=os.path.join(os.path.dirname(__file__), "artifacts"))
    except Exception:
        return ""


def _load_json(name: str, default: dict) -> dict:
    base = os.getenv("CORTIQ_CONFIG_DIR", os.path.join(os.path.dirname(__file__), "config"))
    path = os.path.join(base, name)
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


async def run_equity_analysis(
    ticker: str,
    thesis: str = "",
    mandate: str = "",
    prev_verdict: str = "",
    prev_date: str = "",
) -> AsyncGenerator[Tuple[str, str], None]:
    yield "status", f"Iniciando análise de {ticker}..."

    # Fetch real market data alongside initial web queries
    queries = build_equity_queries(ticker, thesis)
    yield "queries", json.dumps(queries, ensure_ascii=False)

    yield "status", f"Coletando dados de mercado e pesquisando {len(queries)} fontes..."
    market_data, all_results = await asyncio.gather(
        get_equity_data(ticker),
        _run_queries_parallel(queries),
    )

    if market_data:
        yield "market_data", json.dumps(market_data, ensure_ascii=False)

    yield "status", f"{len(all_results)} resultados coletados. Verificando lacunas..."

    # Gap detection
    followup = await _gap_check(ticker, "equity", all_results)
    if followup:
        yield "followup_queries", json.dumps(followup, ensure_ascii=False)
        yield "status", f"Aprofundando {len(followup)} lacunas identificadas..."
        extra = await _run_queries_parallel(followup)
        all_results.extend(extra)

    all_results = deduplicate_results(all_results)

    evaluation = evaluate_results("stock", all_results)
    yield "evaluation", json.dumps(evaluation, ensure_ascii=False)

    retry_rules = _load_json("retry_rules.json", {
        "coverage_threshold": 0.75,
        "primary_threshold": 0.12,
        "retry_on_weak_sections": True,
        "max_followups": 1,
    })

    should_follow_up = (
        evaluation.get("coverage_score", 0) < retry_rules.get("coverage_threshold", 0.75)
        or (retry_rules.get("retry_on_weak_sections", True) and evaluation.get("weakly_covered_sections"))
        or evaluation.get("primary_source_ratio", 0) < retry_rules.get("primary_threshold", 0.12)
    )

    eval_followups = []
    if should_follow_up and retry_rules.get("max_followups", 1) > 0:
        gap_sections = (evaluation.get("missing_sections") or []) + (evaluation.get("weakly_covered_sections") or [])
        if not gap_sections and evaluation.get("primary_source_ratio", 0) < retry_rules.get("primary_threshold", 0.12):
            gap_sections = ["financials", "valuation", "recent_news"]
        eval_followups = build_followup_queries("stock", ticker, gap_sections)
        if eval_followups:
            yield "followup_queries", json.dumps(eval_followups, ensure_ascii=False)
            yield "status", f"Aprofundando {len(eval_followups)} lacunas identificadas..."
            extra = await _run_queries_parallel(eval_followups)
            all_results.extend(extra)
            all_results = deduplicate_results(all_results)
            evaluation = evaluate_results("stock", all_results)
            yield "evaluation", json.dumps(evaluation, ensure_ascii=False)

    sources = [
        {"title": r["title"], "url": r["url"], "source_type": r.get("source_type", "web"), "content": r.get("content", "")}
        for r in all_results[:20] if r.get("url")
    ]
    yield "sources", json.dumps(sources, ensure_ascii=False)

    yield "status", "Sintetizando análise com Claude..."

    report_chunks = []
    async for chunk in stream_equity_report(all_results, ticker, thesis, mandate, prev_verdict, prev_date, market_data):
        report_chunks.append(chunk)
        yield "chunk", chunk

    full_report = "".join(report_chunks)

    verdict_match = re.search(
        r'\*\*(TESE MANTIDA|TESE ALTERADA|TESE INVALIDADA|COMPRAR|MANTER|REDUZIR|VENDER)\*\*',
        full_report
    )
    confidence_match = re.search(r'Confiança:\s*\*?\*?([A-ZÁÉÍÓÚÃÕ]+)\*?\*?', full_report)

    evidence_text = "\n".join([f"- {s['title']} | {s['url']}" for s in sources])
    critic_notes = await generate_critic_notes("stock", full_report, evidence_text, evaluation)
    yield "critic", critic_notes

    artifact_path = _save_artifact({
        "mode": "equity",
        "key": ticker,
        "thesis": thesis,
        "mandate": mandate,
        "market_data": market_data or {},
        "queries": queries,
        "followup_queries": (followup or []) + (eval_followups or []),
        "sources": sources,
        "evaluation": evaluation,
        "critic_notes": critic_notes,
        "verdict": verdict_match.group(1) if verdict_match else "",
        "confidence": confidence_match.group(1) if confidence_match else "",
        "report": full_report,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })
    if artifact_path:
        yield "artifact", artifact_path

    yield "done", "Análise concluída"


async def run_startup_analysis(
    name: str,
    url: str = "",
    thesis: str = "",
    prev_verdict: str = "",
    prev_date: str = "",
) -> AsyncGenerator[Tuple[str, str], None]:
    yield "status", f"Iniciando due diligence: {name}..."

    queries = build_startup_queries(name, url, thesis)
    yield "queries", json.dumps(queries, ensure_ascii=False)

    yield "status", f"Pesquisando {len(queries)} fontes em paralelo..."
    all_results = await _run_queries_parallel(queries)
    yield "status", f"{len(all_results)} resultados coletados. Verificando lacunas..."

    # Gap detection
    followup = await _gap_check(name, "startup", all_results)
    if followup:
        yield "followup_queries", json.dumps(followup, ensure_ascii=False)
        yield "status", f"Aprofundando {len(followup)} lacunas identificadas..."
        extra = await _run_queries_parallel(followup)
        all_results.extend(extra)

    all_results = deduplicate_results(all_results)

    evaluation = evaluate_results("startup", all_results, company_domain=url)
    yield "evaluation", json.dumps(evaluation, ensure_ascii=False)

    retry_rules = _load_json("retry_rules.json", {
        "coverage_threshold": 0.75,
        "primary_threshold": 0.12,
        "retry_on_weak_sections": True,
        "max_followups": 1,
    })

    should_follow_up = (
        evaluation.get("coverage_score", 0) < retry_rules.get("coverage_threshold", 0.75)
        or (retry_rules.get("retry_on_weak_sections", True) and evaluation.get("weakly_covered_sections"))
        or evaluation.get("primary_source_ratio", 0) < retry_rules.get("primary_threshold", 0.12)
    )

    eval_followups = []
    if should_follow_up and retry_rules.get("max_followups", 1) > 0:
        gap_sections = (evaluation.get("missing_sections") or []) + (evaluation.get("weakly_covered_sections") or [])
        if not gap_sections and evaluation.get("primary_source_ratio", 0) < retry_rules.get("primary_threshold", 0.12):
            gap_sections = ["market", "traction", "team"]
        eval_followups = build_followup_queries("startup", name, gap_sections)
        if eval_followups:
            yield "followup_queries", json.dumps(eval_followups, ensure_ascii=False)
            yield "status", f"Aprofundando {len(eval_followups)} lacunas identificadas..."
            extra = await _run_queries_parallel(eval_followups)
            all_results.extend(extra)
            all_results = deduplicate_results(all_results)
            evaluation = evaluate_results("startup", all_results, company_domain=url)
            yield "evaluation", json.dumps(evaluation, ensure_ascii=False)

    sources = [
        {"title": r["title"], "url": r["url"], "source_type": r.get("source_type", "web"), "content": r.get("content", "")}
        for r in all_results[:20] if r.get("url")
    ]
    yield "sources", json.dumps(sources, ensure_ascii=False)

    yield "status", "Gerando VC memo com Claude..."

    report_chunks = []
    async for chunk in stream_startup_report(all_results, name, url, thesis, prev_verdict, prev_date):
        report_chunks.append(chunk)
        yield "chunk", chunk

    full_report = "".join(report_chunks)

    verdict_match = re.search(r'\*\*(INVESTIR|MONITORAR|PASSAR)\*\*', full_report)
    confidence_match = re.search(r'Confiança:\s*\*?\*?([A-ZÁÉÍÓÚÃÕ]+)\*?\*?', full_report)

    evidence_text = "\n".join([f"- {s['title']} | {s['url']}" for s in sources])
    critic_notes = await generate_critic_notes("startup", full_report, evidence_text, evaluation)
    yield "critic", critic_notes

    artifact_path = _save_artifact({
        "mode": "startup",
        "key": name,
        "url": url,
        "thesis": thesis,
        "queries": queries,
        "followup_queries": (followup or []) + (eval_followups or []),
        "sources": sources,
        "evaluation": evaluation,
        "critic_notes": critic_notes,
        "verdict": verdict_match.group(1) if verdict_match else "",
        "confidence": confidence_match.group(1) if confidence_match else "",
        "report": full_report,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })
    if artifact_path:
        yield "artifact", artifact_path

    yield "done", "Due diligence concluída"
