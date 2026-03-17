"""main.py — Cortiq Decision Copilot v2
FastAPI server with SSE streaming, daily briefing scheduler, and draft review API.
"""
import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel
from typing import Optional, List

load_dotenv()

from agent import run_equity_analysis, run_startup_analysis
from briefing_runner import (
    run_watchlist_briefing, load_drafts, load_draft, save_draft, send_brief_email
)

# ── Scheduler ────────────────────────────────────────────
def _setup_scheduler(app):
    try:
        import json
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from zoneinfo import ZoneInfo

        wl_path = os.path.join(os.path.dirname(__file__), "watchlist.json")
        briefing_hour = 7
        try:
            with open(wl_path) as f:
                briefing_hour = json.load(f).get("briefing_hour", 7)
        except Exception:
            pass

        scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/Sao_Paulo"))
        scheduler.add_job(run_watchlist_briefing, "cron", hour=briefing_hour, minute=0)
        app.state.scheduler = scheduler
        return scheduler
    except Exception as e:
        print(f"[scheduler] init failed: {e}")
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = _setup_scheduler(app)
    if scheduler:
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown()


app = FastAPI(title="Cortiq Decision Copilot", lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
STATIC_DIR = os.path.join(FRONTEND_DIR, "static")

PORTFOLIO_PATH = os.path.join(BASE_DIR, "data", "portfolio.json")

def _load_portfolio():
    os.makedirs(os.path.dirname(PORTFOLIO_PATH), exist_ok=True)
    if not os.path.exists(PORTFOLIO_PATH):
        return {"companies": []}
    with open(PORTFOLIO_PATH, encoding="utf-8") as f:
        return json.load(f)

def _save_portfolio(data: dict):
    os.makedirs(os.path.dirname(PORTFOLIO_PATH), exist_ok=True)
    with open(PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Lab state (loop contínuo)
_lab_loop_state = {"running": False, "thread": None}


# ── Pages ─────────────────────────────────────────────────
@app.get("/")
def index():
    with open(os.path.join(FRONTEND_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/briefing")
def briefing_page():
    with open(os.path.join(FRONTEND_DIR, "briefing.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/lab")
def lab_page():
    with open(os.path.join(FRONTEND_DIR, "lab.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/portfolio")
def portfolio_page():
    with open(os.path.join(FRONTEND_DIR, "portfolio.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health():
    return {"status": "ok", "product": "Cortiq Decision Copilot v2"}


# ── SSE helpers ───────────────────────────────────────────
def _sse(event: str, data: str) -> str:
    data_lines = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{data_lines}\n\n"


# ── Analysis endpoints ────────────────────────────────────
@app.get("/analyze/equity")
async def analyze_equity(
    ticker: str, thesis: str = "", mandate: str = "",
    prev_verdict: str = "", prev_date: str = "",
):
    async def gen():
        try:
            async for event, data in run_equity_analysis(ticker, thesis, mandate, prev_verdict, prev_date):
                yield _sse(event, data)
        except Exception as e:
            yield _sse("error", str(e))
            yield _sse("done", "Falhou")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.get("/analyze/startup")
async def analyze_startup(
    name: str, url: str = "", thesis: str = "",
    prev_verdict: str = "", prev_date: str = "",
):
    async def gen():
        try:
            async for event, data in run_startup_analysis(name, url, thesis, prev_verdict, prev_date):
                yield _sse(event, data)
        except Exception as e:
            yield _sse("error", str(e))
            yield _sse("done", "Falhou")

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── Briefing API ──────────────────────────────────────────
class DraftUpdate(BaseModel):
    subject: Optional[str] = None
    content: Optional[str] = None
    recipients: Optional[List[str]] = None


@app.get("/api/drafts")
def list_drafts():
    drafts = load_drafts()
    return [{"id": d["id"], "date": d.get("date"), "status": d.get("status"),
             "subject": d.get("subject"), "generated_at": d.get("generated_at")}
            for d in drafts]


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str):
    draft = load_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Draft não encontrado")
    return draft


@app.patch("/api/drafts/{draft_id}")
def update_draft(draft_id: str, body: DraftUpdate):
    draft = load_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Draft não encontrado")
    if body.subject is not None:
        draft["subject"] = body.subject
    if body.content is not None:
        draft["content"] = body.content
    if body.recipients is not None:
        draft["recipients"] = body.recipients
    save_draft(draft)
    return draft


@app.post("/api/drafts/{draft_id}/send")
def send_draft(draft_id: str):
    draft = load_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Draft não encontrado")
    if not draft.get("recipients"):
        raise HTTPException(400, "Nenhum destinatário configurado")

    ok = send_brief_email(draft)
    if ok:
        from datetime import datetime, timezone
        draft["status"] = "sent"
        draft["sent_at"] = datetime.now(timezone.utc).isoformat()
        save_draft(draft)
        return {"ok": True, "message": f"Brief enviado para {draft['recipients']}"}
    else:
        raise HTTPException(500, "Falha no envio. Verifique RESEND_API_KEY.")


@app.delete("/api/drafts/{draft_id}")
def discard_draft(draft_id: str):
    draft = load_draft(draft_id)
    if not draft:
        raise HTTPException(404, "Draft não encontrado")
    draft["status"] = "discarded"
    save_draft(draft)
    return {"ok": True}


@app.post("/api/briefing/run")
async def trigger_briefing():
    """Manually trigger a briefing generation."""
    draft = await run_watchlist_briefing()
    return {"ok": True, "id": draft["id"]}


@app.get("/api/watchlist")
def get_watchlist():
    import json
    path = os.path.join(BASE_DIR, "watchlist.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.put("/api/watchlist")
async def update_watchlist(request: Request):
    import json
    body = await request.json()
    path = os.path.join(BASE_DIR, "watchlist.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    return {"ok": True}


# ── Lab API ───────────────────────────────────────────────
@app.get("/api/lab/experiments")
def list_lab_experiments():
    import glob as _glob
    exp_dir = os.path.join(BASE_DIR, "experiments")
    os.makedirs(exp_dir, exist_ok=True)
    files = sorted(
        _glob.glob(os.path.join(exp_dir, "exp-*", "experiment.json")),
        reverse=True,
    )[:50]
    results = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            try:
                d = json.load(fh)
                results.append({
                    "experiment_id": d.get("experiment_id"),
                    "timestamp": d.get("timestamp"),
                    "dry_run": d.get("dry_run"),
                    "promoted": d.get("promotion", {}).get("promoted"),
                    "candidates": [
                        {
                            "id": c.get("candidate_id"),
                            "mutation": c.get("mutation"),
                            "decision": c.get("decision"),
                            "rationale": c.get("rationale"),
                            "aggregate_delta": c.get("comparison", {}).get("aggregate_delta"),
                        }
                        for c in d.get("candidates", [])
                    ],
                })
            except Exception:
                pass
    return results


@app.get("/api/lab/leaderboard")
def get_lab_leaderboard():
    path = os.path.join(BASE_DIR, "leaderboard", "index.json")
    if not os.path.exists(path):
        return {"experiments": [], "stats": {"total": 0, "promoted": 0, "keep_rate": 0}, "best": [], "worst": []}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    exps = data.get("experiments", [])
    total = len(exps)
    promoted = sum(1 for e in exps if e.get("decision") == "PROMOTED")
    keep_rate = round(promoted / total * 100, 1) if total else 0
    best = sorted(exps, key=lambda e: e.get("aggregate_delta") or 0, reverse=True)[:5]
    worst = sorted(exps, key=lambda e: e.get("aggregate_delta") or 0)[:5]
    return {
        "experiments": exps[-20:],
        "stats": {"total": total, "promoted": promoted, "keep_rate": keep_rate},
        "best": best,
        "worst": worst,
    }


@app.post("/api/lab/run")
async def run_lab_experiment(
    candidates: int = 3,
    dry_run: bool = True,
    mutation_type: str | None = None,
):
    import sys as _sys
    _sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
    from lab_runner import run_experiment_stream

    async def gen():
        try:
            async for chunk in run_experiment_stream(
                candidates=candidates,
                dry_run=dry_run,
                mutation_type=mutation_type,
            ):
                yield chunk
        except Exception as e:
            yield f"event: __error__\ndata: {json.dumps({'message': str(e)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


@app.get("/api/lab/loop/status")
def lab_loop_status():
    return {"running": _lab_loop_state["running"]}


@app.post("/api/lab/loop/start")
def lab_loop_start(interval_hours: int = 6):
    if _lab_loop_state["running"]:
        return {"ok": False, "message": "Loop já está rodando."}

    def _loop_worker():
        import time
        import sys as _sys
        _sys.path.insert(0, os.path.join(BASE_DIR, "scripts"))
        from experiment_engine import run_experiment
        while _lab_loop_state["running"]:
            try:
                run_experiment(candidates=3, dry_run=False)
            except Exception as e:
                print(f"[lab loop] erro: {e}")
            time.sleep(interval_hours * 3600)

    _lab_loop_state["running"] = True
    t = threading.Thread(target=_loop_worker, daemon=True)
    _lab_loop_state["thread"] = t
    t.start()
    return {"ok": True, "message": f"Loop iniciado. Intervalo: {interval_hours}h."}


@app.post("/api/lab/loop/stop")
def lab_loop_stop():
    _lab_loop_state["running"] = False
    return {"ok": True, "message": "Loop parado."}


# ── Portfolio API ──────────────────────────────────────────
@app.get("/api/portfolio")
def get_portfolio():
    return _load_portfolio()


@app.post("/api/portfolio")
async def save_portfolio(request: Request):
    body = await request.json()
    _save_portfolio(body)
    return {"ok": True}


@app.post("/api/portfolio/analyze")
async def analyze_portfolio_companies():
    import sys as _sys, uuid as _uuid
    _sys.path.insert(0, BASE_DIR)
    from agent import _collect_from_generator, run_equity_analysis, run_startup_analysis
    from reporter import generate_brief_entry

    portfolio = _load_portfolio()
    companies = portfolio.get("companies", [])

    async def gen():
        if not companies:
            yield f"event: done\ndata: {json.dumps({'total': 0})}\n\n"
            return

        async def analyze_one(item):
            try:
                if item["type"] == "equity":
                    data = await _collect_from_generator(
                        run_equity_analysis(item["name"])
                    )
                else:
                    data = await _collect_from_generator(
                        run_startup_analysis(item["name"], item.get("url", ""))
                    )
                sources = data.get("sources", [])
                brief = await generate_brief_entry(sources, item["name"], item["type"])
                return {"id": item.get("id",""), "name": item["name"], "type": item["type"], "brief": brief, "ok": True}
            except Exception as e:
                return {"id": item.get("id",""), "name": item["name"], "type": item["type"], "brief": f"Erro: {e}", "ok": False}

        tasks = [asyncio.create_task(analyze_one(item)) for item in companies]
        for task in asyncio.as_completed(tasks):
            result = await task
            yield f"event: result\ndata: {json.dumps(result, ensure_ascii=False)}\n\n"
            # keepalive friendly — short pause between companies
            await asyncio.sleep(0)
        yield f"event: done\ndata: {json.dumps({'total': len(companies)})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── Lab Evolution API ──────────────────────────────────────
MUTATION_LABELS = {
    "increase_primary_weight": ("Fontes primárias reforçadas", "Priorizamos fontes oficiais (B3, SEC, relatórios de RI) — análises mais fundamentadas em dados primários"),
    "tighten_retry": ("Cobertura mais profunda", "Refinamos quando buscar dados adicionais — análises chegam mais completas"),
    "loosen_retry": ("Velocidade otimizada", "Reduzimos buscas redundantes sem perder qualidade de cobertura"),
    "boost_weak_coverage_weight": ("Tópicos escassos melhorados", "Melhoramos análise de seções com pouca evidência disponível no mercado"),
    "reduce_weak_coverage_weight": ("Foco em profundidade", "Priorizamos qualidade sobre quantidade nos tópicos cobertos"),
    "prioritize_traction_queries": ("Startups: tração em primeiro lugar", "Para startups, buscamos métricas reais de tração antes de qualquer outro dado"),
    "decrease_coverage_weight": ("Evidência acima de cobertura", "Priorizamos qualidade das evidências sobre amplitude de cobertura"),
}

@app.get("/api/lab/evolution")
def get_lab_evolution():
    path = os.path.join(BASE_DIR, "leaderboard", "index.json")
    if not os.path.exists(path):
        return {"improvements": [], "total_experiments": 0, "total_improvements": 0}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    exps = data.get("experiments", [])
    improvements = []
    for e in exps:
        if e.get("decision") != "PROMOTED":
            continue
        mutation = e.get("mutation", {})
        mtype = mutation.get("type", "")
        label, description = MUTATION_LABELS.get(mtype, (mtype, "Otimização aplicada ao pipeline de research"))
        delta = e.get("aggregate_delta") or 0
        improvements.append({
            "timestamp": e.get("timestamp", ""),
            "label": label,
            "description": description,
            "delta": delta,
            "mutation_type": mtype,
        })
    improvements.sort(key=lambda x: x["timestamp"], reverse=True)
    return {
        "improvements": improvements,
        "total_experiments": len(exps),
        "total_improvements": len(improvements),
    }
