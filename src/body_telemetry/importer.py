"""The importer: the files body-harness already writes, as events, so history from before the sender is not lost.

    body-telemetry import <home, seat or eval folder> [--detail counts|lines|full] [--machine LABEL] [--run ID]

It finds every seat folder (`seats/<name>/`) and every eval `results.jsonl` under the folder it is given and reads:

    usage.jsonl     -> model_call     ledger.jsonl   -> turn       calls.jsonl -> tool_call
    reflexes.jsonl  -> reflex         invented.jsonl -> invented   outbox.jsonl -> tell (counts only, never the text)
    results.jsonl   -> eval_sample

It opens only those files, plus a seat's config.json and the world, character and role from its state.json. The
config is hashed the way the sender hashes it, and kept as a `setup` event with only its switches, numbers and model
names (never prompts, providers, env files or anything named like a key). A behavior eval's transcripts give each
sample's behavior graph as a `behavior` event: its shape at counts, its words only at lines or full. Door files, invites and keys are never read. At `counts` (the default)
no speech, notes or wake reasons are kept."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from .store import Store

SEAT_FILES = ("usage.jsonl", "ledger.jsonl", "calls.jsonl", "reflexes.jsonl", "invented.jsonl", "outbox.jsonl")
# Folders a seat or a run keeps that hold no counts: never walked.
SKIP = {"memory", "workspace", "apps", "skills", "cache", "import", "hands", "transcripts", ".git", ".venv",
        "node_modules", "__pycache__"}
SWITCHES = ("menu", "menu_rank", "menu_preopen", "menu_lists", "reflexes", "imagine", "past", "think_tool")
DEEDS = {"act", "say", "gesture", "step", "cross", "act_many"}
READS = {"look", "find_tools", "inspect", "find", "history", "guide", "handbook", "recall", "skill", "owed", "agenda",
         "past", "menu"}
HARNESS_TOOLS = {"find_tools", "look", "memo", "skill", "sleep", "tell", "agenda", "run_python", "schedule",
                 "write_file", "think", "records", "past", "imagine", "reflect", "relive", "behavior"}
STAMP = re.compile(r"^\d{8}-\d{6}$")
_OK_FALSE = re.compile(r'"ok":\s*false')
_CODE = re.compile(r'"code":\s*"([A-Za-z0-9_.-]{1,40})"')
_REASON = re.compile(r'"(?:reason|refused)":\s*"((?:[^"\\]|\\.){0,400})')


def outcome(out: str) -> tuple[bool, str | None, str | None]:
    """A tool's answer read the way the sender reads it: ok or refused, the refusal code, and its reason."""
    head = out[:2000]
    if not _OK_FALSE.search(head) and '"refused":' not in head:
        return True, None, None
    code, why = _CODE.search(head), _REASON.search(head)
    return False, code.group(1) if code else None, why.group(1) if why else None


def layer(tool: str, invented: set[str]) -> str:
    """Which layer a tool belongs to, as near as the files tell (the sender knows the world's own ops exactly)."""
    if tool == "act":
        return "verb"
    if tool == "menu":
        return "menu"
    if tool.startswith("app_"):
        return "app"
    if tool in invented:
        return "invented"
    if tool in HARNESS_TOOLS:
        return "harness"
    return "engine"


def settings_hash(cfg: dict[str, Any]) -> str:
    """The same hash the sender puts on its events, so imported and live rows of one setup group together."""
    keep = {k: v for k, v in cfg.items() if k not in ("telemetry", "session", "env_files", "extra_prompt")}
    return hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest()[:12]


def read_json(p: Path) -> dict[str, Any]:
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def lines(p: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """(raw line, parsed row) for each good line of a JSONL file; a torn last line is skipped."""
    try:
        f = p.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            if isinstance(o, dict):
                yield raw, o


def uid(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(x) for x in parts).encode()).hexdigest()


def seat_config(seat: Path) -> dict[str, Any]:
    """The home's defaults with the seat's settings over them, as body-harness merges them."""
    cfg, mine = read_json(seat.parent.parent / "config.json"), read_json(seat / "config.json")
    merged = {**cfg, **mine}
    merged["providers"] = {**cfg.get("providers", {}), **mine.get("providers", {})}
    merged["env_files"] = list(dict.fromkeys([*cfg.get("env_files", []), *mine.get("env_files", [])]))
    return merged


