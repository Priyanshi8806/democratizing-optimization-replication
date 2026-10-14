"""Benchmark of planner questions with gold actions (in the spirit of OptiGuide, Li et al. 2023).

In-distribution (ID): phrasing close to Table 2 and the few-shot examples.
Out-of-distribution (OOD): paraphrases, typos-free but informal, different word order.
Accuracy is reported per question (exact action + args) and per question *set*
(a set counts only if every question in it is right — the metric Li et al. use).
"""
from __future__ import annotations

import random

from ..milp.instance import Instance

ID_TEMPLATES = {
    "dock_earlier": ["Can demand {d} dock at least one week earlier than the current plan?",
                     "Can {d} dock one week earlier than planned?"],
    "supplier_unavailable": ["What if supplier {s} becomes unavailable?",
                             "What happens if supplier {s} goes offline?"],
    "scale_throughput": ["Can we still satisfy all demands if the throughput at data center {l} drops by 50% next week?",
                         "What if throughput at {l} drops by {p}%?"],
    "force_supplier": ["What if demand {d} must be fulfilled by supplier {s}?"],
    "why_not_supplier": ["Why is {s} not used for {d}?"],
    "explain_assignment": ["Why is {d} docked where it is?"],
    "status_summary": ["Where do we stand as of today?"],
    "scale_shipping_cost": ["What if shipping costs double?"],
    "datacenter_unavailable": ["What if data center {l} becomes unavailable?"],
}

OOD_TEMPLATES = {
    "dock_earlier": ["Is there any way to get rack {d} installed 7 days ahead of its current slot?",
                     "planner here - {d} needs to land a week sooner, doable?"],
    "supplier_unavailable": ["Suppose the {s} warehouse is shut for the whole horizon. Impact?",
                             "We just heard {s} can't ship anything. Re-plan please."],
    "scale_throughput": ["Docking crews at {l} are cut in half from day 8 onwards; do we still make every delivery?",
                         "{l} loading dock capacity falls {p} percent - consequences?"],
    "force_supplier": ["Business wants {d} sourced exclusively from {s}. What does that cost us?"],
    "why_not_supplier": ["How come {d} isn't coming from {s}?"],
    "explain_assignment": ["Walk me through the reasoning for {d}'s placement."],
    "status_summary": ["Give me a quick snapshot of the fulfillment situation."],
    "scale_shipping_cost": ["Freight rates just went up 2x across the board. Effect on the plan?"],
    "datacenter_unavailable": ["{l} is frozen, no new racks can go in. What changes?"],
    "out_of_scope": ["What's the capital of France?", "Write me a poem about servers."],
}


def _gold(name, inst, d, s, l, p, ood_phrase=""):
    last = max(inst.days)
    if name == "dock_earlier":
        return {"demand": d, "days": 7}
    if name in ("supplier_unavailable",):
        return {"supplier": s}
    if name == "scale_throughput":
        # explicit: "next week"/"from day 8" -> days 8..last; "half"/"50%" -> 0.5; "{p}%"/"{p} percent" -> 1-p/100
        late = "next week" in ood_phrase or "day 8" in ood_phrase
        f = 0.5 if ("half" in ood_phrase or "50%" in ood_phrase) else round(1 - p / 100, 4)
        return {"datacenter": l, "factor": f, "start_day": min(8, last) if late else min(inst.days), "end_day": last}
    if name in ("force_supplier", "why_not_supplier"):
        return {"demand": d, "supplier": s}
    if name == "explain_assignment":
        return {"demand": d}
    if name == "scale_shipping_cost":
        return {"factor": 2.0, "supplier": None}
    if name == "datacenter_unavailable":
        return {"datacenter": l}
    return {}


def build(inst: Instance, n_sets: int = 10, seed: int = 0) -> list[dict]:
    """Return a list of question sets; each set is a list of {question, gold, split} dicts."""
    rng = random.Random(seed)
    out = []
    for split, bank in (("ID", ID_TEMPLATES), ("OOD", OOD_TEMPLATES)):
        for k in range(n_sets):
            qs = []
            for name, temps in bank.items():
                tpl = rng.choice(temps)
                d = rng.choice(inst.demands)
                s = rng.choice(inst.compat_sup[d])
                l = rng.choice(inst.datacenters)
                p = rng.choice([20, 30, 50])
                q = tpl.format(d=d, s=s, l=l, p=p)
                gold = {"action": name, "args": _gold(name, inst, d, s, l, p, q)}
                qs.append({"question": q, "gold": gold, "split": split, "set": f"{split}-{k}"})
            out.append(qs)
    return out


def match(pred: dict, gold: dict) -> bool:
    if pred.get("action") != gold["action"]:
        return False
    if gold["action"] == "out_of_scope":
        return True
    pa = pred.get("args", {})
    for k, v in gold["args"].items():
        pv = pa.get(k)
        if isinstance(v, float):
            if pv is None or abs(float(pv) - v) > 1e-6:
                return False
        elif v is None:
            if pv not in (None, "", "null"):
                return False
        elif str(pv) != str(v):
            return False
    return True
