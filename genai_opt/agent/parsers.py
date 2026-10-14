"""Question -> action parsers.

* RuleParser: deterministic regex baseline (no LLM). Good on templated phrasing,
  brittle on paraphrases. Serves as the "no-GenAI" comparison in the benchmark.
* LLMParser: any OpenAI-compatible chat endpoint. Defaults to a local Ollama
  server running Qwen (free, offline). Also works with vLLM / LM Studio /
  llama.cpp server by changing base_url.
"""
from __future__ import annotations

import json
import os
import re

from .catalog import ACTIONS, ActionError, validate
from ..milp.instance import Instance


# --------------------------------------------------------------------------- rules
class RuleParser:
    name = "rules"

    def parse(self, q: str, inst: Instance) -> dict:
        s = q.lower()
        d = re.search(r"\b(d\d+)\b", s)
        sup = re.search(r"\b(s\d+)\b", s)
        dc = re.search(r"\b(dc\d+)\b", s)
        num = re.search(r"(\d+(?:\.\d+)?)\s*%", s)
        d, sup, dc = (d and d.group(1)), (sup and sup.group(1).upper()), (dc and dc.group(1).upper())

        if re.search(r"where do we stand|status|summar|overview|what'?s going on", s):
            return {"action": "status_summary", "args": {}}
        if d and sup and re.search(r"why .*not|why isn'?t|why wasn'?t", s):
            return {"action": "why_not_supplier", "args": {"demand": d, "supplier": sup}}
        if d and dc and re.search(r"why .*not|why isn'?t|why wasn'?t", s):
            return {"action": "why_not_datacenter", "args": {"demand": d, "datacenter": dc}}
        if d and re.search(r"\bwhy\b", s):
            return {"action": "explain_assignment", "args": {"demand": d}}
        if d and re.search(r"earlier", s):
            m = re.search(r"(\d+)\s*day", s)
            days = int(m.group(1)) if m else (7 * int(re.search(r"(\d+)\s*week", s).group(1))
                                              if re.search(r"(\d+)\s*week", s) else 7)
            return {"action": "dock_earlier", "args": {"demand": d, "days": days}}
        if d and sup and re.search(r"must be|fulfilled by|from supplier|use supplier|force", s):
            return {"action": "force_supplier", "args": {"demand": d, "supplier": sup}}
        if sup and re.search(r"unavailable|offline|goes down|is down|shut", s):
            return {"action": "supplier_unavailable", "args": {"supplier": sup}}
        if dc and re.search(r"throughput", s) and num:
            pct = float(num.group(1)) / 100
            f = 1 - pct if re.search(r"drop|fall|decreas|reduc|cut", s) else 1 + pct
            args = {"datacenter": dc, "factor": round(f, 4)}
            if "next week" in s:
                args.update(start_day=min(min(inst.days) + 7, max(inst.days)), end_day=max(inst.days))
            return {"action": "scale_throughput", "args": args}
        if dc and re.search(r"unavailable|offline|closed|no rows|cannot take", s):
            return {"action": "datacenter_unavailable", "args": {"datacenter": dc}}
        if re.search(r"shipping cost", s):
            if "double" in s:
                f = 2.0
            elif num:
                f = 1 + float(num.group(1)) / 100 * (-1 if re.search(r"drop|fall|decreas|reduc|cut", s) else 1)
            else:
                f = 1.0
            return {"action": "scale_shipping_cost", "args": {"factor": f, "supplier": sup}}
        if sup and re.search(r"inventory", s):
            m = re.search(r"(\d+)\s*(?:units|racks)?\s*$", s.rstrip("?. "))
            if m:
                return {"action": "set_inventory", "args": {"supplier": sup, "value": int(m.group(1))}}
        if re.search(r"gap|faster|time limit", s) and num:
            return {"action": "set_solver_params", "args": {"gap": float(num.group(1)) / 100, "time_limit": None}}
        return {"action": "out_of_scope", "args": {"reason": "no rule matched"}}


# --------------------------------------------------------------------------- LLM
def _catalog_text() -> str:
    lines = []
    for name, (desc, args, _) in ACTIONS.items():
        a = ", ".join(f'"{k}": {v}' for k, v in args.items()) or "(no args)"
        lines.append(f"- {name}: {desc}\n    args: {a}")
    return "\n".join(lines)


