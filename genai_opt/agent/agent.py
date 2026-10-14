"""4I assistant: Insight, Interpretability, Interactivity, Improvisation (Sec. 3-5).

    question --parser--> catalog action --apply--> model edit --HiGHS--> solution
             --facts--> grounded explanation (template, optionally LLM-narrated + fidelity-checked)

Explanations are built from solver artifacts (cost deltas, reassignments,
binding constraints, elastic infeasibility diagnosis). If an LLM narrates them,
`fidelity` checks that every number in the narration appears in the facts
(a simple version of the 'explanation fidelity' metric in Table 1).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from .catalog import ACTIONS, ActionError, Scenario, apply, validate
from ..milp.instance import Instance
from ..milp.model import Edits, Solution, marginal_values, solve


@dataclass
class Answer:
    question: str
    action: dict
    principle: str
    text: str
    facts: dict
    scenario_note: str | None = None
    solution: Solution | None = None
    timings: dict = field(default_factory=dict)
    fidelity: float | None = None


class Assistant:
    def __init__(self, inst: Instance, parser, narrator=None):
        self.inst, self.parser, self.narrator = inst, parser, narrator
        self.edits = Edits()
        self.solver = {"time_limit": 60, "gap": None}
        self.base = solve(inst, self.edits, **self.solver)
        self.changelog: list[dict] = []           # Improvisation: traceability of committed changes
        self.last: tuple[Scenario, Solution] | None = None

    # ------------------------------------------------------------------ main entry
    def ask(self, q: str) -> Answer:
        t0 = time.time()
        try:
            action = validate(self.parser.parse(q, self.inst), self.inst)
        except ActionError as e:
            action = {"action": "out_of_scope", "args": {"reason": str(e)}}
        t_parse = time.time() - t0
        name = action["action"]
        principle = ACTIONS[name][2]

        if name == "out_of_scope":
            facts = {"reason": action["args"].get("reason")}
            text = ("That question is outside what I can answer from the fulfillment model. I can summarise status, "
                    "explain assignments, and run what-ifs on suppliers, data centers, throughput, costs, and dock dates.")
            return Answer(q, action, principle, text, facts, timings={"parse": t_parse})
        if name == "status_summary":
            facts = self.insight()
            return Answer(q, action, principle, self._render(q, facts, self._tmpl_status(facts)), facts,
                          timings={"parse": t_parse})
        if name == "explain_assignment":
            facts = self.explain(action["args"]["demand"])
            return Answer(q, action, principle, self._render(q, facts, self._tmpl_explain(facts)), facts,
                          timings={"parse": t_parse})

        sc = apply(action, self.inst, self.edits, self.base, self.solver)
        t1 = time.time()
        sol = solve(sc.inst, sc.edits, time_limit=sc.solver.get("time_limit"), gap=sc.solver.get("gap"),
                    anchor=self.base.assignment)
        t_solve = time.time() - t1
        self.last = (sc, sol)
        facts = self.compare(self.base, sol, sc)
        facts["question_type"] = name
        tmpl = self._tmpl_whatif(name, action["args"], facts)
        ans = Answer(q, action, principle, "", facts, sc.note, sol, {"parse": t_parse, "solve": t_solve})
        ans.text = self._render(q, facts, tmpl, ans)
        return ans

    # ------------------------------------------------------------------ Improvisation
    def commit(self, reason: str = "") -> dict:
        """Adopt the last what-if as the new baseline (event-triggered re-optimisation), with a change log."""
        if not self.last:
            raise RuntimeError("nothing to commit")
        sc, sol = self.last
        if sol.objective is None:
            raise RuntimeError("cannot commit an infeasible scenario")
        entry = {"change": sc.note, "reason": reason, "old_cost": self.base.objective, "new_cost": sol.objective,
                 "time": time.strftime("%Y-%m-%d %H:%M:%S")}
        self.inst, self.edits, self.solver, self.base = sc.inst, sc.edits, sc.solver, sol
        # demands that were required only as a what-if check should not stay hard-wired
        self.edits.must_fulfill = set()
        self.changelog.append(entry)
        self.last = None
        return entry

    # ------------------------------------------------------------------ facts
    def insight(self) -> dict:
        s = self.inst.summary()
        b = self.base
        return {"plan_cost": round(b.objective, 2), **{k: v for k, v in b.cost_breakdown.items()},
                "n_demands": s["n_demands"], "total_rows": s["total_rows"], "total_inventory": s["total_inventory"],
                "saturated_datacenters_rows": b.binding["rows_(5)"],
                "suppliers_at_inventory_limit": b.binding["inventory_(6)"],
                "n_saturated_dock_days": len(b.binding["throughput_(4)"]),
                "top_capacity_values": marginal_values(self.inst, b, top_k=3)}

    def explain(self, d: str) -> dict:
        inst, a = self.inst, self.base.assignment[d]
        f = {"demand": d, "ideal_day": inst.ideal_day[d], "penalty_if_unfulfilled": inst.penalty[d],
             "compatible_datacenters": inst.compat_dc[d], "compatible_suppliers": inst.compat_sup[d],
             "shipping_cost_by_supplier": inst.ship_cost[d], **inst.meta.get("demand", {}).get(d, {})}
        if a:
            l, t, s = a
            f.update({"assigned_dc": l, "assigned_day": t, "assigned_supplier": s,
                      "dock_cost": inst.dock_cost[d][t], "shipping_cost": inst.ship_cost[d][s],
                      "days_late": max(0, t - inst.ideal_day[d]), "days_early": max(0, inst.ideal_day[d] - t),
                      "cheapest_supplier": min(inst.ship_cost[d], key=inst.ship_cost[d].get)})
            if f["cheapest_supplier"] != s:
                f["cheapest_supplier_at_inventory_limit"] = f["cheapest_supplier"] in self.base.binding["inventory_(6)"]
            if t != inst.ideal_day[d]:
                f["ideal_day_saturated_dcs"] = [l2 for l2 in inst.compat_dc[d]
                                                if (l2, inst.ideal_day[d]) in self.base.binding["throughput_(4)"]
                                                or l2 in self.base.binding["rows_(5)"]]
        else:
            f["assigned"] = None
            f["saturated_compatible_dcs"] = [l for l in inst.compat_dc[d] if l in self.base.binding["rows_(5)"]]
            f["saturated_compatible_suppliers"] = [s for s in inst.compat_sup[d] if s in self.base.binding["inventory_(6)"]]
        return f

    def compare(self, b: Solution, s: Solution, sc: Scenario) -> dict:
        f = {"model_edit": sc.note, "status": s.status, "solve_seconds": round(s.solve_seconds, 3),
             "baseline_cost": round(b.objective, 2)}
        if s.objective is None:
            f["violated_requirements"] = [self._humanise(x) for x in s.infeasible_edits]
            return f
        f["scenario_cost"] = round(s.objective, 2)
        f["cost_change"] = round(s.objective - b.objective, 2)
        f["cost_change_pct"] = round(100 * (s.objective - b.objective) / b.objective, 2) if b.objective else None
        for k in ("delay_idle_cost", "missed_demand_cost", "shipping_cost"):
            f[f"delta_{k}"] = round(s.cost_breakdown[k] - b.cost_breakdown[k], 2)
        f["fulfilled_before"], f["fulfilled_after"] = b.cost_breakdown["n_fulfilled"], s.cost_breakdown["n_fulfilled"]
        moved = {d: (b.assignment[d], s.assignment[d]) for d in self.inst.demands if b.assignment[d] != s.assignment[d]}
        f["n_changed_assignments"] = len(moved)
        f["newly_unfulfilled"] = [d for d, (x, y) in moved.items() if x and not y]
        f["newly_fulfilled"] = [d for d, (x, y) in moved.items() if y and not x]
        f["changed_examples"] = {d: {"before": x, "after": y} for d, (x, y) in list(moved.items())[:5]}
        f["binding_after"] = {"rows": s.binding["rows_(5)"], "inventory": s.binding["inventory_(6)"]}
        return f

    def _humanise(self, slack_name: str) -> str:
        m = re.match(r"must_(d\d+)", slack_name)
        if m:
            return f"{m.group(1)} cannot be fulfilled under this scenario"
        m = re.match(r"forcesup_(d\d+)_(S\d+)", slack_name)
        if m:
            d, s = m.groups()
            if s not in self.inst.compat_sup.get(d, []):
                return f"{s} is not a compatible supplier for {d} (hardware incompatibility)"
            return f"{d} cannot be sourced from {s} (supplier has no remaining inventory)"
        m = re.match(r"forcedc_(d\d+)_(DC\d+)", slack_name)
        if m:
            d, l = m.groups()
            if l not in self.inst.compat_dc.get(d, []):
                return f"{l} is not compatible with {d}"
            return f"{d} cannot dock at {l} (rows/throughput exhausted)"
        return slack_name

    # ------------------------------------------------------------------ templates (faithful by construction)
    @staticmethod
    def _tmpl_status(f):
        return (f"Current plan costs {f['plan_cost']} and fulfils {f['n_fulfilled']} of {f['n_demands']} demands "
                f"({f['n_unfulfilled']} unfulfilled, {f['n_late']} docked late). Row capacity is exhausted at "
                f"{', '.join(f['saturated_datacenters_rows']) or 'no data center'}; suppliers at their inventory limit: "
                f"{', '.join(f['suppliers_at_inventory_limit']) or 'none'}. "
                + (f"The most valuable extra unit of capacity is {f['top_capacity_values'][0]['capacity']} "
                   f"(saves {f['top_capacity_values'][0]['saving_per_unit']})." if f["top_capacity_values"] else ""))

    @staticmethod
    def _tmpl_explain(f):
        d = f["demand"]
        if f.get("assigned") is None and "assigned_dc" not in f:
            return (f"{d} is not fulfilled: its penalty ({f['penalty_if_unfulfilled']}) is lower than the cost of "
                    f"displacing other demands. Saturated compatible data centers: "
                    f"{', '.join(f['saturated_compatible_dcs']) or 'none'}; saturated compatible suppliers: "
                    f"{', '.join(f['saturated_compatible_suppliers']) or 'none'}.")
        t = (f"{d} docks at {f['assigned_dc']} on day {f['assigned_day']} (ideal day {f['ideal_day']}), "
             f"sourced from {f['assigned_supplier']} at shipping cost {f['shipping_cost']}.")
        if f["cheapest_supplier"] != f["assigned_supplier"]:
            t += (f" The cheapest compatible supplier is {f['cheapest_supplier']}"
                  + (", but it is at its inventory limit (constraint 6 binding)." if f.get("cheapest_supplier_at_inventory_limit")
                     else ", but using it would displace a demand that values it more."))
        if f["days_late"] or f["days_early"]:
            t += (f" It is {f['days_late']} day(s) late / {f['days_early']} day(s) early because capacity on its ideal "
                  f"day is saturated at {', '.join(f.get('ideal_day_saturated_dcs') or []) or 'its compatible data centers'}.")
        return t

    @staticmethod
    def _tmpl_whatif(name, a, f):
        if f["status"] != "Optimal" and all("cannot be fulfilled under" in v for v in f.get("violated_requirements", [])):
            v = [x.split()[0] for x in f["violated_requirements"]]
            return (f"Not feasible ({f['model_edit']}). At least {len(v)} demand(s) that must be served cannot be "
                    f"docked under this scenario (minimum shortfall from the solver's elastic re-solve), "
                    f"e.g. {', '.join(sorted(v, key=lambda x: int(x[1:]))[:6])}.")
        if f["status"] != "Optimal":
            return (f"Not feasible ({f['model_edit']}). The solver reports: "
                    + "; ".join(f.get("violated_requirements", [])[:6])
                    + (f" (+{len(f['violated_requirements']) - 6} more)" if len(f.get("violated_requirements", [])) > 6 else "") + ".")
        sign = "+" if f["cost_change"] >= 0 else ""
        t = (f"Feasible. Total cost goes from {f['baseline_cost']} to {f['scenario_cost']} ({sign}{f['cost_change']}, "
             f"{sign}{f['cost_change_pct']}%). Fulfilled demands: {f['fulfilled_before']} -> {f['fulfilled_after']}; "
             f"{f['n_changed_assignments']} assignment(s) change.")
        parts = []
        for k, lab in (("delta_shipping_cost", "shipping"), ("delta_delay_idle_cost", "delay/idle"),
                       ("delta_missed_demand_cost", "missed-demand penalty")):
            if abs(f[k]) > 1e-9:
                parts.append(f"{lab} {'+' if f[k] > 0 else ''}{f[k]}")
        if parts:
            t += " Drivers: " + ", ".join(parts) + "."
        if f["newly_unfulfilled"]:
            t += f" Newly unfulfilled: {', '.join(f['newly_unfulfilled'][:8])}."
        return t

    # ------------------------------------------------------------------ optional LLM narration
    def _render(self, q, facts, template, ans: Answer | None = None) -> str:
        if not self.narrator:
            return template
        try:
            text = self.narrator.narrate(q, {**facts, "reference_summary": template})
        except Exception:  # narration is optional; never block on it
            return template
        fid = fidelity(text, facts, template)
        if ans is not None:
            ans.fidelity = fid
        return text if fid >= 0.999 else template + "  [LLM narration rejected: unsupported numbers]"


def fidelity(text: str, facts: dict, template: str = "") -> float:
    """Share of numbers in `text` that appear in facts/template (1.0 = no invented numbers)."""
    nums = re.findall(r"-?\d+(?:\.\d+)?", text.replace(",", ""))
    if not nums:
        return 1.0
    pool = set(re.findall(r"-?\d+(?:\.\d+)?", (str(facts) + " " + template).replace(",", "")))
    pool |= {n.lstrip("-") for n in pool}
    ok = sum(1 for n in nums if n in pool or n.lstrip("-") in pool or _close(n, pool))
    return ok / len(nums)


def _close(n: str, pool: set) -> bool:
    try:
        x = float(n)
    except ValueError:
        return False
    for p in pool:
        try:
            if abs(float(p) - x) <= 0.051 * max(1, abs(x)) * 0.01 + 0.006:
                return True
        except ValueError:
            pass
    return False
