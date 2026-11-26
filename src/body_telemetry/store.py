"""The store: one flat SQLite table of events, the same labels on every row.

Every event is one row. The labels (world, seat, character, model, settings, ...) and the fields most worth counting
are columns; anything else an event carries lands in the `extra` JSON column, so a field this version does not know
never breaks an import or a post, and can still be grouped by as `x.<field>`."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

# Labels: who and what each row is about. Filled on every row the store can fill.
LABELS = ("kind", "source", "harness", "version", "world", "seat", "character", "role", "model", "provider", "maker",
          "settings", "switches", "machine", "run")
# Text fields of one kind or another: what a tool was, how a turn ended, which eval case.
FIELDS = ("use", "tool", "layer", "verb", "code", "event", "ended", "case", "variant", "scene", "trigger", "outcome",
          "read_as", "day")
# Numbers.
NUMBERS = ("turn", "step", "rung", "sample", "level", "ok", "calls", "input_tokens", "cached_tokens",
           "cache_write_tokens", "output_tokens", "reasoning_tokens", "cost", "seconds", "steps", "tool_calls",
           "deeds", "says", "reads", "opens", "nudges", "escalations", "wall", "secs", "out_chars", "rules", "passed",
           "trials", "input_max", "input_sum", "model_calls", "ctx_limit")
TEXT = LABELS + FIELDS
COLUMNS = ("ts",) + TEXT + NUMBERS

# What a seat's words are, by detail level. counts keeps none of them; lines keeps the short ones, cut to 120
# characters; full keeps all. Players' words are other people's words: full is never a default.
WORDS_LINES = ("reason", "note", "refusal", "detail", "error")
WORDS_FULL = WORDS_LINES + ("said", "waiting_for", "what_happened", "text", "rule")
DETAIL = ("counts", "lines", "full")

# Labels a row without them takes from the last row of the same seat (a tool call carries no model; the seat's
# start event names the world and the character once).
FILL = ("harness", "version", "world", "character", "role", "model", "provider", "maker", "settings", "switches")
_SWITCH_KEY = "switch_map"


def q(name: str) -> str:
    """A column name quoted for SQL (case, use and turn are words SQLite knows)."""
    return '"' + name.replace('"', "") + '"'


def model_parts(model: Any) -> tuple[str | None, str | None]:
    """'openrouter:deepseek/deepseek-v4-flash' -> provider 'openrouter', maker 'deepseek'."""
    if not model:
        return None, None
    m = str(model)
    provider, _, rest = m.partition(":") if ":" in m else ("", "", m)
    maker = rest.split("/", 1)[0] if "/" in rest else None
    return provider or None, maker


def switches_label(sw: dict[str, Any]) -> str:
    """The switches that are on, as one groupable label: 'menu+menu_lists', or 'none'."""
    on = sorted(k for k, v in sw.items() if v)
    return "+".join(on) if on else "none"


def nudge_kind(n: Any) -> str:
    """'quiet: Ada, Bo' -> 'quiet'; 'Not asleep yet...' -> 'other'."""
    k = str(n).split(":", 1)[0].strip()
    return k if ":" in str(n) and k.replace("_", "").isalnum() and len(k) <= 24 else "other"


GRAPH_EDGES = ("yes", "no", "unsure", "then", "resume", "none")
GRAPH_ENDS = ("WAKE", "SLEEP")


def graph_shape(g: dict[str, Any], detail: str) -> dict[str, Any]:
    """A behavior graph at a detail level. counts keeps its shape: node kinds, edges, the op and verb of each deed and
    how often each node fired; node names become n1, n2... and a choice's answers a1, a2..., since the agent wrote
    them. lines keeps the words cut to 120 characters; full keeps all of it."""
    if detail == "full":
        return g
    nodes = g.get("nodes") if isinstance(g.get("nodes"), dict) else {}
    if detail == "lines":
        cut = lambda v: v[:120] if isinstance(v, str) else {k: cut(x) for k, x in v.items()} if isinstance(v, dict) \
            else [cut(x) for x in v] if isinstance(v, list) else v
        return cut(g)
    names: dict[str, str] = {e: e for e in GRAPH_ENDS}
    order = [str(g.get("start") or "")] + list(nodes)
    for i in order:  # the start is n1, then nodes as the graph lists them
        if i in nodes and i not in names:
            names[i] = f"n{len(names) - len(GRAPH_ENDS) + 1}"
    to = lambda t: names.get(str(t), "?")
    out: dict[str, Any] = {}
    for i, n in nodes.items():
        if not isinstance(n, dict):
            continue
        m: dict[str, Any] = {"kind": str(n.get("kind") or "")[:12]}
        for k in ("do", "op", "verb"):
            if isinstance(n.get(k), str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_\-]{0,31}", n[k]):
                m[k] = n[k]
        if isinstance(n.get("minutes"), (int, float)):
            m["minutes"] = n["minutes"]
        for k in GRAPH_EDGES:
            if n.get(k):
                m[k] = to(n[k])
        if isinstance(n.get("options"), dict):
            m["options"] = {f"a{j + 1}": to(t) for j, t in enumerate(n["options"].values())}
        out[names[i]] = m
    shape: dict[str, Any] = {"start": to(g.get("start")), "nodes": out}
    if isinstance(g.get("fired"), dict):
        shape["fired"] = {to(k): v for k, v in g["fired"].items() if isinstance(v, (int, float)) and k in names}
    return shape


def cut_words(row: dict[str, Any], detail: str) -> None:
    """Drop or cut the words in an event to a detail level, in place."""
    keep = () if detail == "counts" else WORDS_LINES if detail == "lines" else WORDS_FULL
    for k in WORDS_FULL:
        if k not in row:
            continue
        if k not in keep or row[k] is None:
            row.pop(k)
        elif detail == "lines":
            row[k] = str(row[k])[:120]


def uid_of(ev: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(ev, sort_keys=True, default=str).encode()).hexdigest()


def normalize(ev: dict[str, Any], source: str = "live", detail: str = "full") -> dict[str, Any] | None:
    """One event as a row: known fields to columns, the rest to extra. None if it is not an event."""
    if not isinstance(ev, dict) or not ev.get("kind"):
        return None
    uid = str(ev.get("uid") or uid_of(ev))
    e = dict(ev)
    e.pop("uid", None)
    cut_words(e, detail)
    if isinstance(e.get("graph"), dict):
        e["graph"] = graph_shape(e["graph"], detail)
    row: dict[str, Any] = {"uid": uid, "source": e.pop("source", None) or source}
    try:
        row["ts"] = float(e.pop("ts"))
    except (KeyError, TypeError, ValueError):
        row["ts"] = time.time()
    sw = e.pop("switches", None)
    if isinstance(sw, dict):
        row["switches"] = switches_label(sw)
        row[_SWITCH_KEY] = json.dumps({k: bool(v) for k, v in sw.items()}, sort_keys=True)
    elif isinstance(sw, str):
        row["switches"] = sw
    esc = e.get("escalated")
    if isinstance(esc, list):
        row["escalations"] = len(esc)
    nud = e.get("nudges")
    if isinstance(nud, list):
        # a nudge's words can name other characters ("quiet: Ada"): counts keeps only its kind ("quiet")
        row["nudges"] = len(nud)
        e.pop("nudges")
        e["nudge_kinds"] = [nudge_kind(n) for n in nud]
        if detail != "counts":
            e["nudge_list"] = [str(n)[:120] if detail == "lines" else str(n) for n in nud]
    for k in TEXT:
        if k in e and k not in row:
            v = e.pop(k)
            if v is not None and v != "":
                row[k] = str(v)[:200]
    for k in NUMBERS:
        if k in e and k not in row:
            v = e[k]
            if isinstance(v, bool):
                row[k] = int(v)
                e.pop(k)
            elif isinstance(v, (int, float)) and math.isfinite(v):
                row[k] = v
                e.pop(k)
    if row.get("model") and "provider" not in row:
        row["provider"], maker = model_parts(row["model"])
        if maker:
            row["maker"] = maker
    if row.get("ts"):
        row["day"] = time.strftime("%Y-%m-%d", time.gmtime(row["ts"]))
    row["extra"] = json.dumps(e, default=str, separators=(",", ":")) if e else None
    return row


class Pct:
    """A quantile aggregate for SQLite: p50(x), p95(x), p05(x)."""

    def __init__(self, p: float):
        self.p, self.xs = p, []

    def step(self, v: Any) -> None:
        if isinstance(v, (int, float)):
            self.xs.append(v)

    def finalize(self) -> float | None:
        if not self.xs:
            return None
        xs = sorted(self.xs)
        i = (len(xs) - 1) * self.p
        lo = int(math.floor(i))
        hi = min(lo + 1, len(xs) - 1)
        return xs[lo] + (xs[hi] - xs[lo]) * (i - lo)


def _pct(p: float) -> type:
    return type(f"P{int(p * 100)}", (Pct,), {"__init__": lambda self: Pct.__init__(self, p)})


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  uid TEXT UNIQUE NOT NULL,
  ts REAL NOT NULL,
  {", ".join(q(c) + " TEXT" for c in TEXT)},
  {", ".join(q(c) + " REAL" for c in NUMBERS)},
  {_SWITCH_KEY} TEXT,
  extra TEXT
);
CREATE INDEX IF NOT EXISTS ev_kind_ts ON events(kind, ts);
CREATE INDEX IF NOT EXISTS ev_seat ON events(seat, kind);
CREATE INDEX IF NOT EXISTS ev_model ON events(model, kind);
CREATE INDEX IF NOT EXISTS ev_run ON events(run, kind);
CREATE TABLE IF NOT EXISTS seat_labels (key TEXT PRIMARY KEY, labels TEXT NOT NULL, ts REAL);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        for name, p in (("p05", 0.05), ("p25", 0.25), ("p50", 0.5), ("p75", 0.75), ("p95", 0.95), ("p99", 0.99)):
            self.db.create_aggregate(name, 1, _pct(p))
        self.labels: dict[str, dict[str, Any]] = {
            r["key"]: json.loads(r["labels"]) for r in self.db.execute("SELECT key, labels FROM seat_labels")}

    def _migrate(self) -> None:
        """A column added in a newer version is added to an older database."""
        have = {r[1] for r in self.db.execute("PRAGMA table_info(events)")}
        for c in TEXT + NUMBERS:
            if c not in have:
                self.db.execute(f"ALTER TABLE events ADD COLUMN {q(c)} {'TEXT' if c in TEXT else 'REAL'}")
        # an early build kept nudge words at counts: those rows keep only the kinds (newer rows carry
        # nudge_kinds already, and their words only at lines or full)
        for rid, extra in self.db.execute("SELECT id, extra FROM events WHERE extra LIKE "
                                          "'%\"nudge_list\"%' AND extra NOT LIKE '%\"nudge_kinds\"%'").fetchall():
            e = json.loads(extra)
            e["nudge_kinds"] = [nudge_kind(n) for n in e.pop("nudge_list") or []]
            self.db.execute("UPDATE events SET extra = ? WHERE id = ?", (json.dumps(e, separators=(",", ":")), rid))
        self.db.commit()

    def _fill(self, row: dict[str, Any], dirty: set[str]) -> None:
        key = "|".join(str(row.get(k) or "") for k in ("machine", "run", "seat"))
        if not row.get("seat"):
            return
        known = self.labels.setdefault(key, {})
        for k in FILL + (_SWITCH_KEY,):
            v = row.get(k)
            if v is None:
                if k in known:
                    row[k] = known[k]
            elif known.get(k) != v and (k not in ("model", "provider", "maker") or row.get("kind") in ("turn", "seat")
                                        or "model" not in known):
                # a model call may be on an escalated model: only a turn or the seat's start sets the seat's own
                known[k] = v
                dirty.add(key)

    def insert(self, events: Iterable[dict[str, Any]], source: str = "live", detail: str = "full",
               fill: bool = True) -> tuple[int, int]:
        """Rows in, in one transaction. Returns (new rows, events skipped as not events). A row already held (the
        same event posted twice, a file imported twice) is not added again."""
        rows, skipped, dirty = [], 0, set()
        with self.lock:
            for ev in events:
                r = normalize(ev, source, detail)
                if r is None:
                    skipped += 1
                    continue
                if fill:
                    self._fill(r, dirty)
                rows.append(r)
            cols = ("uid", "ts") + TEXT + NUMBERS + (_SWITCH_KEY, "extra")
            sql = (f"INSERT OR IGNORE INTO events ({', '.join(q(c) for c in cols)}) "
                   f"VALUES ({', '.join('?' for _ in cols)})")
            with self.db:
                before = self.db.total_changes
                self.db.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])
                new = self.db.total_changes - before
                for key in dirty:
                    self.db.execute("INSERT OR REPLACE INTO seat_labels VALUES (?, ?, ?)",
                                    (key, json.dumps(self.labels[key]), time.time()))
        return new, skipped

    def execute(self, sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, tuple(args)).fetchall()

    def count(self) -> int:
        return self.execute("SELECT COUNT(*) FROM events")[0][0]

    def close(self) -> None:
        self.db.close()
