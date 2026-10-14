"""Catalog of supported planner queries ("defined catalog of high-value queries", Sec. 5.4).

The LLM never writes solver code. It maps a question to ONE action from this
catalog with JSON arguments; deterministic Python applies the edit. This is the
OptiGuide-style division of labour the paper describes: GenAI translates, the
solver verifies and certifies.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..milp.instance import Instance
from ..milp.model import Edits, Solution

# name -> (description for the prompt, {arg: type/description}, 4I principle)
ACTIONS: dict[str, tuple[str, dict[str, str], str]] = {
    "status_summary": ("Summarise current state: demand, capacity, loads, bottlenecks.", {}, "Insight"),
    "explain_assignment": ("Explain why demand d got its current data center / day / supplier (or why it is unfulfilled).",
                           {"demand": "demand id, e.g. d7"}, "Interpretability"),
    "why_not_supplier": ("Explain why supplier s is NOT used for demand d (counterfactual: force it and re-solve).",
                         {"demand": "demand id", "supplier": "supplier id, e.g. S2"}, "Interpretability"),
    "why_not_datacenter": ("Explain why data center l is NOT used for demand d (counterfactual: force it and re-solve).",
                           {"demand": "demand id", "datacenter": "data center id, e.g. DC3"}, "Interpretability"),
    "dock_earlier": ("Can demand d dock at least N days earlier than currently planned? (Table 2, row 1)",
                     {"demand": "demand id", "days": "int, default 7 (one week)"}, "Interactivity"),
    "force_supplier": ("What if demand d must be fulfilled by supplier s? (sets w[d,s]=1)",
                       {"demand": "demand id", "supplier": "supplier id"}, "Interactivity"),
    "supplier_unavailable": ("What if supplier s becomes unavailable? (sigma_s = 0; Table 2, row 2)",
                             {"supplier": "supplier id"}, "Interactivity"),
    "set_inventory": ("What if supplier s has a different inventory level?",
                      {"supplier": "supplier id", "value": "int, new inventory"}, "Interactivity"),
    "scale_throughput": ("What if docking throughput at data center l changes by a factor (e.g. drops 50% -> 0.5)? "
                         "Keeps all currently fulfilled demands required (Table 2, row 3).",
                         {"datacenter": "data center id", "factor": "float, e.g. 0.5",
                          "start_day": "int, first affected day (default first day)",
                          "end_day": "int, last affected day (default last day)"}, "Interactivity"),
    "datacenter_unavailable": ("What if data center l cannot take any new racks? (rho_l = 0)",
                               {"datacenter": "data center id"}, "Interactivity"),
    "set_rows": ("What if data center l has a different number of free rows?",
                 {"datacenter": "data center id", "value": "int"}, "Interactivity"),
    "scale_shipping_cost": ("What if shipping costs change by a factor (optionally for one supplier only)?",
                            {"factor": "float, e.g. 2.0 for double", "supplier": "optional supplier id or null"},
                            "Interactivity"),
    "set_solver_params": ("Change solver settings, e.g. accept a relative optimality gap or a time limit.",
                          {"gap": "float, e.g. 0.05 for 5%", "time_limit": "seconds or null"}, "Improvisation"),
    "out_of_scope": ("The question cannot be answered with the catalog.", {"reason": "short text"}, "-"),
}


@dataclass
class Scenario:
    inst: Instance
    edits: Edits
    solver: dict
    note: str


class ActionError(ValueError):
    pass


def _need(args, key, valid=None):
    if key not in args or args[key] in (None, ""):
        raise ActionError(f"missing argument '{key}'")
    v = args[key]
    if valid is not None and v not in valid:
        raise ActionError(f"unknown {key} '{v}'")
    return v


def validate(action: dict, inst: Instance) -> dict:
    """Normalise and validate an action dict produced by a parser. Raises ActionError."""
    if not isinstance(action, dict) or "action" not in action:
        raise ActionError("expected {'action': ..., 'args': {...}}")
    name = action["action"]
    if name not in ACTIONS:
        raise ActionError(f"unknown action '{name}'")
    args = dict(action.get("args") or {})
    D, S, L = set(inst.demands), set(inst.suppliers), set(inst.datacenters)
    for k in list(args):
        if isinstance(args[k], str):
            a = args[k].strip()
            if k == "demand":
                a = a.lower()
            if k in ("supplier", "datacenter"):
                a = a.upper()
            args[k] = a
    if "demand" in ACTIONS[name][1]:
        _need(args, "demand", D)
    if "supplier" in ACTIONS[name][1] and name != "scale_shipping_cost":
        _need(args, "supplier", S)
    if name == "scale_shipping_cost" and args.get("supplier") not in (None, "", "NULL", "NONE"):
        _need(args, "supplier", S)
    elif name == "scale_shipping_cost":
        args["supplier"] = None
    if "datacenter" in ACTIONS[name][1]:
        _need(args, "datacenter", L)
    if name == "dock_earlier":
        args["days"] = int(args.get("days") or 7)
    if name == "scale_throughput":
        args["factor"] = float(_need(args, "factor"))
        args["start_day"] = int(args.get("start_day") or min(inst.days))
        args["end_day"] = int(args.get("end_day") or max(inst.days))
    if name == "scale_shipping_cost":
        args["factor"] = float(_need(args, "factor"))
    if name in ("set_inventory", "set_rows"):
        args["value"] = int(_need(args, "value"))
    return {"action": name, "args": args}


def apply(action: dict, inst: Instance, base_edits: Edits, base_sol: Solution, solver: dict) -> Scenario | None:
    """Turn a validated action into a scenario (model edit). Returns None for explain-only actions."""
    name, a = action["action"], action["args"]
    new = inst.copy()
    e = Edits(set(base_edits.must_fulfill), dict(base_edits.forced_supplier), set(base_edits.forbidden_supplier),
              dict(base_edits.forced_dc), set(base_edits.forbidden_dc), dict(base_edits.dock_window))
    sv = dict(solver)

    if name in ("status_summary", "explain_assignment", "out_of_scope"):
        return None
    if name == "why_not_supplier" or name == "force_supplier":
        e.forced_supplier[a["demand"]] = a["supplier"]
        e.must_fulfill.add(a["demand"])
        note = f"w[{a['demand']},{a['supplier']}] = 1"
    elif name == "why_not_datacenter":
        e.forced_dc[a["demand"]] = a["datacenter"]
        e.must_fulfill.add(a["demand"])
        note = f"{a['demand']} docked at {a['datacenter']}"
    elif name == "dock_earlier":
        d = a["demand"]
        cur = base_sol.assignment.get(d)
        ref = cur[1] if cur else inst.ideal_day[d]
        hi = ref - a["days"]
        e.dock_window[d] = (min(inst.days), hi)
        e.must_fulfill.add(d)
        note = f"restrict {d} to days <= {hi} and impose (2) at equality"
    elif name == "supplier_unavailable":
        new.inventory[a["supplier"]] = 0
        note = f"sigma[{a['supplier']}] = 0"
    elif name == "set_inventory":
        new.inventory[a["supplier"]] = a["value"]
        note = f"sigma[{a['supplier']}] = {a['value']}"
    elif name == "scale_throughput":
        l = a["datacenter"]
        for t in new.days:
            if a["start_day"] <= t <= a["end_day"]:
                new.throughput[l][t] = int(new.throughput[l][t] * a["factor"])  # floor: racks are integral
        e.must_fulfill |= set(base_sol.fulfilled)
        note = (f"delta[{l},t] *= {a['factor']} for t in [{a['start_day']},{a['end_day']}]; "
                f"(2) at equality for the {len(base_sol.fulfilled)} currently fulfilled demands")
    elif name == "datacenter_unavailable":
        new.rows[a["datacenter"]] = 0
        note = f"rho[{a['datacenter']}] = 0"
    elif name == "set_rows":
        new.rows[a["datacenter"]] = a["value"]
        note = f"rho[{a['datacenter']}] = {a['value']}"
    elif name == "scale_shipping_cost":
        for d in new.demands:
            for s in new.ship_cost[d]:
                if a["supplier"] in (None, s):
                    new.ship_cost[d][s] *= a["factor"]
        note = f"h[d,{a['supplier'] or 's'}] *= {a['factor']}"
    elif name == "set_solver_params":
        if a.get("gap") is not None:
            sv["gap"] = float(a["gap"])
        if a.get("time_limit") is not None:
            sv["time_limit"] = float(a["time_limit"])
        note = f"solver params {sv}"
    else:  # pragma: no cover
        raise ActionError(name)
    return Scenario(new, e, sv, note)
