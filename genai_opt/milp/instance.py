"""Synthetic instance generator for the Section 5 fulfillment MILP.

Mirrors the notation in Simchi-Levi, Dai, Menache & Wu (Sec. 5.2.1):
  L  data centers (rho_l rows, delta_{l,t} docking throughput per day)
  S  suppliers / warehouses (sigma_s inventory)
  D  demands (one rack each), compatible sets L(d), S(d)
  T  planning days
  c_{d,t}  idle cost (dock before ideal date) or delay cost (dock after)
  u_d      penalty for not fulfilling d
  h_{d,s}  shipping cost from supplier s for demand d

Microsoft's data are proprietary; every number here is synthetic. Parameter
ranges are chosen so that capacity (rows, throughput, inventory) is tight
enough for constraints to bind and trade-offs to be non-trivial.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

REGIONS = ["US-East", "US-West", "EU-North", "Asia-SE"]
HW_TYPES = ["GPU-H", "GPU-M", "CPU-Std", "Storage"]
BIZ_GROUPS = ["Azure-Compute", "Azure-AI", "M365", "Gaming"]


@dataclass
class Instance:
    days: list[int]
    datacenters: list[str]
    suppliers: list[str]
    demands: list[str]
    rows: dict[str, int]                      # rho_l
    throughput: dict[str, dict[int, int]]     # delta_{l,t}
    inventory: dict[str, int]                 # sigma_s
    compat_dc: dict[str, list[str]]           # L(d)
    compat_sup: dict[str, list[str]]          # S(d)
    ideal_day: dict[str, int]
    penalty: dict[str, float]                 # u_d
    dock_cost: dict[str, dict[int, float]]    # c_{d,t}
    ship_cost: dict[str, dict[str, float]]    # h_{d,s}
    meta: dict = field(default_factory=dict)  # descriptive attributes (region, hw type, ...)

    # ---- IO -------------------------------------------------------------
    def to_json(self, path: str | Path) -> None:
        d = asdict(self)
        # json needs str keys
        d["throughput"] = {l: {str(t): v for t, v in tv.items()} for l, tv in self.throughput.items()}
        d["dock_cost"] = {k: {str(t): v for t, v in tv.items()} for k, tv in self.dock_cost.items()}
        Path(path).write_text(json.dumps(d, indent=1))

    @classmethod
    def from_json(cls, path: str | Path) -> "Instance":
        d = json.loads(Path(path).read_text())
        d["throughput"] = {l: {int(t): v for t, v in tv.items()} for l, tv in d["throughput"].items()}
        d["dock_cost"] = {k: {int(t): v for t, v in tv.items()} for k, tv in d["dock_cost"].items()}
        return cls(**d)

    def copy(self) -> "Instance":
        import copy
        return copy.deepcopy(self)

    def summary(self) -> dict:
        """'Insight' snapshot: load vs. capacity on sources and destinations."""
        n_d = len(self.demands)
        dc_load = {l: sum(l in self.compat_dc[d] for d in self.demands) for l in self.datacenters}
        sup_load = {s: sum(s in self.compat_sup[d] for d in self.demands) for s in self.suppliers}
        return {
            "n_demands": n_d,
            "n_datacenters": len(self.datacenters),
            "n_suppliers": len(self.suppliers),
            "horizon_days": len(self.days),
            "total_rows": sum(self.rows.values()),
            "total_inventory": sum(self.inventory.values()),
            "total_throughput": sum(sum(v.values()) for v in self.throughput.values()),
            "dc_compatible_demand_vs_rows": {l: (dc_load[l], self.rows[l]) for l in self.datacenters},
            "supplier_compatible_demand_vs_inventory": {s: (sup_load[s], self.inventory[s]) for s in self.suppliers},
            "demands_by_ideal_day": {t: sum(self.ideal_day[d] == t for d in self.demands) for t in self.days},
        }


def generate(
    n_demands: int = 60,
    n_datacenters: int = 4,
    n_suppliers: int = 3,
    n_days: int = 14,
    tightness: float = 0.9,
    idle_cost_per_day: float = 1.0,
    delay_cost_per_day: float = 4.0,
    seed: int = 0,
) -> Instance:
    """Generate a random but structured instance.

    tightness < 1 means aggregate capacity (rows, inventory) is below total demand,
    so some demand goes unfulfilled or is delayed and constraints bind.
    """
    rng = np.random.default_rng(seed)
    days = list(range(1, n_days + 1))
    L = [f"DC{i+1}" for i in range(n_datacenters)]
    S = [f"S{i+1}" for i in range(n_suppliers)]
    D = [f"d{i+1}" for i in range(n_demands)]

    dc_region = {l: REGIONS[i % len(REGIONS)] for i, l in enumerate(L)}
    sup_region = {s: REGIONS[(i * 2 + 1) % len(REGIONS)] for i, s in enumerate(S)}
    # rough distance matrix between regions -> drives shipping cost
    dist = {(a, b): (0.2 if a == b else 1.0 + 0.5 * abs(REGIONS.index(a) - REGIONS.index(b)))
            for a in REGIONS for b in REGIONS}
    sup_hw = {s: set(rng.choice(HW_TYPES, size=rng.integers(2, len(HW_TYPES) + 1), replace=False)) for s in S}
    dc_hw = {l: set(rng.choice(HW_TYPES, size=rng.integers(2, len(HW_TYPES) + 1), replace=False)) for l in L}

    meta = {"dc_region": dc_region, "sup_region": sup_region,
            "sup_hw": {s: sorted(v) for s, v in sup_hw.items()},
            "dc_hw": {l: sorted(v) for l, v in dc_hw.items()}, "demand": {}}

    compat_dc, compat_sup, ideal, pen, ccost, hcost = {}, {}, {}, {}, {}, {}
    for d in D:
        hw = str(rng.choice(HW_TYPES))
        region = str(rng.choice(REGIONS))
        group = str(rng.choice(BIZ_GROUPS))
        # compatible DCs: support the hw type; prefer requested region but allow others
        Ld = [l for l in L if hw in dc_hw[l]] or [str(rng.choice(L))]
        Sd = [s for s in S if hw in sup_hw[s]] or [str(rng.choice(S))]
        t_star = int(rng.integers(1, n_days + 1))
        compat_dc[d], compat_sup[d], ideal[d] = Ld, Sd, t_star
        pen[d] = float(round(rng.uniform(40, 80) * (1.5 if group == "Azure-AI" else 1.0), 2))
        ccost[d] = {t: float(idle_cost_per_day * (t_star - t) if t <= t_star else delay_cost_per_day * (t - t_star))
                    for t in days}
        hcost[d] = {s: float(round(5 * dist[(sup_region[s], region)] + rng.uniform(0, 1), 2)) for s in Sd}
        meta["demand"][d] = {"hw": hw, "region": region, "group": group}

    # capacities sized relative to demand
    rows = {l: max(1, int(round(tightness * n_demands / n_datacenters * rng.uniform(0.7, 1.3)))) for l in L}
    inv = {s: max(1, int(round(tightness * n_demands / n_suppliers * rng.uniform(0.7, 1.3)))) for s in S}
    per_day = max(1, int(round(n_demands / (n_datacenters * n_days) * 1.5)))
    thr = {l: {t: int(rng.integers(max(1, per_day - 1), per_day + 2)) for t in days} for l in L}

    return Instance(days, L, S, D, rows, thr, inv, compat_dc, compat_sup, ideal, pen, ccost, hcost, meta)
