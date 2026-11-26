"""Helpers: the things body-harness adds to make a smaller model play better, which seats have each one switched on,
and how often each one is used. One pass over turns, tool calls, judge looks and model calls, with the same filters
as a query. Eval samples are left out unless run= names a run, as in the seats summary."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from .query import where_sql
from .store import Store

# key, name, what it does, the switch that turns it on (None: always on), what counts as a use
HELPERS = (
    ("escalate", "Escalation", "a stronger model takes over a turn the seat's own model stalls on", "escalate",
     "turns escalated"),
    ("imagine", "Imagination", "the agent plays a plan through in its head before it acts", "imagine",
     "imagine, reflect and relive calls"),
    ("past", "The past", "the agent reads back what happened before it sat down", "past", "past calls"),
    ("menu", "Menu", "a numbered list of what can be done here, instead of guessing at verbs", "menu", "menu calls"),
    ("think_tool", "Think", "a tool to think in, so thinking never leaks into speech", "think_tool", "think calls"),
    ("reflexes", "Reflexes", "rules set before sleep, checked by a small judge model while the agent sleeps",
     "reflexes", "judge looks"),
    ("sleep", "Sleep", "the agent ends its own turn and names what should wake it", None, "turns ended asleep"),
    ("nudges", "Nudges", "the harness points out a stuck, quiet or unanswered moment", None, "turns nudged"),
)
SUBSWITCHES = (("menu_rank", "ranked"), ("menu_preopen", "opened at wake"), ("menu_lists", "with lists"))
TOOLS = {"imagine": "imagine", "reflect": "imagine", "relive": "imagine", "past": "past", "menu": "menu",
         "think": "think_tool"}


def helpers(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    base = {k: v for k, v in params.items() if k not in ("measure", "by", "bucket", "kind")}
    extra_clause = [] if params.get("run") else ["(run IS NULL OR run = '')"]

    def rows(kind: str, cols: str, more: str = ""):
        clauses, args = where_sql(base, kind)
        w = " WHERE " + " AND ".join(clauses + extra_clause + ([more] if more else []))
        return store.execute(f"SELECT machine, run, seat, {cols} FROM events{w}", args)

    seat: dict[tuple, dict[str, Any]] = defaultdict(lambda: {"turns": 0, "uses": Counter(), "on": {}, "ts": 0.0,
                                                            "model": None, "character": None})
    nudge_kinds: Counter = Counter()
    escalated_to: Counter = Counter()
    for r in rows("turn", "ts, model, character, switch_map, escalations, nudges, ended, extra"):
        s = seat[(r[0], r[1], r[2])]
        s["turns"] += 1
        if (r[3] or 0) >= s["ts"]:
            s["ts"] = r[3] or 0
            s["model"] = r[4] or s["model"]
            s["character"] = r[5] or s["character"]
            if r[6]:
                s["on"] = json.loads(r[6])
        s["uses"]["escalate"] += (r[7] or 0) > 0
        s["uses"]["nudges"] += (r[8] or 0) > 0
        s["uses"]["sleep"] += r[9] == "slept"
        if r[10] and ((r[7] or 0) > 0 or (r[8] or 0) > 0):
            e = json.loads(r[10])
            nudge_kinds.update(e.get("nudge_kinds") or [])
            escalated_to.update(str(m) for m in e.get("escalated") or [])
    for r in rows("tool_call", "tool, COUNT(*)", f"tool IN ({', '.join(repr(t) for t in TOOLS)}) GROUP BY machine, "
                  "run, seat, tool"):
        seat[(r[0], r[1], r[2])]["uses"][TOOLS[r[3]]] += r[4]
    for r in rows("reflex", "COUNT(*)", "1 GROUP BY machine, run, seat"):
        seat[(r[0], r[1], r[2])]["uses"]["reflexes"] += r[3]
    # a call on a model other than the seat's own is an escalation (an older harness wrote no rung)
    cost: dict[tuple, Counter] = defaultdict(Counter)
    for r in rows("model_call", "use, model, MAX(COALESCE(rung, 0)), SUM(cost)",
                  "1 GROUP BY machine, run, seat, use, model"):
        k = (r[0], r[1], r[2])
        own = seat[k]["model"] if k in seat else None
        cost[k]["reflexes" if r[3] == "judge" else "escalate" if r[5] or (own and r[4] != own) else "own"] +=             r[6] or 0
    seats = [dict(v, key=k) for k, v in seat.items() if v["turns"] or v["uses"]]
    spend = sum(sum(c.values()) for c in cost.values())

    def is_on(s: dict[str, Any], sw: str | None) -> bool:
        return True if sw is None else bool(s["on"].get(sw))

    out = []
    for key, name, what, sw, unit in HELPERS:
        on = [s for s in seats if is_on(s, sw)]
        turns_on = sum(s["turns"] for s in on)
        uses = sum(s["uses"][key] for s in seats)
        h = {"key": key, "name": name, "what": what, "switch": sw, "unit": unit, "seats": len(seats),
             "seats_on": len(on), "seats_using": sum(1 for s in on if s["uses"][key]), "uses": uses,
             "turns_on": turns_on, "per_100_turns": round(100 * sum(s["uses"][key] for s in on) / turns_on, 2)
             if turns_on else None, "cost": round(sum(cost[s["key"]][key] for s in seats), 4)
             if key in ("escalate", "reflexes") else None}
        if key == "menu":
            h["sub"] = [{"switch": k, "name": n, "seats_on": sum(1 for s in seats if s["on"].get(k))}
                        for k, n in SUBSWITCHES]
        out.append(h)
    # by model: the same rates per seat model, so it shows which models lean on which helper
    models: dict[str, dict[str, Any]] = defaultdict(lambda: {"turns": Counter(), "uses": Counter(), "seats": 0})
    for s in seats:
        m = models[s["model"] or "(none)"]
        m["seats"] += 1
        for key, _, _, sw, _ in HELPERS:
            if is_on(s, sw):
                m["turns"][key] += s["turns"]
                m["uses"][key] += s["uses"][key]
    by_model = [{"model": k, "seats": v["seats"], "turns": max(v["turns"].values(), default=0),
                 "per_100_turns": {h: round(100 * v["uses"][h] / v["turns"][h], 2) if v["turns"][h] else None
                                   for h, *_ in HELPERS}}
                for k, v in sorted(models.items(), key=lambda x: -max(x[1]["turns"].values(), default=0))]
    per_seat = [{"machine": s["key"][0], "run": s["key"][1], "seat": s["key"][2], "character": s["character"],
                 "model": s["model"], "turns": s["turns"],
                 "on": {h: is_on(s, sw) for h, _, _, sw, _ in HELPERS} | {k: bool(s["on"].get(k))
                                                                          for k, _ in SUBSWITCHES},
                 "uses": {h: s["uses"][h] for h, *_ in HELPERS},
                 "per_100_turns": {h: (round(100 * s["uses"][h] / s["turns"], 1) if s["turns"] else 0.0)
                                   if is_on(s, sw) else None for h, _, _, sw, _ in HELPERS}}
                for s in sorted(seats, key=lambda s: -s["turns"])][:200]
    return {"helpers": out, "spend": round(spend, 4), "by_model": by_model, "seats": per_seat,
            "nudge_kinds": [{"kind": k, "n": n} for k, n in nudge_kinds.most_common()],
            "escalated_to": [{"model": k, "n": n} for k, n in escalated_to.most_common(12)]}
