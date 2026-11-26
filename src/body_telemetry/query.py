"""Queries: a measure, grouped by any labels, filtered, bucketed in time.

    measure=spend by=seat,model bucket=day since=7d seat=mara,nico kind=model_call

A measure is a name from MEASURES (each knows which kind of event it counts) or a plain aggregate over a number
column: sum:cost, avg:deeds, p50:input_tokens, p95:secs, max:input_max, count. Labels and fields group and filter by
name; x.<field> reaches a field kept in the extra column, sw.<switch> one switch. Rates that are a share of trials
(pass rate, refusal rate, ...) come with a 95% Wilson interval."""

from __future__ import annotations

import json
import math
import re
import time
from collections import Counter
from typing import Any

from .store import FIELDS, LABELS, NUMBERS, Store, q

# name: (kind, expression or (numerator, denominator), label, unit, type)
#   type: value | ratio (a ratio of sums) | share (successes of trials: gets a 95% interval)
MEASURES: dict[str, tuple[str | None, Any, str, str, str]] = {
    "events": (None, "COUNT(*)", "events", "count", "value"),
    "seats": (None, "COUNT(DISTINCT seat)", "seats", "count", "value"),
    "spend": ("model_call", "SUM(cost)", "spend", "usd", "value"),
    "model_calls": ("model_call", "COUNT(*)", "model calls", "count", "value"),
    "input_tokens": ("model_call", "SUM(input_tokens)", "input tokens", "tokens", "value"),
    "cached_tokens": ("model_call", "SUM(cached_tokens)", "cached tokens", "tokens", "value"),
    "output_tokens": ("model_call", "SUM(output_tokens)", "output tokens", "tokens", "value"),
    "reasoning_tokens": ("model_call", "SUM(reasoning_tokens)", "reasoning tokens", "tokens", "value"),
    "input_p50": ("model_call", "p50(input_tokens)", "input per call, median", "tokens", "value"),
    "input_p95": ("model_call", "p95(input_tokens)", "input per call, top 5%", "tokens", "value"),
    "input_max": ("model_call", "MAX(input_tokens)", "input per call, largest", "tokens", "value"),
    "context_use": ("model_call", ("SUM(input_tokens)", "SUM(CASE WHEN ctx_limit > 0 THEN ctx_limit END)"),
                    "input over the model's limit", "share", "ratio"),
    "cache_hit": ("model_call", ("SUM(cached_tokens)", "SUM(input_tokens)"), "cache hit rate", "share", "ratio"),
    "cost_per_call": ("model_call", "AVG(cost)", "cost per model call", "usd", "value"),
    "latency_p50": ("model_call", "p50(seconds)", "model wait, median", "seconds", "value"),
    "latency_p95": ("model_call", "p95(seconds)", "model wait, top 5%", "seconds", "value"),
    "turns": ("turn", "COUNT(*)", "turns", "count", "value"),
    "cost_per_turn": ("turn", "AVG(cost)", "cost per turn", "usd", "value"),
    "steps_per_turn": ("turn", "AVG(steps)", "model calls per turn", "count", "value"),
    "tools_per_turn": ("turn", "AVG(tool_calls)", "tool calls per turn", "count", "value"),
    "deeds_per_turn": ("turn", "AVG(deeds)", "deeds per turn", "count", "value"),
    "says_per_turn": ("turn", "AVG(says)", "lines said per turn", "count", "value"),
    "reads_per_turn": ("turn", "AVG(reads)", "reads per turn", "count", "value"),
    "opens_per_turn": ("turn", "AVG(opens)", "menu opens per turn", "count", "value"),
    "deed_share": ("turn", ("SUM(deeds)", "SUM(tool_calls)"), "deeds among tool calls", "share", "ratio"),
    "idle_turns": ("turn", ("SUM(COALESCE(deeds, 0) = 0)", "COUNT(*)"), "turns with no deed", "share", "share"),
    "nudges_per_100": ("turn", ("100.0 * SUM(nudges)", "COUNT(*)"), "nudges per 100 turns", "count", "ratio"),
    "out_of_steps": ("turn", ("SUM(ended = 'out of steps')", "COUNT(*)"), "turns out of steps", "share", "share"),
    "escalated": ("turn", ("SUM(escalations > 0)", "COUNT(*)"), "turns escalated", "share", "share"),
    "wall_p50": ("turn", "p50(wall)", "turn wall time, median", "seconds", "value"),
    "tool_calls": ("tool_call", "COUNT(*)", "tool calls", "count", "value"),
    "refusals": ("tool_call", "SUM(ok = 0)", "refused calls", "count", "value"),
    "refusal_rate": ("tool_call", ("SUM(ok = 0)", "COUNT(*)"), "refusal rate", "share", "share"),
    "tool_secs_p50": ("tool_call", "p50(secs)", "tool wait, median", "seconds", "value"),
    "answer_chars_p50": ("tool_call", "p50(out_chars)", "answer size, median", "chars", "value"),
    "invented": ("invented", "COUNT(*)", "invented tools", "count", "value"),
    "reflexes": ("reflex", "COUNT(*)", "judge looks", "count", "value"),
    "reflex_handled": ("reflex", ("SUM(outcome = 'handled')", "COUNT(*)"), "looks handled asleep", "share",
                       "share"),
    "tells": ("tell", "COUNT(*)", "reports to the operator", "count", "value"),
    "samples": ("eval_sample", "COUNT(*)", "eval samples", "count", "value"),
    "pass_rate": ("eval_sample", ("SUM(passed)", "SUM(trials)"), "pass rate", "share", "share"),
    "eval_cost": ("eval_sample", "SUM(cost)", "eval spend", "usd", "value"),
    "eval_cost_per_sample": ("eval_sample", "AVG(cost)", "cost per sample", "usd", "value"),
    "eval_input_max_p50": ("eval_sample", "p50(input_max)", "largest input, median", "tokens", "value"),
    "eval_calls_per_sample": ("eval_sample", "AVG(model_calls)", "model calls per sample", "count", "value"),
}
AGGS = {"sum": "SUM", "avg": "AVG", "min": "MIN", "max": "MAX", "p05": "p05", "p25": "p25", "p50": "p50",
        "p75": "p75", "p95": "p95", "p99": "p99"}
