"""Multi-agent 4I system: scripted demo or interactive chat.

  python scripts/multi_agent.py                              # scripted demo, no LLM (rules)
  python scripts/multi_agent.py --llm                        # Qwen 2.5 7B via Ollama
  python scripts/multi_agent.py --llm --model llama3.1:8b    # Llama 3.1 8B via Ollama
  python scripts/multi_agent.py --llm --chat                 # talk to it yourself
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genai_opt.agent.parsers import LLMParser  # noqa: E402
from genai_opt.agents import MultiAgentSystem  # noqa: E402
from genai_opt.milp.instance import generate  # noqa: E402

SCRIPT = [
    "Where do we stand as of today?",                                         # Insight
    "Why is d5 docked where it is?",                                          # Interpretability
    "How come d1 isn't coming from S2?",                                      # Interpretability (counterfactual)
    "What if supplier S3 becomes unavailable?",                               # Interactivity
    "Can demand d3 dock at least one week earlier than the current plan?",    # Interactivity
    "Apply that scenario.",                                                   # Improvisation (auto-apply, small)
    "Supplier S1's plant is down for five weeks. Update the plan.",           # Improvisation (needs approval)
    "yes, approve",                                                           # approval
    "Where do we stand as of today?",                                         # Insight after changes
    "Write me a poem about servers.",                                         # out of scope
]


def show(r):
    print(f"\n>>> {r.question}")
    print(r.trace.pretty())
    print(f"[{r.route} | {r.status}] {r.text}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--chat", action="store_true")
    ap.add_argument("--no-narrate", action="store_true")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()

    llm = LLMParser(model=a.model, base_url=a.base_url) if a.llm else None
    M = MultiAgentSystem(generate(seed=a.seed), llm=llm, narrate=not a.no_narrate)
    print(f"Multi-agent 4I system | LLM: {llm.model if llm else 'none (rules)'} | baseline cost "
          f"{M.state.base.objective:.2f}")

    if a.chat:
        print("Type a question (':log' = change log, ':q' = quit).")
        while True:
            try:
                q = input("\nplanner> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if q in (":q", "quit", "exit"):
                break
            if q == ":log":
                for e in M.changelog:
                    print(e)
                continue
            if q:
                show(M.ask(q))
        return

    lines = [f"# Multi-agent 4I transcript (LLM: {llm.model if llm else 'rules'})\n"]
    for q in SCRIPT:
        r = M.ask(q)
        show(r)
        lines += [f"### {q}", "```", r.trace.pretty(), "```", f"**[{r.route} | {r.status}]** {r.text}", ""]
    lines += ["## Change log", *[f"- {e}" for e in M.changelog]]
    Path("results").mkdir(exist_ok=True)
    tag = (llm.model.split(":")[0] if llm else "rules").replace("/", "_")
    out = Path("results") / f"multi_agent_{tag}.md"
    out.write_text("\n".join(lines))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
