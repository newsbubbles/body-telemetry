"""Flow: what agents call next. The tool calls of each turn, in order, read as paths, and the paths added up step by
step: from the start of a turn, or from every call of one op (or a set of ops), what came after it.

    start=act,say   from every act or say call    (empty: from the start of each turn)
    depth=4         steps after the start
    node=tool|verb  name a step by its tool, or an act by its verb as well (act:give)
    top=6           the commonest names at each step; the rest are folded into other

The same filters as a query choose whose turns: one seat, a set of seats, a model, a world, everyone."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .query import QueryError, where_sql
from .store import Store

WAKE, END, OTHER = "(turn starts)", "(turn ends)", "other"
MAX_ROWS = 1_000_000


def name_of(tool: str, verb: str | None, node: str) -> str:
    return f"act:{verb}" if node == "verb" and tool == "act" and verb else tool


def flow(store: Store, params: dict[str, Any]) -> dict[str, Any]:
    starts = {s for s in str(params.get("start") or "").split(",") if s}
    depth = max(1, min(int(params.get("depth") or 4), 8))
    node = "verb" if params.get("node") == "verb" else "tool"
    top = max(2, min(int(params.get("top") or 6), 12))
    clauses, args = where_sql({k: v for k, v in params.items() if k not in ("start", "depth", "node", "top")},
                              "tool_call")
    w = " WHERE " + " AND ".join(clauses)
    rows = store.execute(f"SELECT machine, run, seat, turn, tool, verb, ok FROM events{w} "
                         f"ORDER BY machine, run, seat, turn, ts LIMIT {MAX_ROWS}", args)
    turns: list[list[tuple[str, int]]] = []
    key = None
    for r in rows:
        k = (r[0], r[1], r[2], r[3])
        if k != key:
            turns.append([])
            key = k
        turns[-1].append((name_of(r[4], r[5], node), 1 if r[6] in (None, 1) else 0))
    paths: list[list[tuple[str, int]]] = []
    for seq in turns:
        seq = seq + [(END, 1)]
        if not starts:
            paths.append([(WAKE, 1)] + seq[:depth])
            continue
        for i, (name, _) in enumerate(seq):
            if name in starts or name.split(":")[0] in starts:
                paths.append(seq[i:i + depth + 1])
    if not paths:
        return {"steps": [], "links": [], "paths": 0, "turns": len(turns)}
    # the commonest names at each step stay; the rest fold into other (so the picture stays readable)
    keep: list[set[str]] = []
    for s in range(depth + 1):
        c = Counter(p[s][0] for p in paths if len(p) > s)
        keep.append({n for n, _ in c.most_common(top)} | {END, WAKE})
    nodes: list[dict[str, list[int]]] = [defaultdict(lambda: [0, 0]) for _ in range(depth + 1)]
    links: Counter = Counter()
    for p in paths:
        prev = None
        for s, (name, ok) in enumerate(p):
            n = name if name in keep[s] else OTHER
            nodes[s][n][0] += 1
            nodes[s][n][1] += 1 - ok
            if prev is not None:
                links[(s - 1, prev, n)] += 1
            if n == END:
                break
            prev = n
    return {"paths": len(paths), "turns": len(turns), "start": sorted(starts), "node": node,
            "steps": [[{"name": n, "n": v[0], "refused": v[1]} for n, v in sorted(st.items(), key=lambda x: -x[1][0])]
                      for st in nodes if st],
            "links": [{"step": s, "from": a, "to": b, "n": n, "share": round(n / nodes[s][a][0], 4)}
                      for (s, a, b), n in links.most_common()]}


__all__ = ["flow", "QueryError"]