def seat_labels(seat: Path, machine: str, run: str) -> dict[str, Any]:
    cfg = seat_config(seat)
    brief = (read_json(seat / "state.json").get("briefing") or {}) if (seat / "state.json").exists() else {}
    sw = {k: bool(cfg.get(k)) for k in SWITCHES} | {"escalate": bool(cfg.get("escalate"))}
    return {"harness": "body-harness", "seat": seat.name, "settings": settings_hash(cfg), "switches": sw,
            "machine": machine, "run": run, "model": cfg.get("model") or None,
            "world": brief.get("world") or None, "character": brief.get("you") or None,
            "role": brief.get("role") or None}


# What a setup event keeps of a config: switches and numbers by any plain name, and these model names. Never prompts,
# providers, env files, telemetry or session settings, or anything named like a secret.
SETUP_MODELS = ("model", "judge", "escalate", "imagine_model", "think_model")
SETUP_NEVER = ("providers", "env_files", "extra_prompt", "extra", "telemetry", "session", "joined")
_SECRETISH = re.compile(r"key|token|secret|pass|auth|cookie|door|invite", re.I)


def safe_setup(cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if k in SETUP_NEVER or _SECRETISH.search(k) or not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k):
            continue
        if isinstance(v, bool) or (isinstance(v, (int, float)) and abs(v) < 1e9):
            out[k] = v
        elif k in SETUP_MODELS and isinstance(v, str) and len(v) <= 120:
            out[k] = v
        elif k in SETUP_MODELS and isinstance(v, list) and all(isinstance(x, str) and len(x) <= 120 for x in v):
            out[k] = v[:8]
    return out


def setup_event(seat: Path, base: dict[str, Any]) -> dict[str, Any]:
    """The seat's setup as one event, timed when its config last changed (a changed config is a new event)."""
    files = [p for p in (seat.parent.parent / "config.json", seat / "config.json") if p.exists()]
    setup = safe_setup(seat_config(seat))
    # an eval run holds many seats of one name: its sample labels tell them apart
    key = "|".join(str(base.get(k) or "") for k in ("machine", "run", "case", "scene", "variant", "sample")) + "|" + seat.name
    return {**base, "uid": uid("setup", key, json.dumps(setup, sort_keys=True)), "kind": "setup",
            "ts": max((p.stat().st_mtime for p in files), default=time.time()), "setup": setup}


def transcripts(run_dir: Path) -> dict[tuple[str, str, str, str], Path]:
    """A behavior eval's transcripts by (scenario, model slug, variant, rep), as the eval names them."""
    out = {}
    for p in (run_dir / "transcripts").glob("*.json") if (run_dir / "transcripts").is_dir() else ():
        parts = p.stem.split("__")
        if len(parts) == 4:
            out[tuple(parts)] = p
    return out


