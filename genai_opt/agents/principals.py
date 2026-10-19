"""Router + the four 4I agents."""
from __future__ import annotations

import json
import re
import time

from .base import Agent, Result, Trace
from .specialists import ExecutorAgent, ExplainerAgent, PlannerAgent, ValidatorAgent, principle_of
from ..agent.parsers import RuleParser, _extract_json

ROUTES = ["insight", "interpretability", "interactivity", "improvisation", "approve", "reject", "out_of_scope"]

ROUTER_SYSTEM = """You route a supply-chain planner's message to ONE agent. Reply with JSON {"route": <name>}.
- insight: asks about the current state / status / overview ("where do we stand?")
- interpretability: asks WHY the plan does something, or why an option was NOT chosen
- interactivity: a HYPOTHETICAL what-if ("what if...", "can we...", "suppose...") to explore, not to adopt
- improvisation: reports a REAL change that should update the plan ("S3 is down for 5 weeks, update the plan",
  "new policy: ...", "apply that scenario", "I accept a 5% gap for faster solves")
- approve: approves a pending change ("yes", "approve", "go ahead")
- reject: rejects a pending change ("no", "cancel")
- out_of_scope: unrelated to the fulfillment plan"""

ROUTER_SHOTS = [
    ("Where do we stand as of today?", "insight"),
    ("Why is d12 docked at DC3?", "interpretability"),
    ("Why isn't S1 used for d7?", "interpretability"),
    ("What if supplier S2 becomes unavailable?", "interactivity"),
    ("Supplier S2's plant just shut down for five weeks. Update the plan.", "improvisation"),
    ("Apply that scenario.", "improvisation"),
    ("yes, approve", "approve"),
    ("Write me a poem", "out_of_scope"),
]

_EVENT = re.compile(r"\b(update the plan|re-?plan|re-?optimi[sz]e|apply|adopt|commit|new policy|from now on|"
                    r"is (now )?down|went down|has gone down|shut (down)?|is offline|confirmed|effective)\b")
_HYPO = re.compile(r"^\s*(what if|what happens|can |could |suppose|is there|would )")


class RouterAgent(Agent):
    name = "Router"

    def run(self, q: str, trace: Trace, has_pending: bool) -> str:
        t0 = time.time()
        route, by = None, "rules"
        if self.llm:
            try:
                msgs = [{"role": "system", "content": ROUTER_SYSTEM}]
                for u, r in ROUTER_SHOTS:
                    msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": json.dumps({"route": r})}]
                msgs.append({"role": "user", "content": q})
                route = json.loads(_extract_json(self.llm.chat(msgs))).get("route")
                by = getattr(self.llm, "model", "llm")
            except Exception:
                route = None
        if route not in ROUTES:
            route, by = self._rules(q, has_pending), "rules"
        if route in ("approve", "reject") and not has_pending:
            route = "out_of_scope" if not _EVENT.search(q.lower()) else "improvisation"
        trace.add(self.name, f"route -> {route}", {"by": by}, t0)
        return route

    def _rules(self, q: str, has_pending: bool) -> str:
        s = q.lower().strip()
        if has_pending and re.fullmatch(r"(yes|y|ok|okay|approve[d]?|go ahead|do it|confirm)[.! ]*.*", s):
            return "approve"
        if has_pending and re.fullmatch(r"(no|n|reject|cancel|don'?t|stop)[.! ]*.*", s):
            return "reject"
        if _EVENT.search(s) and not _HYPO.search(s):
            return "improvisation"
        a = RuleParser().parse(q, self.state.inst)
        return {"Insight": "insight", "Interpretability": "interpretability",
                "Interactivity": "interactivity", "Improvisation": "improvisation"}.get(principle_of(a), "out_of_scope")


class Handoff(Exception):
    """Raised when a worker finds the request belongs to another 4I agent."""

    def __init__(self, route, action):
        self.route, self.action = route, action


class _Pipeline(Agent):
    """Agents that use the Planner → Executor → Validator → Explainer pipeline."""

    def __init__(self, state, llm=None, narrate=True):
        super().__init__(state, llm)
        self.planner = PlannerAgent(state, llm)
        self.executor = ExecutorAgent(state)
        self.validator = ValidatorAgent(state)
        self.explainer = ExplainerAgent(state, llm if narrate else None, self.validator)

    def plan(self, q, trace, action=None, allowed=()):
        action = action or self.planner.run(q, trace)
        if action["action"] == "out_of_scope":
            raise Handoff("out_of_scope", action)
        p = principle_of(action).lower()
        if allowed and p not in allowed:
            raise Handoff(p, action)
        return action


class InsightAgent(_Pipeline):
    name = "Insight"

    def run(self, q, trace, action=None) -> Result:
        t0 = time.time()
        c = self.state.core
        facts = c.insight()
        if c.changelog:
            facts["recent_changes"] = [e["change"] for e in c.changelog[-3:]]
        trace.add(self.name, "compiled state snapshot", {"cost": facts["plan_cost"], "unfulfilled": facts["n_unfulfilled"]}, t0)
        tmpl = c._tmpl_status(facts) + (f" Recent committed changes: {'; '.join(facts['recent_changes'])}."
                                        if facts.get("recent_changes") else "")
        return Result(q, "insight", self.explainer.run(q, facts, tmpl, trace), facts, trace)


