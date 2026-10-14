"""pytest -q tests/"""
import itertools
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genai_opt.agent.agent import Assistant, fidelity  # noqa: E402
from genai_opt.agent.parsers import LLMParser, RuleParser  # noqa: E402
from genai_opt.milp.instance import generate  # noqa: E402
from genai_opt.milp.model import Edits, solve  # noqa: E402


def brute_force(inst):
    """Enumerate every (dc, day, supplier) or 'unfulfilled' per demand; check (2)-(6)."""
    opts = {d: [None] + [(l, t, s) for l in inst.compat_dc[d] for t in inst.days for s in inst.compat_sup[d]]
            for d in inst.demands}
    best = float("inf")
    for combo in itertools.product(*opts.values()):
        lt, rows, inv = {}, {}, {}
        ok = True
        cost = 0.0
        for d, a in zip(inst.demands, combo):
            if a is None:
                cost += inst.penalty[d]
                continue
            l, t, s = a
            lt[l, t] = lt.get((l, t), 0) + 1
            rows[l] = rows.get(l, 0) + 1
            inv[s] = inv.get(s, 0) + 1
            if lt[l, t] > inst.throughput[l][t] or rows[l] > inst.rows[l] or inv[s] > inst.inventory[s]:
                ok = False
                break
            cost += inst.dock_cost[d][t] + inst.ship_cost[d][s]
        if ok:
            best = min(best, cost)
    return best


def test_milp_matches_brute_force():
    for seed in range(5):
        inst = generate(n_demands=4, n_datacenters=2, n_suppliers=2, n_days=2, tightness=0.6, seed=seed)
        sol = solve(inst)
        assert abs(sol.objective - brute_force(inst)) < 1e-6, seed


def test_solution_respects_constraints():
    inst = generate(seed=3)
    sol = solve(inst)
    lt, rows, inv = {}, {}, {}
    for d, a in sol.assignment.items():
        if a:
            l, t, s = a
            assert l in inst.compat_dc[d] and s in inst.compat_sup[d]
            lt[l, t] = lt.get((l, t), 0) + 1
            rows[l] = rows.get(l, 0) + 1
            inv[s] = inv.get(s, 0) + 1
    assert all(v <= inst.throughput[l][t] for (l, t), v in lt.items())
    assert all(v <= inst.rows[l] for l, v in rows.items())
    assert all(v <= inst.inventory[s] for s, v in inv.items())


def test_incompatible_supplier_is_diagnosed():
    inst = generate(seed=0)
    d = next(d for d in inst.demands if len(inst.compat_sup[d]) < len(inst.suppliers))
    bad = next(s for s in inst.suppliers if s not in inst.compat_sup[d])
    sol = solve(inst, Edits(forced_supplier={d: bad}, must_fulfill={d}))
    assert sol.status != "Optimal"
    assert any(bad in x for x in sol.infeasible_edits)


def test_anchor_does_not_change_cost():
    inst = generate(seed=1)
    base = solve(inst)
    again = solve(inst, anchor=base.assignment)
    assert abs(base.objective - again.objective) < 1e-6
    assert again.assignment == base.assignment


def test_fidelity_flags_invented_numbers():
    facts = {"cost_change": 4.28, "scenario_cost": 680.65}
    assert fidelity("Cost rises by 4.28 to 680.65.", facts) == 1.0
    assert fidelity("Cost rises by 9.99 to 680.65.", facts) == 0.5


class FakeLLM(LLMParser):
    """LLMParser with a scripted client: first reply invalid, second valid -> exercises the repair loop."""

    def __init__(self, replies):
        self.model, self.temperature, self.retries, self.name = "fake", 0, 1, "fake"
        self._replies = iter(replies)
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self._create)))

    def _create(self, **kw):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=next(self._replies)))])


def test_llm_parser_self_correction():
    inst = generate(seed=0)
    p = FakeLLM(['{"action": "supplier_unavailable", "args": {"supplier": "S99"}}',
                 '```json\n{"action": "supplier_unavailable", "args": {"supplier": "s2"}}\n```'])
    assert p.parse("S2 is down", inst) == {"action": "supplier_unavailable", "args": {"supplier": "S2"}}


def test_assistant_commit_changelog():
    inst = generate(seed=0)
    A = Assistant(inst, RuleParser())
    a = A.ask("What if supplier S3 becomes unavailable?")
    entry = A.commit("plant shutdown")
    assert entry["new_cost"] == a.solution.objective and A.inst.inventory["S3"] == 0 and len(A.changelog) == 1
