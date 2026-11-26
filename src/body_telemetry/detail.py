"""One seat close up, and the behavior graphs agents wrote.

    GET /v1/seat?seat=fen[&machine=..&run=..]   the seat's summary, its setup (and how often it changed), the helpers
                                                it has on and how much it used them, and its latest behavior graphs
    GET /v1/graphs?case=..&model=..             behavior graphs with what they scored, newest first (the same filters
                                                as a query)"""

from __future__ import annotations

import json
from typing import Any

from .helpers import helpers
from .query import QueryError, seats, where_sql
from .store import Store


def graphs(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    limit = max(1, min(int(params.get("limit") or 50), 500))
    clauses, args = where_sql({k: v for k, v in params.items() if k != "kind"}, "behavior")
    rows = store.execute(f"SELECT ts, machine, run, seat, character, model, \"case\", variant, sample, extra FROM events "
                         f"WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT {limit}", args)
    out = []
    for r in rows:
        g = (json.loads(r["extra"]) if r["extra"] else {}).get("graph") or {}
        nodes = g.get("nodes") or {}
        kinds: dict[str, int] = {}
        for n in nodes.values():
            kinds[n.get("kind") or "?"] = kinds.get(n.get("kind") or "?", 0) + 1
        out.append({k: r[k] for k in ("ts", "machine", "run", "seat", "character", "model", "case", "variant", "sample")}
                   | {"graph": g, "nodes": len(nodes), "kinds": kinds})
    # what each eval graph scored: the eval sample with the same run, case, model, variant and sample
    keyed = [o for o in out if o["run"] and o["case"]]
    if keyed:
        runs = sorted({o["run"] for o in keyed})
        score = {(s["run"], s["case"], s["model"], s["variant"], s["sample"]): (s["passed"], s["trials"]) for s in store.execute(
            f"SELECT run, \"case\", model, variant, sample, passed, trials FROM events WHERE kind = 'eval_sample' "
            f"AND run IN ({', '.join('?' for _ in runs)})", runs)}
        for o in keyed:
            o["passed"], o["trials"] = score.get((o["run"], o["case"], o["model"], o["variant"], o["sample"]), (None, None))
    return {"rows": out}


def seat_detail(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    if not params.get("seat"):
        raise QueryError("seat= names the seat")
    who = {k: params[k] for k in ("seat", "machine", "run") if params.get(k) not in (None, "")}
    summary = seats(store, who)
    # eval runs hold seats of the same name: without run=, the seat is its live play
    who_live = who if who.get("run") else {**who, "run": "(none)"}
    clauses, args = where_sql(who_live, "setup")
    setups = store.execute(f"SELECT ts, extra FROM events WHERE {' AND '.join(clauses)} ORDER BY ts DESC LIMIT 20", args)
    setup = (json.loads(setups[0]["extra"] or "{}").get("setup") if setups else None) or {}
    h = helpers(store, who)
    me = next((s for s in h["seats"] if s["seat"] == who["seat"]), None)
    return {"seat": summary[0] if summary else None, "setup": setup, "setup_ts": setups[0]["ts"] if setups else None,
            "setup_changes": max(0, len(setups) - 1),
            "helpers": [{"key": x["key"], "name": x["name"], "what": x["what"], "unit": x["unit"],
                         "on": me["on"][x["key"]] if me else None, "uses": me["uses"][x["key"]] if me else 0,
                         "per_100_turns": me["per_100_turns"][x["key"]] if me else None} for x in h["helpers"]],
            "graphs": graphs(store, {**who_live, "limit": 5})["rows"]}
