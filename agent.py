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
from reporter import stream_equity_report, stream_startup_report


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


def _save_artifact(data: dict) -> None:
    """Save analysis artifact as JSON."""
    try:
        mode = data.get("mode", "unknown")
        key = re.sub(r'[^a-zA-Z0-9_-]', '_', data.get("key", "unknown"))[:30]
        ts = int(time.time())
        dir_path = os.path.join("artifacts", f"{mode}_{key}_{ts}")
        os.makedirs(dir_path, exist_ok=True)
        with open(os.path.join(dir_path, "analysis.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


async def run_equity_analysis(
    ticker: str,
    thesis: str = "",
    mandate: str = "",
    prev_verdict: str = "",
    prev_date: str = "",
) -> AsyncGenerator[Tuple[str, str], None]:
    yield "status", f"Iniciando análise de {ticker}..."

    queries = build_equity_queries(ticker, thesis)
    yield "queries", json.dumps(queries, ensure_ascii=False)

    yield "status", f"Pesquisando {len(queries)} fontes em paralelo..."
    all_results = await _run_queries_parallel(queries)
    yield "status", f"{len(all_results)} resultados coletados. Verificando lacunas..."

    # Gap detection
    followup = await _gap_check(ticker, "equity", all_results)
    if followup:
        yield "followup_queries", json.dumps(followup, ensure_ascii=False)
        yield "status", f"Aprofundando {len(followup)} lacunas identificadas..."
        extra = await _run_queries_parallel(followup)
        all_results.extend(extra)

    all_results = deduplicate_results(all_results)

    sources = [
        {"title": r["title"], "url": r["url"], "source_type": r.get("source_type", "web")}
        for r in all_results[:20] if r.get("url")
    ]
    yield "sources", json.dumps(sources, ensure_ascii=False)

    yield "status", "Sintetizando análise com Claude..."

    report_chunks = []
    async for chunk in stream_equity_report(all_results, ticker, thesis, mandate, prev_verdict, prev_date):
        report_chunks.append(chunk)
        yield "chunk", chunk

    full_report = "".join(report_chunks)

    verdict_match = re.search(
        r'\*\*(TESE MANTIDA|TESE ALTERADA|TESE INVALIDADA|COMPRAR|MANTER|REDUZIR|VENDER)\*\*',
        full_report
    )
    confidence_match = re.search(r'Confiança:\s*\*?\*?([A-ZÁÉÍÓÚÃÕ]+)\*?\*?', full_report)

    _save_artifact({
        "mode": "equity",
        "key": ticker,
        "thesis": thesis,
        "mandate": mandate,
        "queries": queries,
        "followup_queries": followup,
        "sources": sources,
        "verdict": verdict_match.group(1) if verdict_match else "",
        "confidence": confidence_match.group(1) if confidence_match else "",
        "report": full_report,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })

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

    sources = [
        {"title": r["title"], "url": r["url"], "source_type": r.get("source_type", "web")}
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

    _save_artifact({
        "mode": "startup",
        "key": name,
        "url": url,
        "thesis": thesis,
        "queries": queries,
        "followup_queries": followup,
        "sources": sources,
        "verdict": verdict_match.group(1) if verdict_match else "",
        "confidence": confidence_match.group(1) if confidence_match else "",
        "report": full_report,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })

    yield "done", "Due diligence concluída"