DIMS = LABELS + FIELDS
BUCKETS = {"none": 0, "minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400}
_KEY = re.compile(r"^[A-Za-z0-9_]{1,40}$")
_REL = re.compile(r"^(\d+(?:\.\d+)?)([mhdw])$")
NONE = "(none)"


class QueryError(ValueError):
    pass


def dim_sql(name: str) -> str:
    """A label, a field, x.<extra field> or sw.<switch>, as SQL. Anything else is refused (no SQL from outside)."""
    if name in DIMS:
        return q(name)
    head, _, key = name.partition(".")
    if head in ("x", "sw") and _KEY.match(key):
        return f"json_extract({'extra' if head == 'x' else 'switch_map'}, '$.{key}')"
    raise QueryError(f"no such label: {name}")


def measure_sql(name: str) -> tuple[str | None, Any, str, str, str]:
    if name in MEASURES:
        return MEASURES[name]
    if name == "count":
        return None, "COUNT(*)", "count", "count", "value"
    agg, _, col = name.partition(":")
    if agg in AGGS and col in NUMBERS:
        return None, f"{AGGS[agg]}({q(col)})", f"{agg} of {col}", col, "value"
    raise QueryError(f"no such measure: {name}")


def when(v: str, now: float) -> float:
    """1795700000, 24h, 7d, 2026-11-26 or 2026-11-26T18:00 as Unix seconds."""
    v = v.strip()
    m = _REL.match(v)
    if m:
        return now - float(m.group(1)) * {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    try:
        return float(v)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(v, fmt))  # the server's local time
        except ValueError:
            continue
    raise QueryError(f"not a time: {v}")


def wilson(k: float, n: float, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 1.0
    p = min(max(k / n, 0.0), 1.0)
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def where_sql(params: dict[str, Any], kind: str | None) -> tuple[list[str], list[Any]]:
    now = time.time()
    clauses, args = [], []
    if kind and not params.get("kind"):
        clauses.append("kind = ?")
        args.append(kind)
    if params.get("since"):
        clauses.append("ts >= ?")
        args.append(when(str(params["since"]), now))
    if params.get("until"):
        clauses.append("ts < ?")
        args.append(when(str(params["until"]), now))
    for k, v in params.items():
        if k in ("measure", "by", "bucket", "since", "until", "limit", "key", "before") or v in (None, ""):
            continue
        neg = k.startswith("not.")
        name = k[4:] if neg else k
        if name not in DIMS and not name.startswith(("x.", "sw.")):
            raise QueryError(f"no such filter: {k}")
        col = dim_sql(name)
        vals = [x for x in (v if isinstance(v, list) else str(v).split(","))]
        parts = []
        real = [x for x in vals if x != NONE]
        if real:
            parts.append(f"{col} IN ({', '.join('?' for _ in real)})")
            args.extend(real)
        if NONE in vals:
            parts.append(f"({col} IS NULL OR {col} = '')")
        cond = "(" + " OR ".join(parts) + ")"
        clauses.append(f"NOT {cond}" if neg else cond)
    return clauses, args


def run_query(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("measure") or "events")
    kind, expr, label, unit, typ = measure_sql(name)
    by = [b for b in str(params.get("by") or "").split(",") if b][:4]
    bucket = str(params.get("bucket") or "none")
    size = BUCKETS.get(bucket)
    if size is None:
        try:
            size = max(60, int(bucket))
        except ValueError:
            raise QueryError(f"no such bucket: {bucket}") from None
    limit = min(int(params.get("limit") or 2000), 20000)
    clauses, args = where_sql(params, kind)
    sel, grp = [], []
    if size:
        sel.append(f"CAST(ts / {size} AS INTEGER) * {size} AS t")
        grp.append("t")
    for i, b in enumerate(by):
        sel.append(f"{dim_sql(b)} AS g{i}")
        grp.append(f"g{i}")
    if isinstance(expr, tuple):
        sel += [f"{expr[0]} AS num", f"{expr[1]} AS den"]
    else:
        sel.append(f"{expr} AS value")
    sel.append("COUNT(*) AS n")
    sql = f"SELECT {', '.join(sel)} FROM events"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if grp:
        sql += " GROUP BY " + ", ".join(grp)
    order = (["t"] if size else []) + ["n DESC"]
    sql += f" ORDER BY {', '.join(order)} LIMIT {limit + 1}"
    t0 = time.time()
    got = store.execute(sql, args)
    rows = []
    for r in got[:limit]:
        out: dict[str, Any] = {}
        if size:
            out["t"] = r["t"]
        for i, b in enumerate(by):
            v = r[f"g{i}"]
            out[b] = NONE if v is None or v == "" else v
        if isinstance(expr, tuple):
            num, den = r["num"] or 0, r["den"] or 0
            out["value"] = num / den if den else None
            out["num"], out["den"] = num, den
            if typ == "share" and den:
                out["lo"], out["hi"] = wilson(num, den)
        else:
            out["value"] = r["value"]
        out["n"] = r["n"]
        rows.append(out)
    return {"measure": name, "label": label, "unit": unit, "type": typ, "kind": kind, "by": by,
            "bucket": size or None, "rows": rows, "truncated": len(got) > limit, "ms": round((time.time() - t0) * 1e3)}


def meta(store: Store) -> dict[str, Any]:
    """What the store holds: kinds, time range, measures, labels and their most common values. One pass over the
    table (it reads every row: the server keeps the answer until rows come in)."""
    dims = [d for d in DIMS if d != "day"]
    counts = [Counter() for _ in dims]
    lo, hi, n = None, None, 0
    switches: set[str] = set()
    seen_sw: set[str] = set()
    with store.lock:
        cur = store.db.execute(f"SELECT ts, switch_map, {', '.join(q(d) for d in dims)} FROM events")
        for row in cur:
            n += 1
            ts = row[0]
            lo = ts if lo is None or ts < lo else lo
            hi = ts if hi is None or ts > hi else hi
            if row[1] and row[1] not in seen_sw:
                seen_sw.add(row[1])
                switches.update(json.loads(row[1]))
            for c, v in zip(counts, row[2:]):
                if v is not None:
                    c[v] += 1
    values = {d: [[v, k] for v, k in c.most_common(200)] for d, c in zip(dims, counts) if c}
    return {"rows": n, "from": lo, "to": hi, "kinds": dict(counts[dims.index("kind")]), "values": values,
            "switches": sorted(switches), "numbers": list(NUMBERS), "buckets": list(BUCKETS),
            "measures": {k: {"kind": v[0], "label": v[2], "unit": v[3], "type": v[4]} for k, v in MEASURES.items()}}


def events(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    """The rows behind a slice, newest first: what a chart's bar or point is made of. A measure narrows them to
    its kind; `before` (a time) pages back."""
    kind = measure_sql(str(params["measure"]))[0] if params.get("measure") else None
    clauses, args = where_sql(params, kind)
    if params.get("before"):
        clauses.append("ts < ?")
        args.append(float(params["before"]))
    limit = max(1, min(int(params.get("limit") or 100), 500))
    w = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    total = store.execute(f"SELECT COUNT(*) FROM events{w}", args)[0][0]
    out = []
    for r in store.execute(f"SELECT * FROM events{w} ORDER BY ts DESC LIMIT {limit}", args):
        row = {k: r[k] for k in r.keys() if r[k] is not None and k not in ("id", "uid", "switch_map", "extra")}
        if r["extra"]:
            try:
                row["extra"] = json.loads(r["extra"])
            except ValueError:
                pass
        out.append(row)
    return {"total": total, "rows": out}


def seats(store: Store, params: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per seat: who, on what model, turns, spend (all and the last 24 hours), last wake, trouble.
    Eval samples are left out unless run= names a run."""
    clauses, args = where_sql({k: v for k, v in params.items() if k not in ("measure", "by", "bucket")}, None)
    if not params.get("run"):
        clauses.append("(run IS NULL OR run = '')")
    w = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    day = time.time() - 86400
    sql = f"""SELECT machine, seat, MAX(world) world, MAX(character) character, MAX(role) role,
        MAX(CASE WHEN kind = 'turn' THEN model END) model,
        SUM(kind = 'turn') turns, SUM(CASE WHEN kind = 'model_call' THEN cost END) spend,
        SUM(CASE WHEN kind = 'model_call' AND ts >= {day} THEN cost END) spend_24h,
        MAX(CASE WHEN kind = 'turn' THEN ts END) last_wake, MAX(ts) last_seen, MIN(ts) first_seen,
        SUM(kind = 'tool_call') tool_calls, SUM(kind = 'tool_call' AND ok = 0) refusals,
        SUM(kind = 'turn' AND ended = 'out of steps') out_of_steps,
        SUM(kind = 'tell' AND event = 'error') errors, SUM(kind = 'invented') invented,
        SUM(CASE WHEN kind = 'turn' THEN deeds END) deeds,
        MAX(CASE WHEN kind = 'model_call' THEN input_tokens END) input_max,
        p50(CASE WHEN kind = 'model_call' THEN input_tokens END) input_p50,
        SUM(CASE WHEN kind = 'model_call' THEN cached_tokens END) * 1.0 /
            NULLIF(SUM(CASE WHEN kind = 'model_call' THEN input_tokens END), 0) cache_hit,
        MAX(source) source, MAX(version) version, MAX(switches) switches
        FROM events{w} GROUP BY machine, seat ORDER BY last_seen DESC LIMIT 2000"""
    now = time.time()
    return [dict(r, state=state_of(r["errors"], now - (r["last_seen"] or 0))) for r in store.execute(sql, args)]


def state_of(errors: int | None, age: float) -> str:
    """A seat's state from its last event: errors win, then awake (the last 5 minutes), quiet (the last hour), asleep."""
    return "errors" if errors else "awake" if age < 300 else "quiet" if age < 3600 else "asleep"