class InterpretabilityAgent(_Pipeline):
    name = "Interpretability"

    def run(self, q, trace, action=None) -> Result:
        action = self.plan(q, trace, action, allowed=("interpretability",))
        c, a = self.state.core, action["args"]
        if action["action"] == "explain_assignment":
            t0 = time.time()
            facts = c.explain(a["demand"])
            trace.add(self.name, "collected model facts", {"demand": a["demand"]}, t0)
            tmpl = c._tmpl_explain(facts)
        else:  # why_not_*: counterfactual — force the alternative, re-solve, compare
            trace.add(self.name, "running counterfactual", {"force": a})
            sc, sol = self.executor.run(action, trace)
            self.validator.check_plan(sc, sol, trace)
            facts = c.compare(c.base, sol, sc)
            alt = a.get("supplier") or a.get("datacenter")
            if sol.objective is None:
                tmpl = (f"{alt} is not used for {a['demand']} because it is not possible: "
                        + "; ".join(facts.get("violated_requirements", [])) + ".")
            else:
                tmpl = (f"{alt} could serve {a['demand']}, but the plan would cost {facts['scenario_cost']} instead of "
                        f"{facts['baseline_cost']} (+{facts['cost_change']}), changing {facts['n_changed_assignments']} "
                        f"assignment(s). The current choice is the cheaper feasible option.")
        return Result(q, "interpretability", self.explainer.run(q, facts, tmpl, trace), facts, trace)


class InteractivityAgent(_Pipeline):
    name = "Interactivity"

    def run(self, q, trace, action=None) -> Result:
        action = self.plan(q, trace, action, allowed=("interactivity", "improvisation"))
        sc, sol = self.executor.run(action, trace)
        check = self.validator.check_plan(sc, sol, trace)
        c = self.state.core
        c.last = (sc, sol)                       # so the planner can later say "apply that"
        facts = c.compare(c.base, sol, sc) | {"validation": check}
        tmpl = c._tmpl_whatif(action["action"], action["args"], facts)
        if check["feasible"] and not check["ok"]:
            tmpl += " [Validator flagged: " + "; ".join(check["issues"][:3]) + "]"
        text = self.explainer.run(q, facts, tmpl, trace) + "  (What-if only; say 'apply that' to adopt it.)"
        return Result(q, "interactivity", text, facts, trace)


class ImprovisationAgent(_Pipeline):
    """Event-triggered re-optimisation with a safety gate (Sec. 3.4 / 4.4 / 5.3).

    Small, validated changes are auto-applied; anything costly or infeasible needs human approval.
    """
    name = "Improvisation"
    AUTO_APPROVE_PCT = 5.0

    def run(self, q, trace, action=None) -> Result:
        c = self.state.core
        if action is None and re.search(r"\b(apply|adopt)\b.*\b(that|this|it|scenario|last)\b", q.lower()) and c.last:
            sc, sol = c.last
            trace.add(self.name, "adopting last what-if", {"edit": sc.note})
        else:
            action = self.plan(q, trace, action, allowed=("interactivity", "improvisation"))
            sc, sol = self.executor.run(action, trace)
        check = self.validator.check_plan(sc, sol, trace)
        facts = c.compare(c.base, sol, sc) | {"validation": check}
        if not check["feasible"] or not check["ok"]:
            trace.add(self.name, "change blocked", {"reason": "infeasible or failed validation"})
            tmpl = c._tmpl_whatif("", {}, facts) + " Not applied."
            return Result(q, "improvisation", self.explainer.run(q, facts, tmpl, trace), facts, trace, status="rejected")
        pct = abs(facts.get("cost_change_pct") or 0)
        proposal = {"scenario": (sc, sol), "reason": q, "edit": sc.note, "cost_change_pct": facts["cost_change_pct"]}
        if pct <= self.AUTO_APPROVE_PCT:
            entry = self.commit(proposal, trace, auto=True)
            tmpl = c._tmpl_whatif("", {}, facts) + f" Auto-applied (impact {pct:.1f}% ≤ {self.AUTO_APPROVE_PCT}%). Logged."
            facts["changelog_entry"] = entry
            return Result(q, "improvisation", self.explainer.run(q, facts, tmpl, trace), facts, trace)
        self.state.pending = proposal
        trace.add(self.name, "awaiting human approval", {"impact_pct": pct})
        tmpl = (c._tmpl_whatif("", {}, facts) + f" This changes cost by {facts['cost_change_pct']}%, above the "
                f"{self.AUTO_APPROVE_PCT}% auto-apply limit. Approve? (yes / no)")
        return Result(q, "improvisation", self.explainer.run(q, facts, tmpl, trace), facts, trace,
                      status="needs_approval", proposal={"edit": sc.note, "cost_change_pct": facts["cost_change_pct"]})

    def commit(self, proposal, trace, auto=False) -> dict:
        c = self.state.core
        c.last = proposal["scenario"]
        entry = c.commit(("[auto] " if auto else "[approved] ") + proposal["reason"])
        trace.add(self.name, "committed new baseline", {"edit": entry["change"], "new_cost": round(entry["new_cost"], 2)})
        return entry
