"""Parser accuracy + end-to-end correctness on the question benchmark.

Usage:
  python scripts/run_benchmark.py --backend rules
  python scripts/run_benchmark.py --backend llm --model qwen2.5:7b-instruct            # Ollama default
  python scripts/run_benchmark.py --backend llm --model qwen2.5:14b-instruct --base-url http://localhost:11434/v1
  python scripts/run_benchmark.py --backend llm --model Qwen/Qwen2.5-7B-Instruct --base-url http://<hpc-node>:8000/v1  # vLLM

Metrics (cf. Li et al. 2023 / Table 1 of the paper):
  question accuracy   exact action + args
  set accuracy        a question set counts only if ALL its questions are correct
  e2e accuracy        predicted scenario gives the same solver outcome (status + cost) as the gold scenario
  latency             parse time, solve time  (Interactivity: time-to-first-feasible-solution)
"""
import argparse
import json
import statistics as st
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genai_opt.agent.catalog import ActionError, apply, validate  # noqa: E402
from genai_opt.agent.parsers import LLMParser, RuleParser  # noqa: E402
from genai_opt.bench.questions import build, match  # noqa: E402
from genai_opt.milp.instance import generate  # noqa: E402
from genai_opt.milp.model import Edits, solve  # noqa: E402


def outcome(action, inst, base):
    if action["action"] in ("status_summary", "explain_assignment", "out_of_scope"):
        return ("explain", action["action"], json.dumps(action["args"], sort_keys=True))
    sc = apply(action, inst, Edits(), base, {"time_limit": 60, "gap": None})
    s = solve(sc.inst, sc.edits, diagnose=False)
    return (s.status, round(s.objective, 4) if s.objective is not None else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["rules", "llm"], default="rules")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--sets", type=int, default=10)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="results")
    a = ap.parse_args()

    inst = generate(seed=a.seed)
    base = solve(inst)
    parser = RuleParser() if a.backend == "rules" else LLMParser(model=a.model, base_url=a.base_url)
    sets = build(inst, n_sets=a.sets, seed=a.seed)

    rows = []
    for qs in sets:
        for item in qs:
            t0 = time.time()
            try:
                pred = validate(parser.parse(item["question"], inst), inst)
            except ActionError as e:
                pred = {"action": "out_of_scope", "args": {"reason": str(e)}}
            except Exception as e:  # connection errors etc.
                print(f"parser error: {e}", file=sys.stderr)
                pred = {"action": "error", "args": {"reason": str(e)}}
            tp = time.time() - t0
            ok = match(pred, item["gold"])
            t1 = time.time()
            try:
                e2e = ok or (pred["action"] != "error" and outcome(pred, inst, base) == outcome(item["gold"], inst, base))
            except Exception:
                e2e = False
            ts = time.time() - t1
            rows.append({**item, "pred": pred, "correct": ok, "e2e_correct": bool(e2e), "parse_s": tp, "solve_s": ts})

    out = Path(a.out)
    out.mkdir(exist_ok=True)
    tag = a.backend if a.backend == "rules" else f"llm_{(parser.model).replace('/', '_').replace(':', '_')}"
    with open(out / f"bench_{tag}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    summ = {"backend": parser.name}
    for split in ("ID", "OOD"):
        R = [r for r in rows if r["split"] == split]
        set_ids = sorted({r["set"] for r in R})
        summ[split] = {
            "n_questions": len(R),
            "question_acc": round(sum(r["correct"] for r in R) / len(R), 3),
            "e2e_acc": round(sum(r["e2e_correct"] for r in R) / len(R), 3),
            "set_acc": round(sum(all(r["correct"] for r in R if r["set"] == s) for s in set_ids) / len(set_ids), 3),
            "median_parse_s": round(st.median(r["parse_s"] for r in R), 4),
        }
        by_type = {}
        for r in R:
            by_type.setdefault(r["gold"]["action"], []).append(r["correct"])
        summ[split]["acc_by_type"] = {k: round(sum(v) / len(v), 2) for k, v in sorted(by_type.items())}
    (out / f"bench_{tag}_summary.json").write_text(json.dumps(summ, indent=2))
    print(json.dumps(summ, indent=2))


if __name__ == "__main__":
    main()
