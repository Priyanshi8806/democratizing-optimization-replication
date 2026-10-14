"""Walk through the 4I loop of Sec. 5.3 on a synthetic instance and write a transcript.

  python scripts/demo.py                       # rule parser, no LLM needed
  python scripts/demo.py --llm                 # Ollama + qwen2.5:7b-instruct (parse + narrate)
  python scripts/demo.py --llm --model qwen2.5:3b-instruct
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genai_opt.agent.agent import Assistant  # noqa: E402
from genai_opt.agent.parsers import LLMParser, RuleParser  # noqa: E402
from genai_opt.milp.instance import generate  # noqa: E402
from genai_opt.milp.model import solve  # noqa: E402

SCRIPT = [
    ("Insight", "Where do we stand as of today?"),
    ("Interpretability", "Why is d5 docked where it is?"),
    ("Interpretability", "Why is S2 not used for d1?"),
    ("Interactivity (Table 2, row 1)", "Can demand d3 dock at least one week earlier than the current plan?"),
    ("Interactivity (Table 2, row 2)", "What if supplier S3 becomes unavailable?"),
    ("Interactivity (Table 2, row 3)", "Can we still satisfy all demands if the throughput at data center DC2 drops by 50% next week?"),
    ("Interactivity (Sec. 5.2 example)", "What if demand d1 must be fulfilled by supplier S2?"),
    ("Out of scope", "What's the capital of France?"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    inst = generate(seed=a.seed)
    Path("data").mkdir(exist_ok=True)
    inst.to_json(f"data/instance_seed{a.seed}.json")
    if a.llm:
        p = LLMParser(model=a.model, base_url=a.base_url)
        A = Assistant(inst, p, narrator=p)
    else:
        A = Assistant(inst, RuleParser())

    md = [f"# 4I assistant transcript (parser: {A.parser.name}, seed {a.seed})\n"]
    for tag, q in SCRIPT:
        ans = A.ask(q)
        md += [f"## {tag}", f"**Planner:** {q}", "",
               f"**Parsed action:** `{json.dumps(ans.action)}`",
               f"**Model edit:** {ans.scenario_note or '(none - explanation only)'}", "",
               f"**Assistant:** {ans.text}", "",
               f"_timings: {', '.join(f'{k} {v:.3f}s' for k, v in ans.timings.items())}"
               + (f"; narration fidelity {ans.fidelity:.2f}" if ans.fidelity is not None else "") + "_", ""]

    # Improvisation: event-triggered re-optimisation with a change log
    A.ask("What if supplier S3 becomes unavailable?")
    entry = A.commit("S3 plant down for five weeks (planner-initiated event)")
    after = A.ask("Where do we stand as of today?")
    md += ["## Improvisation (event-triggered re-optimisation)",
           "**Planner:** S3 is down for five weeks - update the plan.", "",
           f"**Change log entry:** `{json.dumps(entry, default=str)}`", "",
           f"**Assistant (new baseline):** {after.text}", ""]

    # Interactivity metric: time-to-first-feasible-solution vs. size
    md += ["## Solve time vs. instance size (Interactivity metric)", "",
           "| demands | DCs | suppliers | days | binaries | solve s |", "|---|---|---|---|---|---|"]
    for n, L, S, T in [(60, 4, 3, 14), (200, 6, 4, 21), (500, 8, 5, 28), (1000, 10, 6, 28)]:
        I = generate(n_demands=n, n_datacenters=L, n_suppliers=S, n_days=T, seed=1)
        nb = sum(len(I.compat_dc[d]) * T + len(I.compat_sup[d]) for d in I.demands)
        t0 = time.time()
        s = solve(I, time_limit=120)
        md.append(f"| {n} | {L} | {S} | {T} | {nb} | {time.time() - t0:.2f} ({s.status}) |")

    Path("results").mkdir(exist_ok=True)
    out = Path("results") / f"demo_transcript_{'llm' if a.llm else 'rules'}.md"
    out.write_text("\n".join(md))
    print("\n".join(md))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
