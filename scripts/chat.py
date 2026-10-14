"""Interactive 4I assistant in the terminal.

  ollama pull qwen2.5:7b-instruct && ollama serve      # in another terminal (if not already running)
  python scripts/chat.py                               # LLM parse + narration via Ollama
  python scripts/chat.py --rules                       # no LLM

Commands:  :commit <reason>   adopt last what-if as new baseline
           :log               show change log
           :facts             show raw facts behind the last answer
           :quit
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genai_opt.agent.agent import Assistant  # noqa: E402
from genai_opt.agent.parsers import LLMParser, RuleParser  # noqa: E402
from genai_opt.milp.instance import Instance, generate  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rules", action="store_true")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--instance", default=None, help="JSON instance (default: generate seed 0)")
    ap.add_argument("--no-narrate", action="store_true", help="use templated explanations only")
    a = ap.parse_args()
    inst = Instance.from_json(a.instance) if a.instance else generate(seed=0)
    if a.rules:
        A = Assistant(inst, RuleParser())
    else:
        p = LLMParser(model=a.model, base_url=a.base_url)
        A = Assistant(inst, p, narrator=None if a.no_narrate else p)
    print(f"Loaded {len(inst.demands)} demands, {len(inst.datacenters)} DCs, {len(inst.suppliers)} suppliers. "
          f"Baseline cost {A.base.objective:.2f}. Parser: {A.parser.name}")
    last = None
    while True:
        try:
            q = input("\nplanner> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not q:
            continue
        if q in (":q", ":quit", "exit"):
            break
        if q.startswith(":commit"):
            try:
                print(json.dumps(A.commit(q[7:].strip()), indent=1, default=str))
            except RuntimeError as e:
                print(e)
            continue
        if q == ":log":
            print(json.dumps(A.changelog, indent=1, default=str))
            continue
        if q == ":facts":
            print(json.dumps(last.facts if last else {}, indent=1, default=str))
            continue
        last = A.ask(q)
        print(f"[{last.principle}] action={json.dumps(last.action)}")
        if last.scenario_note:
            print(f"[model edit] {last.scenario_note}")
        print(last.text)


if __name__ == "__main__":
    main()
