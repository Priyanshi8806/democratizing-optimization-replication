"""Shared state, trace and base class for the multi-agent 4I system.

Architecture (paper Sec. 3, 5.3 and 6.2):

    question
       │
    RouterAgent ──► InsightAgent            (state summary)
       │        ──► InterpretabilityAgent   Planner → Executor(counterfactual) → Validator → Explainer
       │        ──► InteractivityAgent      Planner → Executor → Validator → Explainer   (what-if, no commit)
       │        ──► ImprovisationAgent      Planner → Executor → Validator → approval gate → commit + change log
       ▼
    answer + full agent trace

Every agent is plain Python. LLM-backed agents use any OpenAI-compatible endpoint
(Ollama + Qwen / Llama by default). Each LLM agent has a deterministic fallback,
so the whole system also runs with no LLM at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..agent.agent import Assistant
from ..milp.instance import Instance


@dataclass
class Step:
    agent: str
    action: str
    detail: dict
    seconds: float


@dataclass
class Trace:
    steps: list[Step] = field(default_factory=list)

    def add(self, agent, action, detail=None, t0=None):
        self.steps.append(Step(agent, action, detail or {}, round(time.time() - t0, 4) if t0 else 0.0))

    def pretty(self) -> str:
        return "\n".join(f"  [{s.agent:>16}] {s.action}"
                         + (f"  {_short(s.detail)}" if s.detail else "")
                         + (f"  ({s.seconds:.2f}s)" if s.seconds else "") for s in self.steps)


def _short(d: dict, n: int = 160) -> str:
    s = ", ".join(f"{k}={v}" for k, v in d.items())
    return s if len(s) <= n else s[: n - 3] + "..."


@dataclass
class Result:
    question: str
    route: str
    text: str
    facts: dict
    trace: Trace
    status: str = "answered"          # answered | needs_approval | rejected | out_of_scope
    proposal: dict | None = None


class SharedState:
    """Blackboard shared by all agents: the model, the incumbent plan, the change log."""

    def __init__(self, inst: Instance):
        # Assistant is reused as the state holder + fact builders (insight/explain/compare)
        self.core = Assistant(inst, parser=None)
        self.pending: dict | None = None      # Improvisation proposal awaiting approval
        self.history: list[dict] = []          # every question, route and outcome

    @property
    def inst(self):
        return self.core.inst

    @property
    def base(self):
        return self.core.base


class Agent:
    name = "agent"

    def __init__(self, state: SharedState, llm=None):
        self.state, self.llm = state, llm
