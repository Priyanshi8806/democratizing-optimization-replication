"""Orchestrator: routes each message, handles hand-offs and the approval loop."""
from __future__ import annotations

from .base import Result, SharedState, Trace
from .principals import (Handoff, ImprovisationAgent, InsightAgent, InteractivityAgent,
                         InterpretabilityAgent, RouterAgent)
from ..milp.instance import Instance

OUT_OF_SCOPE = ("That's outside what the fulfillment model can answer. I can summarise status, explain "
                "assignments, run what-ifs, and apply real changes to the plan.")


class MultiAgentSystem:
    def __init__(self, inst: Instance, llm=None, narrate: bool = True):
        """llm: an LLMParser (Ollama/Qwen/Llama) or None for a fully deterministic run."""
        self.state = SharedState(inst)
        self.router = RouterAgent(self.state, llm)
        self.agents = {
            "insight": InsightAgent(self.state, llm, narrate),
            "interpretability": InterpretabilityAgent(self.state, llm, narrate),
            "interactivity": InteractivityAgent(self.state, llm, narrate),
            "improvisation": ImprovisationAgent(self.state, llm, narrate),
        }

    @property
    def changelog(self):
        return self.state.core.changelog

    def ask(self, q: str) -> Result:
        trace = Trace()
        route = self.router.run(q, trace, has_pending=self.state.pending is not None)
        res = self._dispatch(q, route, trace)
        self.state.history.append({"q": q, "route": res.route, "status": res.status})
        return res

    def _dispatch(self, q, route, trace, action=None, hops=0) -> Result:
        if route == "approve":
            p, self.state.pending = self.state.pending, None
            entry = self.agents["improvisation"].commit(p, trace)
            return Result(q, "improvisation", f"Approved and applied: {entry['change']}. Cost {entry['old_cost']:.2f} "
                          f"-> {entry['new_cost']:.2f}. Logged in change log.", {"changelog_entry": entry}, trace)
        if route == "reject":
            p, self.state.pending = self.state.pending, None
            trace.add("Improvisation", "proposal discarded", {"edit": p["edit"]})
            return Result(q, "improvisation", f"Discarded: {p['edit']}. Plan unchanged.", {}, trace, status="rejected")
        if route == "out_of_scope":
            return Result(q, "out_of_scope", OUT_OF_SCOPE, {}, trace, status="out_of_scope")
        try:
            return self.agents[route].run(q, trace, action)
        except Handoff as h:
            if hops >= 1 or h.route == "out_of_scope":
                reason = h.action.get("args", {}).get("reason", "")
                msg = (f"I couldn't map that to the model: {reason}. Valid ids: suppliers "
                       f"{', '.join(self.state.inst.suppliers)}; data centers {', '.join(self.state.inst.datacenters)}; "
                       f"demands d1-d{len(self.state.inst.demands)}." if "unknown" in reason or "missing" in reason
                       else OUT_OF_SCOPE)
                return Result(q, "out_of_scope", msg, {"reason": reason}, trace, status="out_of_scope")
            trace.add(route.capitalize(), f"hand-off -> {h.route}", {"action": h.action["action"]})
            return self._dispatch(q, h.route, trace, h.action, hops + 1)
