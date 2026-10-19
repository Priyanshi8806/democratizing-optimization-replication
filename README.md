# Replication: *Democratizing Optimization with Generative AI* (Simchi-Levi, Dai, Menache, Wu; SSRN 5511218, v. 30 Sep 2026)

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Priyanshi8806/democratizing-optimization-replication/blob/main/replication_colab.ipynb)

This is a perspective paper. It has no estimation and no public data. This repository reproduces the parts that can be reproduced:

| Paper element | Here | Status |
|---|---|---|
| Fig. 1, Google Trends (5 terms, US, Aug 2020–Aug 2025) | `scripts/figure1_trends.py` | Code only. Run it locally, because Trends is blocked from the build sandbox |
| Sec. 5.2.1 MILP, eqs. (1)–(7) | `genai_opt/milp/model.py` (PuLP + HiGHS) | Done. Checked against brute force |
| Microsoft demand plan / inventories | `genai_opt/milp/instance.py` | **Synthetic.** The real data are proprietary |
| Table 2 what-if queries plus Sec. 5.3 4I loop | `genai_opt/agent/` | Done (rule-based and LLM parsers) |
| OptiGuide-style accuracy (Li et al. 2023: ID vs OOD, set-level) | `scripts/run_benchmark.py` | Done for rules. Run it for Qwen locally |
| Table 1 metrics (time-to-solve, explanation fidelity, change log) | `agent.py`, `demo.py` | Done |

## Setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest -q tests/                 # 7 tests, including MILP == brute force on small instances
```

## Free LLM (Ollama + Qwen)
```bash
# install Ollama from https://ollama.com, then:
ollama pull qwen2.5:7b-instruct          # ~4.7 GB; use qwen2.5:3b-instruct on a weak laptop, qwen2.5:14b-instruct if you have the RAM
ollama serve                              # usually already running as a service
python scripts/demo.py --llm             # 4I walkthrough -> results/demo_transcript_llm.md
python scripts/run_benchmark.py --backend llm --model qwen2.5:7b-instruct
python scripts/run_benchmark.py --backend llm --model llama3.1:8b
python scripts/chat.py                    # interactive; :commit <reason>, :log, :facts
```
Any OpenAI-compatible server works, e.g. vLLM on an HPC node:
`--base-url http://<node>:8000/v1 --model Qwen/Qwen2.5-7B-Instruct` (or set `LLM_BASE_URL` and `LLM_MODEL`).

## Multi-agent 4I system (`genai_opt/agents/`)
```
message ─► Router ─► Insight            state snapshot
                 ├─► Interpretability   Planner → Executor (counterfactual) → Validator → Explainer
                 ├─► Interactivity      Planner → Executor → Validator → Explainer        (what-if, never commits)
                 └─► Improvisation      Planner → Executor → Validator → approval gate → commit + change log
```
* LLM agents (Router, Planner, Explainer) use Qwen / Llama via Ollama. Each has a rule-based fallback, so the system runs with no LLM.
* The Executor (HiGHS) and Validator (constraint and fidelity checks) never use an LLM. This follows the paper's "GenAI translates, solver verifies" design.
* Improvisation auto-applies validated changes with ≤5% cost impact. Larger ones wait for `yes` or `no`.
* If an agent receives a request that belongs to another one, it hands the request off (e.g. a "why not" question sent to Interactivity).
```bash
python scripts/multi_agent.py                         # no LLM
python scripts/multi_agent.py --llm                   # Qwen 2.5 7B
python scripts/multi_agent.py --llm --model llama3.1:8b --chat
```

## Design (mirrors the paper's division of labour)
```
question ─LLM/rules─▶ ONE action from a fixed catalog (JSON) ─validate─▶ model edit ─HiGHS─▶ solution
        ◀─ template explanation from solver facts (optionally LLM-narrated, rejected if it invents numbers)
```
* The LLM never writes solver code, and never sees raw data beyond entity ids. This matches Sec. 5.4: proprietary data stay in enterprise systems.
* Infeasible what-ifs are diagnosed by an elastic re-solve. The diagnosis names the minimal set of violated requirements, a lightweight IIS.
* Scenario re-solves add a 1e-5 tie-break toward the baseline, so plans don't churn among alternative optima. Reported costs exclude this term.
* `Assistant.commit()` adopts a what-if as the new baseline and writes a change log. This is Improvisation, planner-initiated as in the deployed system.

## Results so far (seed 0, 60 demands × 4 DCs × 3 suppliers × 14 days)
* Baseline cost 676.37; 51/60 fulfilled; all row capacities bind; S1 and S3 are at their inventory limits.
* Table 2: dock d3 one week earlier gives +7.00 (+1.0%). S3 unavailable gives +818.48 (+121%), and 15 demands drop. Halving DC2 throughput next week stays feasible at +1.00.
* Rule parser: 100% on in-distribution phrasing and 15% on out-of-distribution phrasing (set accuracy 100% vs 0%). This is the gap the LLM is supposed to close. The rules were written against the ID templates, so the ID score is optimistic by construction.
* Solve time: 0.3 s at 60 demands, 2.2 s at 200, 13 s at 500, 42 s at 1,000 (HiGHS, 2 vCPU).
