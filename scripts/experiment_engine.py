"""experiment_engine.py

Loop controlado: proposer -> candidate -> benchmark -> compare -> keep/discard -> registry -> leaderboard.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(BASE_DIR, "config")
RUNS_DIR = os.path.join(BASE_DIR, "runs")
EXPER_DIR = os.path.join(BASE_DIR, "experiments")
LEADERBOARD_DIR = os.path.join(BASE_DIR, "leaderboard")
BASELINE_STATE = os.path.join(EXPER_DIR, "baseline_state.json")
LINEAGE_PATH = os.path.join(EXPER_DIR, "baseline_lineage.json")


def _now() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: str, data: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _run_benchmark(config_dir: str, label: str, dry_run: bool = True) -> str:
    dry_flag = "--dry-run" if dry_run else ""
    import sys
    cmd = (
        f"CORTIQ_CONFIG_DIR={config_dir} "
        f"{sys.executable} "
        f"{os.path.join(BASE_DIR, 'scripts', 'benchmark_runner.py')} {dry_flag} --version {label}"
    )
    os.system(cmd)
    runs = sorted([f for f in os.listdir(RUNS_DIR) if f.startswith('run-') and f.endswith('.json') and label in f])
    return os.path.join(RUNS_DIR, runs[-1])


def _compare(baseline_path: str, candidate_path: str) -> dict:
    import sys
    cmd = (
        f"{sys.executable} "
        f"{os.path.join(BASE_DIR, 'scripts', 'compare_runs.py')} {baseline_path} {candidate_path}"
    )
    return json.loads(os.popen(cmd).read())


def _promotion_gate(compare: dict, baseline_run: dict, candidate_run: dict) -> tuple[str, str]:
    # Decision rules
    agg_delta = compare.get("aggregate_delta", 0)
    worsen_critic = any(d.get("delta_final_score", 0) < 0 and d.get("delta_retry", 0) > 0 for d in compare.get("deltas", []))

    if agg_delta > 0 and not worsen_critic:
        return "KEEP", "Aggregate improved without critical regressions."
    if agg_delta == 0 and not worsen_critic:
        return "KEEP", "No regression detected."
    return "DISCARD", "Aggregate declined or regression detected."


def _update_leaderboard(log: dict) -> None:
    os.makedirs(LEADERBOARD_DIR, exist_ok=True)
    path = os.path.join(LEADERBOARD_DIR, "index.json")
    if os.path.exists(path):
        data = _load_json(path)
    else:
        data = {"experiments": []}
    data["experiments"].append({
        "experiment_id": log["experiment_id"],
        "decision": log["decision"],
        "aggregate_delta": log["comparison"].get("aggregate_delta"),
        "timestamp": log["timestamp"],
        "mutation": log["mutation"],
    })
    _write_json(path, data)


def _load_baseline_state() -> dict:
    if os.path.exists(BASELINE_STATE):
        return _load_json(BASELINE_STATE)
    return {"active_config": CONFIG_DIR, "history": []}


def _update_lineage(entry: dict) -> None:
    if os.path.exists(LINEAGE_PATH):
        data = _load_json(LINEAGE_PATH)
    else:
        data = {"lineage": []}
    data["lineage"].append(entry)
    _write_json(LINEAGE_PATH, data)


def run_experiment(
    mutation_type: str | None = None,
    candidates: int = 3,
    dry_run: bool = True,
    on_progress=None,   # callback(event: str, data: dict)
) -> dict:
    def _emit(event: str, data: dict):
        if on_progress:
            on_progress(event, data)

    os.makedirs(EXPER_DIR, exist_ok=True)
    exp_id = f"exp-{_now()}"
    exp_path = os.path.join(EXPER_DIR, exp_id)
    os.makedirs(exp_path, exist_ok=True)
    _emit("start", {"exp_id": exp_id, "candidates": candidates, "dry_run": dry_run})

    state = _load_baseline_state()
    active_baseline = state.get("active_config", CONFIG_DIR)

    baseline_dir = os.path.join(exp_path, "baseline")
    shutil.copytree(active_baseline, baseline_dir)

    import sys
    sys.path.append(os.path.join(BASE_DIR, "scripts"))
    from proposer import propose_heuristic

    baseline_run = _run_benchmark(baseline_dir, "baseline", dry_run=dry_run)
    _emit("baseline_done", {"run": baseline_run})

    candidate_runs = []
    candidate_logs = []

    for idx in range(candidates):
        cand_dir = os.path.join(exp_path, f"candidate_{idx+1}")
        shutil.copytree(active_baseline, cand_dir)
        os.environ["CORTIQ_CONFIG_DIR"] = cand_dir
        mutation = propose_heuristic(mutation_type)
        cand_run = _run_benchmark(cand_dir, f"candidate_{idx+1}", dry_run=dry_run)
        compare = _compare(baseline_run, cand_run)
        decision, rationale = _promotion_gate(compare, _load_json(baseline_run), _load_json(cand_run))
        _emit("candidate_done", {
            "idx": idx + 1,
            "mutation": mutation,
            "decision": decision,
            "rationale": rationale,
            "aggregate_delta": compare.get("aggregate_delta", 0),
        })

        candidate_runs.append(cand_run)
        candidate_logs.append({
            "candidate_id": idx + 1,
            "mutation": mutation,
            "candidate_config": cand_dir,
            "candidate_run": cand_run,
            "comparison": compare,
            "decision": decision,
            "rationale": rationale,
        })

    # escolher melhor candidato KEEP pelo aggregate_delta
    keep_candidates = [c for c in candidate_logs if c["decision"] == "KEEP"]
    best_candidate = None
    if keep_candidates:
        best_candidate = max(keep_candidates, key=lambda c: c["comparison"].get("aggregate_delta", 0))

    promotion = {
        "promoted": False,
        "new_baseline": None,
    }

    if best_candidate:
        promotion["promoted"] = True
        promotion["new_baseline"] = best_candidate["candidate_config"]
        state["history"].append({
            "timestamp": datetime.utcnow().isoformat(),
            "from": active_baseline,
            "to": best_candidate["candidate_config"],
            "experiment_id": exp_id,
        })
        state["active_config"] = best_candidate["candidate_config"]
        _write_json(BASELINE_STATE, state)
        _update_lineage(state["history"][-1])

    log = {
        "experiment_id": exp_id,
        "timestamp": datetime.utcnow().isoformat(),
        "baseline_config": baseline_dir,
        "baseline_run": baseline_run,
        "candidates": candidate_logs,
        "promotion": promotion,
        "dry_run": dry_run,
    }

    _emit("done", {
        "exp_id": exp_id,
        "promoted": promotion["promoted"],
        "best_delta": best_candidate["comparison"].get("aggregate_delta", 0) if best_candidate else 0,
    })
    _write_json(os.path.join(exp_path, "experiment.json"), log)
    _update_leaderboard({
        "experiment_id": exp_id,
        "decision": "PROMOTED" if promotion["promoted"] else "NONE",
        "comparison": {"aggregate_delta": best_candidate["comparison"].get("aggregate_delta", 0) if best_candidate else 0},
        "mutation": best_candidate["mutation"] if best_candidate else {},
        "timestamp": log["timestamp"],
    })
    return log


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutation", default=None)
    parser.add_argument("--candidates", type=int, default=3)
    parser.add_argument("--real", action="store_true")
    args = parser.parse_args()
    out = run_experiment(mutation_type=args.mutation, candidates=args.candidates, dry_run=not args.real)
    print(json.dumps(out, ensure_ascii=False, indent=2))