def seat_events(seat: Path, base: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every event a seat folder's files hold."""
    key = f"{base.get('machine')}|{base.get('run')}|{seat.name}"
    model = base.get("model")
    invented: set[str] = set()
    for raw, r in lines(seat / "invented.jsonl"):
        invented.add(str(r.get("tool")))
        yield {**base, "uid": uid("invented", key, raw), "ts": r.get("ts"), "kind": "invented",
               "model": r.get("model") or model, "tool": r.get("tool"), "read_as": r.get("read_as"),
               "arg_names": r.get("args") if isinstance(r.get("args"), list) else None}
    for raw, r in lines(seat / "usage.jsonl"):
        why = str(r.get("reason") or "")
        yield {**base, "uid": uid("usage", key, raw), "ts": r.get("ts"), "kind": "model_call",
               "model": r.get("model") or model, "use": "judge" if why.startswith("reflex") else "turn",
               **{k: r.get(k) for k in ("calls", "input_tokens", "cached_tokens", "cache_write_tokens",
                                        "output_tokens", "reasoning_tokens", "cost", "seconds")},
               "cost_known": r.get("cost_known"), "reason": why}
    for raw, r in lines(seat / "ledger.jsonl"):
        u = r.get("usage") if isinstance(r.get("usage"), dict) else {}
        calls = [c for c in (r.get("calls") or []) if isinstance(c, dict)]
        tools = [str(c.get("tool")) for c in calls]
        note = str(r.get("note") or "")
        by = u.get("by_model") if isinstance(u.get("by_model"), dict) else {}
        own = model or (max(by, key=lambda m: (by[m] or {}).get("calls", 0)) if by else None)
        yield {**base, "uid": uid("ledger", key, raw), "ts": r.get("ts"), "kind": "turn", "turn": r.get("turn"),
               "model": own, "steps": u.get("calls"), "tool_calls": len(calls),
               "deeds": sum(c.get("tool") in DEEDS and outcome(str(c.get("out") or ""))[0] for c in calls),
               "says": tools.count("say"), "reads": sum(t in READS for t in tools), "opens": tools.count("menu"),
               "nudges": r.get("nudges") or [], "escalated": r.get("escalated") or [],
               "ended": "out of steps" if note.startswith("(the turn ran out of steps") else
               "slept" if "sleep" in tools else "other",
               "imagined": r.get("imagined"), "world_t": r.get("t"),
               **{k: u.get(k) for k in ("input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens",
                                        "reasoning_tokens", "cost", "seconds")},
               "reason": r.get("reason"), "note": note}
    for raw, r in lines(seat / "calls.jsonl"):
        tool, out = str(r.get("tool") or ""), str(r.get("out") or "")
        ok, code, why = outcome(out)
        args = r.get("args") if isinstance(r.get("args"), dict) else {}
        yield {**base, "uid": uid("calls", key, raw), "ts": r.get("ts"), "kind": "tool_call", "turn": r.get("turn"),
               "tool": tool, "layer": layer(tool, invented), "verb": args.get("verb") if tool == "act" else None,
               "ok": ok, "code": code, "out_chars": len(out), "refusal": why}
    for raw, r in lines(seat / "reflexes.jsonl"):
        p = r.get("p") if isinstance(r.get("p"), dict) else {}
        yield {**base, "uid": uid("reflexes", key, raw), "ts": r.get("ts"), "kind": "reflex", "turn": r.get("turn"),
               "trigger": r.get("trigger"), "outcome": r.get("outcome"), "rules": len(p) or None,
               "do": r.get("do"), "judge_cost": r.get("cost"),
               "secs": (r["ms"] / 1000) if isinstance(r.get("ms"), (int, float)) else None,
               "refusal": r.get("refused"), "error": r.get("error"), "rule": r.get("rule"),
               "waiting_for": r.get("waiting_for"), "what_happened": r.get("what_happened")}
    for raw, r in lines(seat / "outbox.jsonl"):
        yield {**base, "uid": uid("outbox", key, raw), "ts": r.get("ts"), "kind": "tell", "event": r.get("kind"),
               "out_chars": len(str(r.get("text") or ""))}


def run_id(results_dir: Path) -> str:
    return f"{results_dir.parent.name}/{results_dir.name}"


def run_time(results_dir: Path) -> float:
    if STAMP.match(results_dir.name):
        return time.mktime(time.strptime(results_dir.name, "%Y%m%d-%H%M%S"))
    try:
        return (results_dir / "results.jsonl").stat().st_mtime
    except OSError:
        return time.time()


def rel_key(path: str, run_dir: Path) -> tuple[str, ...] | None:
    """A sample's seat folder below its run folder, as path parts that hold on any machine."""
    parts = [x for x in re.split(r"[\\/]+", path) if x]
    if run_dir.name not in parts:
        return None
    i = len(parts) - 1 - parts[::-1].index(run_dir.name)
    return tuple(parts[i + 1:])


def sample_labels(rel: tuple[str, ...]) -> dict[str, Any]:
    """case/[scene/]variant/NN/seats/name -> the sample's case, scene, variant and number."""
    if len(rel) < 4 or rel[-2] != "seats":
        return {}
    mid = rel[1:-3]
    try:
        n = int(rel[-3])
    except ValueError:
        n = None
    return {"case": rel[0], "scene": mid[0] if len(mid) > 1 else None, "variant": mid[-1] if mid else None,
            "sample": n}


def eval_events(results: Path, base: dict[str, Any], first_ts: dict[str, float]) -> Iterator[dict[str, Any]]:
    """One eval_sample per results.jsonl row: the harness suite's or the behavior eval's."""
    run_dir, run = results.parent, base.get("run") or run_id(results.parent)
    t0 = run_time(run_dir)
    graphs = transcripts(run_dir)
    for i, (raw, r) in enumerate(lines(results)):
        ev = {**base, "uid": uid("results", run, raw), "kind": "eval_sample", "run": run}
        if "scenario" in r:  # the behavior eval: one row per scenario, model, variant and rep; score of `of` wakes
            author = r.get("author") if isinstance(r.get("author"), dict) else {}
            wakes = [w for w in (r.get("wakes") or []) if isinstance(w, dict)]
            ev.update(ts=t0 + i * 1e-3, use="behavior", case=r.get("scenario"), level=r.get("level"),
                      model=r.get("model"), variant=r.get("variant") or None, sample=r.get("rep"),
                      passed=r.get("score"), trials=r.get("of"), misses=r.get("misses"), shape=r.get("shape"),
                      author={k: v for k, v in author.items() if isinstance(v, (int, float, bool)) or k == "ops_used"},
                      wakes=len(wakes), cost=(r.get("cost_author") or 0) + (r.get("cost_judge") or 0),
                      cost_author=r.get("cost_author"), cost_judge=r.get("cost_judge"), seconds=r.get("secs"),
                      waiting_for=author.get("waiting_for"))
            yield ev
            slug = str(r.get("model") or "").split(":")[-1].replace("/", "_")
            t = graphs.get((str(r.get("scenario")), slug, str(r.get("variant")), str(r.get("rep"))))
            g = read_json(t).get("graph") if t else None
            if isinstance(g, dict) and g.get("nodes"):
                yield {**base, "uid": uid("graph", run, raw), "kind": "behavior", "run": run, "ts": ev["ts"],
                       "use": "eval", "case": ev["case"], "model": ev["model"], "variant": ev.get("variant"),
                       "sample": ev["sample"], "graph": g}
            continue
        rel = rel_key(str(r.get("dir") or ""), run_dir) or ()
        seat_dir = run_dir.joinpath(*rel) if rel else None
        labels = seat_labels(seat_dir, base.get("machine", ""), run) if seat_dir and seat_dir.is_dir() else {}
        variant = str(r.get("variant") or "")
        scene, _, variant = variant.rpartition("/")
        ok = r.get("ok")
        tools = r.get("tools") if isinstance(r.get("tools"), list) else []
        ev.update({k: v for k, v in labels.items() if v is not None})
        ev.update(ts=first_ts.get(str(seat_dir), t0 + i * 1e-3), use="suite", case=r.get("case"),
                  scene=scene or None, variant=variant or None, sample=r.get("sample"),
                  passed=None if ok is None else bool(ok), trials=0 if ok is None else 1,
                  ended="error" if r.get("error") else None, tool_calls=len(tools), tools=tools,
                  nudges=r.get("nudges") or [], opens=r.get("opens"), fourth_wall=r.get("fourth_wall"),
                  model_calls=r.get("model_calls"), input_max=r.get("input_max"), input_sum=r.get("input_sum"),
                  cost=r.get("cost"), seconds=r.get("seconds"), detail=r.get("detail"), said=r.get("said"),
                  error=r.get("error"))
        yield ev


def find(root: Path) -> tuple[list[Path], list[Path]]:
    """Seat folders and results.jsonl files under root, without walking the folders that hold no counts."""
    seats, results = [], []
    if root.parent.name == "seats" and any((root / f).exists() for f in SEAT_FILES):
        return [root], []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        names = {e.name for e in entries}
        if "results.jsonl" in names:
            results.append(Path(d) / "results.jsonl")
        if Path(d).parent.name == "seats" and names & set(SEAT_FILES):
            seats.append(Path(d))
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False) and e.name not in SKIP:
                stack.append(Path(e.path))
    return sorted(seats), sorted(results)


