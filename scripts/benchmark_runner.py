"""benchmark_runner.py

Roda benchmarks fixos e gera métricas comparáveis.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from glob import glob
from typing import Dict, List

from dotenv import load_dotenv
import argparse
import uuid

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND_DIR = BASE_DIR
BENCH_DIR = os.path.join(BASE_DIR, "benchmarks")
RUNS_DIR = os.path.join(BASE_DIR, "runs")


def _load_env():
    load_dotenv(os.path.join(BACKEND_DIR, ".env"))


def _config_dir() -> str:
    return os.getenv("CORTIQ_CONFIG_DIR", os.path.join(BASE_DIR, "config"))


def _load_eval_rules() -> Dict:
    path = os.path.join(_config_dir(), "evaluation_rules.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _now_ts() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def _load_benchmarks() -> List[Dict]:
    files = sorted(glob(os.path.join(BENCH_DIR, "*.json")))
    items = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            items.append(json.load(fh))
    return items


def _score_critic_usefulness(critic: str) -> float:
    if not critic:
        return 0.0
    lines = [l for l in critic.split("\n") if l.strip()]
    return min(1.0, len(lines) / 6.0)


def _extract_primary_domains(results: List[dict]) -> List[str]:
    domains = []
    for block in results:
        for item in (block or {}).get("results", []):
            url = (item or {}).get("url") or ""
            if not url:
                continue
            parts = url.split("/")
            if len(parts) > 2:
                domains.append(parts[2])
    return list(dict.fromkeys(domains))


def _mock_review(bench: Dict) -> Dict:
    expected_sections = bench.get("expected_sections", [])
    critical_gaps = bench.get("critical_gaps_if_missing", [])

    # Simples heurística offline
    weakly_covered = [s for s in critical_gaps if s in expected_sections]
    covered = [s for s in expected_sections if s not in weakly_covered]
    missing = []

    coverage_score = round((len(covered) + 0.4 * len(weakly_covered)) / max(1, len(expected_sections)), 2)
    primary_ratio = 0.2 if bench.get("expected_primary_source_domains") else 0.0
    evidence_score = round(min(1.0, 0.5 * coverage_score + 0.5 * primary_ratio), 2)

    critic_notes = "- Mock critic: sinais fracos em dados críticos.\n- Mock critic: evidencia insuficiente para valuation."

    return {
        "evaluation": {
            "coverage_score": coverage_score,
            "evidence_score": evidence_score,
            "primary_source_ratio": primary_ratio,
            "covered_sections": covered,
            "weakly_covered_sections": weakly_covered,
            "missing_sections": missing,
            "primary_backed_sections": covered if primary_ratio > 0 else [],
        },
        "critic_notes": critic_notes,
        "followup_queries": ["mock followup"] if weakly_covered else [],
        "artifact_path": None,
        "results": [],
    }


def run_benchmarks(version: str = "v4-dev", dry_run: bool = False) -> Dict:
    if not dry_run:
        _load_env()
    os.makedirs(RUNS_DIR, exist_ok=True)

    run_id = f"run-{_now_ts()}-{version}-{uuid.uuid4().hex[:6]}"
    run_path = os.path.join(RUNS_DIR, f"{run_id}.json")

    results = []
    total_score = 0.0

    if not dry_run:
        import sys
        sys.path.append(BACKEND_DIR)
        from agent import run_equity_pipeline_sync, run_startup_pipeline_sync

    benchmarks = _load_benchmarks()
    # Em modo real, limitar a 2 casos (1 equity + 1 startup) para não exceder timeout
    if not dry_run:
        equity_cases = [b for b in benchmarks if b["mode"] == "stock"][:1]
        startup_cases = [b for b in benchmarks if b["mode"] == "startup"][:1]
        benchmarks = equity_cases + startup_cases

    for bench in benchmarks:
        start = time.time()
        mode = bench["mode"]

        if dry_run:
            reviewed = _mock_review(bench)
            latency = round(time.time() - start, 2)
            eval_data = reviewed["evaluation"]
            primary_domains = bench.get("expected_primary_source_domains", [])
        else:
            if mode == "stock":
                reviewed = run_equity_pipeline_sync(bench["input"]["ticker"])
            else:
                reviewed = run_startup_pipeline_sync(
                    bench["input"]["startup_name"],
                    bench["input"].get("website", ""),
                )
            latency = round(time.time() - start, 2)
            eval_data = reviewed.get("evaluation", {})
            primary_domains = _extract_primary_domains([{"results": reviewed.get("sources", [])}])

        expected_primary = bench.get("expected_primary_source_domains", [])
        primary_hit = any(p in ".".join(primary_domains) for p in expected_primary) if expected_primary else True

        critical_gaps = bench.get("critical_gaps_if_missing", [])
        gap_missed = any(g in eval_data.get("missing_sections", []) for g in critical_gaps)

        critic_score = _score_critic_usefulness(reviewed.get("critic_notes", ""))

        rules = _load_eval_rules()
        final_score = round(
            rules.get("coverage_weight", 0.45) * eval_data.get("coverage_score", 0) +
            rules.get("evidence_weight", 0.35) * eval_data.get("evidence_score", 0) +
            rules.get("primary_weight", 0.10) * eval_data.get("primary_source_ratio", 0) +
            rules.get("critic_weight", 0.10) * critic_score,
            2,
        )

        if not primary_hit:
            final_score = round(max(0.0, final_score - 0.1), 2)
        if gap_missed:
            final_score = round(max(0.0, final_score - 0.15), 2)

        total_score += final_score

        results.append({
            "id": bench["id"],
            "mode": mode,
            "input": bench["input"],
            "evaluation": eval_data,
            "critic_score": critic_score,
            "primary_domains": primary_domains,
            "retry_count": 1 if reviewed.get("followup_queries") else 0,
            "latency": latency,
            "final_score": final_score,
            "artifact_path": reviewed.get("artifact_path"),
            "dry_run": dry_run,
        })

    aggregate = round(total_score / max(1, len(results)), 2)

    payload = {
        "run_id": run_id,
        "timestamp": datetime.utcnow().isoformat(),
        "version": version,
        "dry_run": dry_run,
        "results": results,
        "aggregate_score": aggregate,
    }

    with open(run_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    return payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="v4-dev")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    out = run_benchmarks(version=args.version, dry_run=args.dry_run)
    print(json.dumps(out, ensure_ascii=False, indent=2))
