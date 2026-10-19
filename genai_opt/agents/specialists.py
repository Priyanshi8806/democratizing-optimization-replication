"""Worker agents used inside the 4I agents (Sec. 6.2 'canonical decomposition').

Planner   : natural language -> ONE catalog action (LLM, fallback: rules)
Executor  : action -> model edit -> HiGHS solve (no LLM; the solver does the solving)
Validator : independent checks on the edit and the returned plan (no LLM)
Explainer : solver facts -> plain-language rationale (template; optional LLM narration,
            rejected if it states numbers not present in the facts)
"""
from __future__ import annotations

import time

from ..agent.agent import fidelity
from ..agent.catalog import ACTIONS, ActionError, apply, validate
from ..agent.parsers import RuleParser
from .base import Agent, Trace


class PlannerAgent(Agent):
    name = "Planner"

    def run(self, question: str, trace: Trace) -> dict:
        t0 = time.time()
        parser = self.llm or RuleParser()
        try:
            action = validate(parser.parse(question, self.state.inst), self.state.inst)
            src = getattr(parser, "name", "rules")
        except (ActionError, Exception) as e:  # LLM down / invalid output -> deterministic fallback
            try:
                action = validate(RuleParser().parse(question, self.state.inst), self.state.inst)
                src = f"rules (fallback: {type(e).__name__})"
            except ActionError as e2:
                action, src = {"action": "out_of_scope", "args": {"reason": str(e2)}}, "rules"
        trace.add(self.name, "parsed request", {"action": action["action"], "args": action["args"], "by": src}, t0)
        return action


class ExecutorAgent(Agent):
    name = "Executor"

    def run(self, action: dict, trace: Trace):
        from ..milp.model import solve
        t0 = time.time()
        c = self.state.core
        sc = apply(action, c.inst, c.edits, c.base, c.solver)
        sol = solve(sc.inst, sc.edits, time_limit=sc.solver.get("time_limit"), gap=sc.solver.get("gap"),
                    anchor=c.base.assignment)
        trace.add(self.name, "solved scenario", {"edit": sc.note, "status": sol.status,
                                                 "cost": round(sol.objective, 2) if sol.objective else None}, t0)
        return sc, sol


class ValidatorAgent(Agent):
    """Rule-based checks the paper asks for (Sec. 2.2, 6.1, 6.3.2): never trust, verify."""
    name = "Validator"

    def check_plan(self, sc, sol, trace: Trace) -> dict:
        t0 = time.time()
        inst, issues = sc.inst, []
        if sol.objective is None:
            out = {"ok": False, "feasible": False, "issues": ["solver found no feasible plan"]}
            trace.add(self.name, "plan infeasible", {"why": sol.infeasible_edits[:3]}, t0)
            return out
        lt, rows, inv = {}, {}, {}
        for d, a in sol.assignment.items():
            if not a:
                if d in sc.edits.must_fulfill:
                    issues.append(f"{d} required but unfulfilled")
                continue
            l, t, s = a
            if l not in inst.compat_dc[d]:
                issues.append(f"{d} at incompatible DC {l}")
            if s not in inst.compat_sup[d]:
                issues.append(f"{d} from incompatible supplier {s}")
            if d in sc.edits.forced_supplier and sc.edits.forced_supplier[d] != s:
                issues.append(f"{d} not from forced supplier")
            lo, hi = sc.edits.dock_window.get(d, (min(inst.days), max(inst.days)))
            if not lo <= t <= hi:
                issues.append(f"{d} outside dock window")
            lt[l, t] = lt.get((l, t), 0) + 1
            rows[l] = rows.get(l, 0) + 1
            inv[s] = inv.get(s, 0) + 1
        issues += [f"throughput exceeded at {k}" for k, v in lt.items() if v > inst.throughput[k[0]][k[1]]]
        issues += [f"rows exceeded at {l}" for l, v in rows.items() if v > inst.rows[l]]
        issues += [f"inventory exceeded at {s}" for s, v in inv.items() if v > inst.inventory[s]]
        recomputed = sol.cost_breakdown["total"]
        out = {"ok": not issues, "feasible": True, "issues": issues, "cost_recomputed": recomputed}
        trace.add(self.name, "plan verified" if not issues else "plan REJECTED",
                  {"checks": "compatibility, (2)-(6), edits", "issues": len(issues)}, t0)
        return out

    def check_explanation(self, text: str, facts: dict, template: str, trace: Trace) -> float:
        f = fidelity(text, facts, template)
        trace.add(self.name, "explanation fidelity", {"score": round(f, 3)})
        return f


class ExplainerAgent(Agent):
    name = "Explainer"

    def __init__(self, state, llm=None, validator: ValidatorAgent | None = None):
        super().__init__(state, llm)
        self.validator = validator

    def run(self, question: str, facts: dict, template: str, trace: Trace) -> str:
        t0 = time.time()
        if not self.llm:
            trace.add(self.name, "template explanation", {}, t0)
            return template
        try:
            text = self.llm.narrate(question, {**facts, "reference_summary": template})
        except Exception as e:
            trace.add(self.name, "LLM unavailable, template used", {"err": type(e).__name__}, t0)
            return template
        trace.add(self.name, "LLM narration", {"model": getattr(self.llm, "model", "?")}, t0)
        if self.validator and self.validator.check_explanation(text, facts, template, trace) < 0.999:
            trace.add(self.name, "narration rejected (unsupported numbers) -> template", {})
            return template
        return text


def principle_of(action: dict) -> str:
    return ACTIONS[action["action"]][2]
