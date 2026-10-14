"""MILP from Section 5.2.1, eqs. (1)-(7), plus scenario edits and diagnostics.

    min  sum_d [ sum_{l in L(d)} sum_t c_{d,t} z_{d,l,t}
               + u_d (1 - sum_{l,t} z_{d,l,t})
               + sum_{s in S(d)} h_{d,s} w_{d,s} ]
    s.t. (2) sum_{l,t} z_{d,l,t} <= 1                       for all d
         (3) sum_{l,t} z_{d,l,t} =  sum_s w_{d,s}             for all d
         (4) sum_{d: l in L(d)} z_{d,l,t} <= delta_{l,t}     for all l,t
         (5) sum_{d: l in L(d)} sum_t z_{d,l,t} <= rho_l     for all l
         (6) sum_{d: s in S(d)} w_{d,s} <= sigma_s           for all s
         (7) z, w binary

Scenario edits (the "model edits" in Table 2) are applied either to the
Instance data (capacities, costs) or as extra constraints via `Edits`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import pulp

from .instance import Instance

BIG = 1e6


@dataclass
class Edits:
    """Constraint-level edits layered on top of eqs. (1)-(7)."""
    must_fulfill: set[str] = field(default_factory=set)          # impose (2) at equality for d
    forced_supplier: dict[str, str] = field(default_factory=dict)  # w_{d,s} = 1
    forbidden_supplier: set[tuple[str, str]] = field(default_factory=set)  # w_{d,s} = 0
    forced_dc: dict[str, str] = field(default_factory=dict)      # sum_t z_{d,l,t} = 1
    forbidden_dc: set[tuple[str, str]] = field(default_factory=set)
    dock_window: dict[str, tuple[int, int]] = field(default_factory=dict)  # z_{d,l,t}=0 outside [lo,hi]

    def describe(self) -> list[str]:
        out = []
        out += [f"(2) at equality for {d}" for d in sorted(self.must_fulfill)]
        out += [f"w[{d},{s}] = 1" for d, s in self.forced_supplier.items()]
        out += [f"w[{d},{s}] = 0" for d, s in sorted(self.forbidden_supplier)]
        out += [f"{d} docked at {l}" for d, l in self.forced_dc.items()]
        out += [f"{d} not docked at {l}" for d, l in sorted(self.forbidden_dc)]
        out += [f"{d} docks in days [{lo},{hi}]" for d, (lo, hi) in self.dock_window.items()]
        return out


@dataclass
class Solution:
    status: str
    objective: float | None
    assignment: dict[str, tuple[str, int, str] | None]   # d -> (dc, day, supplier) or None
    cost_breakdown: dict[str, float]
    solve_seconds: float
    binding: dict[str, list] = field(default_factory=dict)
    infeasible_edits: list[str] = field(default_factory=list)

    @property
    def fulfilled(self) -> list[str]:
        return [d for d, a in self.assignment.items() if a is not None]


def _feasible_days(inst: Instance, edits: Edits, d: str) -> list[int]:
    lo, hi = edits.dock_window.get(d, (min(inst.days), max(inst.days)))
    return [t for t in inst.days if lo <= t <= hi]


ANCHOR_EPS = 1e-5  # tie-break reward for keeping a baseline assignment (plan stability)


def build(inst: Instance, edits: Edits | None = None, elastic: bool = False, anchor: dict | None = None):
    """Return (problem, z, w, slacks). With elastic=True, hard edits get penalised slacks
    so we can tell *which* edit makes the model infeasible."""
    edits = edits or Edits()
    prob = pulp.LpProblem("fulfillment", pulp.LpMinimize)
    z, w = {}, {}
    for d in inst.demands:
        Ld = [l for l in inst.compat_dc[d] if (d, l) not in edits.forbidden_dc]
        for l in Ld:
            for t in _feasible_days(inst, edits, d):
                z[d, l, t] = pulp.LpVariable(f"z_{d}_{l}_{t}", cat="Binary")
        for s in inst.compat_sup[d]:
            if (d, s) not in edits.forbidden_supplier:
                w[d, s] = pulp.LpVariable(f"w_{d}_{s}", cat="Binary")

    zd = {d: [v for (dd, _, _), v in z.items() if dd == d] for d in inst.demands}
    wd = {d: [v for (dd, _), v in w.items() if dd == d] for d in inst.demands}

    slacks = {}

    def hard(name, expr_lhs, sense, rhs):
        """Add a hard edit constraint; elastic version adds a penalised slack."""
        if elastic:
            s = pulp.LpVariable(f"slack_{name}", lowBound=0)
            slacks[name] = s
            if sense == "==":
                s2 = pulp.LpVariable(f"slackn_{name}", lowBound=0)
                slacks[name + "_neg"] = s2
                prob.addConstraint(expr_lhs + s - s2 == rhs, name)
            elif sense == ">=":
                prob.addConstraint(expr_lhs + s >= rhs, name)
        else:
            if sense == "==":
                prob.addConstraint(expr_lhs == rhs, name)
            else:
                prob.addConstraint(expr_lhs >= rhs, name)

    # objective (1)
    obj = (pulp.lpSum(inst.dock_cost[d][t] * v for (d, l, t), v in z.items())
           + pulp.lpSum(inst.penalty[d] * (1 - pulp.lpSum(zd[d])) for d in inst.demands)
           + pulp.lpSum(inst.ship_cost[d][s] * v for (d, s), v in w.items()))
    if anchor:  # among (near-)optimal plans, prefer the one closest to the baseline plan
        keep = []
        for d, a in anchor.items():
            if a:
                l, t, s_ = a
                if (d, l, t) in z:
                    keep.append(z[d, l, t])
                if (d, s_) in w:
                    keep.append(w[d, s_])
        obj = obj - ANCHOR_EPS * pulp.lpSum(keep)
    prob += obj

    for d in inst.demands:
        prob += pulp.lpSum(zd[d]) <= 1, f"c2_{d}"
        prob += pulp.lpSum(zd[d]) == pulp.lpSum(wd[d]), f"c3_{d}"
    for l in inst.datacenters:
        for t in inst.days:
            terms = [v for (d, ll, tt), v in z.items() if ll == l and tt == t]
            if terms:
                prob += pulp.lpSum(terms) <= inst.throughput[l][t], f"c4_{l}_{t}"
        terms = [v for (d, ll, tt), v in z.items() if ll == l]
        if terms:
            prob += pulp.lpSum(terms) <= inst.rows[l], f"c5_{l}"
    for s in inst.suppliers:
        terms = [v for (d, ss), v in w.items() if ss == s]
        if terms:
            prob += pulp.lpSum(terms) <= inst.inventory[s], f"c6_{s}"

    # ---- edits ----
    for d in edits.must_fulfill:
        hard(f"must_{d}", pulp.lpSum(zd[d]), ">=", 1)
    for d, s in edits.forced_supplier.items():
        hard(f"forcesup_{d}_{s}", w.get((d, s), 0) + 0 * pulp.lpSum(wd[d]), ">=", 1)
    for d, l in edits.forced_dc.items():
        hard(f"forcedc_{d}_{l}", pulp.lpSum(v for (dd, ll, _), v in z.items() if dd == d and ll == l), ">=", 1)

    if elastic and slacks:
        prob.setObjective(obj + BIG * pulp.lpSum(slacks.values()))
    return prob, z, w, slacks


def _solver(time_limit: float | None = None, gap: float | None = None):
    kw = {"msg": False}
    if time_limit:
        kw["timeLimit"] = time_limit
    if gap is not None:
        kw["gapRel"] = gap
    return pulp.HiGHS(**kw)


def solve(inst: Instance, edits: Edits | None = None, time_limit: float | None = 60,
          gap: float | None = None, diagnose: bool = True, anchor: dict | None = None) -> Solution:
    """anchor: baseline assignment {d: (l,t,s)|None}; adds a tiny tie-break toward it."""
    edits = edits or Edits()
    if anchor and gap is None:
        gap = 1e-7  # tie-break must not be swamped by the default MIP gap
    prob, z, w, _ = build(inst, edits, anchor=anchor)
    t0 = time.time()
    prob.solve(_solver(time_limit, gap))
    secs = time.time() - t0
    status = pulp.LpStatus[prob.status]
    if status != "Optimal":
        bad = diagnose_infeasibility(inst, edits) if diagnose else []
        return Solution(status, None, {d: None for d in inst.demands}, {}, secs, infeasible_edits=bad)

    assign = {d: None for d in inst.demands}
    for (d, l, t), v in z.items():
        if v.value() > 0.5:
            s = next(ss for (dd, ss), vv in w.items() if dd == d and vv.value() > 0.5)
            assign[d] = (l, t, s)
    breakdown = cost_breakdown(inst, assign)
    sol = Solution(status, breakdown["total"], assign, breakdown, secs)  # true cost, excludes tie-break
    sol.binding = binding_constraints(inst, assign)
    return sol


def cost_breakdown(inst: Instance, assign) -> dict[str, float]:
    dock = sum(inst.dock_cost[d][a[1]] for d, a in assign.items() if a)
    miss = sum(inst.penalty[d] for d, a in assign.items() if not a)
    ship = sum(inst.ship_cost[d][a[2]] for d, a in assign.items() if a)
    late = sum(1 for d, a in assign.items() if a and a[1] > inst.ideal_day[d])
    early = sum(1 for d, a in assign.items() if a and a[1] < inst.ideal_day[d])
    return {"delay_idle_cost": round(dock, 4), "missed_demand_cost": round(miss, 4),
            "shipping_cost": round(ship, 4), "total": round(dock + miss + ship, 4),
            "n_fulfilled": sum(1 for a in assign.values() if a), "n_unfulfilled": sum(1 for a in assign.values() if not a),
            "n_late": late, "n_early": early}


def binding_constraints(inst: Instance, assign) -> dict[str, list]:
    """Constraints (4)-(6) with zero slack in the incumbent plan."""
    used_lt, used_l, used_s = {}, {}, {}
    for d, a in assign.items():
        if a:
            l, t, s = a
            used_lt[l, t] = used_lt.get((l, t), 0) + 1
            used_l[l] = used_l.get(l, 0) + 1
            used_s[s] = used_s.get(s, 0) + 1
    return {
        "throughput_(4)": [(l, t) for (l, t), u in used_lt.items() if u >= inst.throughput[l][t]],
        "rows_(5)": [l for l, u in used_l.items() if u >= inst.rows[l]],
        "inventory_(6)": [s for s, u in used_s.items() if u >= inst.inventory[s]],
    }


def diagnose_infeasibility(inst: Instance, edits: Edits) -> list[str]:
    """Elastic re-solve: which hard edits cannot be satisfied together (IIS-lite)."""
    prob, _, _, slacks = build(inst, edits, elastic=True)
    prob.solve(_solver(60))
    return [name for name, s in slacks.items() if s.value() and s.value() > 1e-6]


def marginal_values(inst: Instance, base: Solution, top_k: int = 5) -> list[dict]:
    """Cost reduction from +1 unit on each binding capacity (MILP-honest 'shadow price')."""
    out = []
    cand = ([("rows", l) for l in base.binding.get("rows_(5)", [])]
            + [("inventory", s) for s in base.binding.get("inventory_(6)", [])])
    for kind, key in cand[: top_k * 2]:
        alt = inst.copy()
        if kind == "rows":
            alt.rows[key] += 1
        else:
            alt.inventory[key] += 1
        s2 = solve(alt, diagnose=False)
        if s2.objective is not None:
            out.append({"capacity": f"{kind}[{key}]", "saving_per_unit": round(base.objective - s2.objective, 3)})
    return sorted(out, key=lambda r: -r["saving_per_unit"])[:top_k]