FEW_SHOT = [
    ("Can demand d4 dock at least one week earlier than the current plan?",
     {"action": "dock_earlier", "args": {"demand": "d4", "days": 7}}),
    ("What if supplier S2 becomes unavailable?",
     {"action": "supplier_unavailable", "args": {"supplier": "S2"}}),
    ("Can we still satisfy all demands if the throughput at data center DC1 drops by 50% next week?",
     {"action": "scale_throughput", "args": {"datacenter": "DC1", "factor": 0.5, "start_day": 8, "end_day": 14}}),
    ("Why is S1 not used for d12?",
     {"action": "why_not_supplier", "args": {"demand": "d12", "supplier": "S1"}}),
    ("What's the weather in Seattle?",
     {"action": "out_of_scope", "args": {"reason": "not about the fulfillment plan"}}),
]

SYSTEM = """You translate a supply-chain planner's question into exactly ONE action for an optimization model.
You never solve the problem yourself; a MILP solver does that.

Entities in the current model:
  demands: {demands}
  suppliers: {suppliers}
  data centers: {dcs}
  planning days: {d0}..{d1} (today is day {d0}; "next week" = days {nw0}..{nw1})

Allowed actions:
{catalog}

Rules:
- Reply with a single JSON object: {{"action": <name>, "args": {{...}}}}. No prose.
- Use ids exactly as listed (d7, S2, DC3). If the question names an entity that does not exist, still copy it; the system will reject it.
- Percent changes become factors: "drops 30%" -> 0.7, "doubles" -> 2.0, "up 15%" -> 1.15.
- If nothing in the catalog fits, use out_of_scope.
"""


class LLMParser:
    """OpenAI-compatible chat client. Defaults to Ollama + Qwen running locally.

    env vars: LLM_BASE_URL (default http://localhost:11434/v1), LLM_MODEL (default qwen2.5:7b-instruct),
              LLM_API_KEY (default 'ollama').
    """

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None,
                 temperature: float = 0.0, retries: int = 1):
        from openai import OpenAI
        self.model = model or os.getenv("LLM_MODEL", "qwen2.5:7b-instruct")
        self.client = OpenAI(base_url=base_url or os.getenv("LLM_BASE_URL", "http://localhost:11434/v1"),
                             api_key=api_key or os.getenv("LLM_API_KEY", "ollama"))
        self.temperature, self.retries = temperature, retries
        self.name = f"llm:{self.model}"

    def chat(self, messages, json_mode=True) -> str:
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        r = self.client.chat.completions.create(model=self.model, messages=messages,
                                                temperature=self.temperature, **kw)
        return r.choices[0].message.content

    def parse(self, q: str, inst: Instance) -> dict:
        d0, d1 = min(inst.days), max(inst.days)
        sys = SYSTEM.format(demands=", ".join(inst.demands[:12]) + (f", ... {inst.demands[-1]}" if len(inst.demands) > 12 else ""),
                            suppliers=", ".join(inst.suppliers), dcs=", ".join(inst.datacenters),
                            d0=d0, d1=d1, nw0=min(d0 + 7, d1), nw1=min(d0 + 13, d1), catalog=_catalog_text())
        msgs = [{"role": "system", "content": sys}]
        for uq, ua in FEW_SHOT:
            msgs += [{"role": "user", "content": uq}, {"role": "assistant", "content": json.dumps(ua)}]
        msgs.append({"role": "user", "content": q})
        last_err = None
        for _ in range(self.retries + 1):
            raw = self.chat(msgs)
            try:
                act = json.loads(_extract_json(raw))
                return validate(act, inst)
            except (json.JSONDecodeError, ActionError) as e:   # self-correction loop with validator feedback
                last_err = e
                msgs += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": f"Invalid: {e}. Reply again with one valid JSON action."}]
        return {"action": "out_of_scope", "args": {"reason": f"parser failed: {last_err}"}}

    def narrate(self, question: str, facts: dict) -> str:
        msgs = [{"role": "system", "content":
                 "You explain optimization results to a supply-chain planner in 2-4 plain sentences. "
                 "Use ONLY the numbers and ids in FACTS; do not invent any. Mention binding constraints when relevant."},
                {"role": "user", "content": f"QUESTION: {question}\nFACTS: {json.dumps(facts, default=str)}"}]
        return self.chat(msgs, json_mode=False).strip()


def _extract_json(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, flags=re.S)
    return m.group(0) if m else raw