def import_path(store: Store, root: str | Path, detail: str = "counts", machine: str = "", run: str = "",
                say: Callable[[str], None] = print) -> dict[str, int]:
    """Import everything under root. Returns new rows by kind. Importing the same files again adds nothing."""
    root = Path(root)
    seats, results = find(root)
    run_dirs = {r.parent for r in results}
    say(f"found {len(seats)} seat folder(s) and {len(results)} eval run(s) under {root.name or root}")
    got: dict[str, int] = {}
    first_ts: dict[str, float] = {}
    t = time.time()
    for n, seat in enumerate(seats, 1):
        owner = next((a for a in seat.parents if a in run_dirs), None)
        rn = run or (run_id(owner) if owner else "")
        base = seat_labels(seat, machine, rn)
        if owner:
            base.update({k: v for k, v in sample_labels(tuple(seat.relative_to(owner).parts)).items()
                         if v is not None})
        evs = list(seat_events(seat, base)) + [setup_event(seat, base)]
        ts = [e["ts"] for e in evs if isinstance(e.get("ts"), (int, float))]
        if ts:
            first_ts[str(seat)] = min(ts)
        _put(store, evs, detail, got)
        if n % 500 == 0 or time.time() - t > 15:
            say(f"  {n}/{len(seats)} seats, {sum(got.values())} new rows")
            t = time.time()
    for res in results:
        _put(store, list(eval_events(res, {"harness": "body-harness", "machine": machine, "run": run}, first_ts)),
             detail, got)
    return got


def _put(store: Store, evs: list[dict[str, Any]], detail: str, got: dict[str, int]) -> None:
    for e in evs:
        for k in [k for k, v in e.items() if v is None]:
            e.pop(k)
    by: dict[str, list[dict[str, Any]]] = {}
    for e in evs:
        by.setdefault(e["kind"], []).append(e)
    for kind, rows in by.items():
        new, _ = store.insert(rows, source="import", detail=detail, fill=False)
        got[kind] = got.get(kind, 0) + new
