import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from body_telemetry import importer, query, server
from body_telemetry.flow import flow
from body_telemetry.helpers import helpers
from body_telemetry.store import Store, normalize

LABELS = {"harness": "body-harness", "version": "0.1.0", "settings": "abc123def456", "machine": "", "run": "",
          "switches": {"menu": True, "reflexes": False}, "seat": "fen"}


def ev(kind, **kw):
    return {"ts": 1795700000.0 + kw.pop("dt", 0), "kind": kind, **LABELS, **kw}


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_counts_keeps_no_words_and_unknown_fields_go_to_extra(self):
        r = normalize(ev("turn", note="Bo owes me two coin", reason="to_me()", new_field=7), detail="counts")
        self.assertNotIn("note", json.dumps(r))
        self.assertEqual(json.loads(r["extra"])["new_field"], 7)
        self.assertEqual(r["switches"], "menu")
        r = normalize(ev("turn", note="x" * 300), detail="lines")
        self.assertEqual(len(json.loads(r["extra"])["note"]), 120)

    def test_the_same_event_twice_is_one_row_and_labels_fill_from_the_seat(self):
        start = ev("seat", event="start", model="openrouter:maker/m-1", world="A Village", character="Fen")
        call = ev("tool_call", dt=1, tool="act", layer="verb", verb="give", ok=False, code="no_target")
        self.assertEqual(self.store.insert([start, call]), (2, 0))
        self.assertEqual(self.store.insert([call, {"not": "an event"}]), (0, 1))
        row = self.store.execute("SELECT * FROM events WHERE kind = 'tool_call'")[0]
        self.assertEqual((row["model"], row["world"], row["provider"], row["maker"], row["ok"]),
                         ("openrouter:maker/m-1", "A Village", "openrouter", "maker", 0))

    def test_queries_group_filter_bucket_and_give_intervals(self):
        self.store.insert([ev("model_call", dt=i * 3600, model="p:a/x" if i % 2 else "p:b/y", cost=0.01,
                              input_tokens=1000 + i, cached_tokens=500) for i in range(10)])
        self.store.insert([ev("eval_sample", dt=i, case="c1", variant="A" if i < 6 else "B", passed=i % 3 != 0,
                              trials=1, run="grid/1") for i in range(12)])
        out = query.run_query(self.store, {"measure": "spend", "by": "model"})
        self.assertEqual(sorted(round(r["value"], 3) for r in out["rows"]), [0.05, 0.05])
        out = query.run_query(self.store, {"measure": "cache_hit", "bucket": "day"})
        self.assertTrue(all(0.49 < r["value"] < 0.5 for r in out["rows"]))
        out = query.run_query(self.store, {"measure": "pass_rate", "by": "variant", "variant": "A"})
        (a,) = out["rows"]
        self.assertAlmostEqual(a["value"], 4 / 6)
        self.assertTrue(a["lo"] < a["value"] < a["hi"])
        out = query.run_query(self.store, {"measure": "p50:input_tokens", "by": "x.nope,sw.menu"})
        self.assertEqual(out["rows"][0]["value"], 1004.5)
        with self.assertRaises(query.QueryError):
            query.run_query(self.store, {"measure": "sum:cost; DROP TABLE events"})
        with self.assertRaises(query.QueryError):
            query.run_query(self.store, {"measure": "spend", "by": "x.a'b"})

    def test_nudge_words_stay_home_at_counts(self):
        r = normalize(ev("turn", nudges=["quiet: Ada, Bo", "stuck: 'the well' in 3 of the last 6 notes"]), detail="counts")
        self.assertNotIn("Ada", r["extra"])
        self.assertEqual((r["nudges"], json.loads(r["extra"])["nudge_kinds"]), (2, ["quiet", "stuck"]))

    def test_the_senders_newer_events(self):
        evs = [ev("seat", event="start", turns=0, model="p:m/x", uid="u1"),
               ev("setup", dt=1, uid="u2", setup={"model": "p:m/x", "menu": True, "max_steps": 8,
                                                 "prices": {"p:m/x": [1, 2]}, "api_key_env": "MY_KEY", "extra_prompt": "hi"}),
               ev("model_call", dt=2, uid="u3", model="p:m/x", use="turn", cost=0.001,
                  ctx={"system": 4000, "tools": 2500, "wake": 300, "history": 9000, "tool_results": 1200}),
               ev("turn", dt=3, uid="u4", turn=1, nudges=2, nudge_kinds=["stuck", "quiet"], ended="slept"),
               ev("tell", dt=4, uid="u5", to="operator", about="flaw", out_chars=120),
               ev("seat", dt=5, uid="u6", event="error", error="TimeoutError"),
               ev("seat", dt=6, uid="u7", event="stop", why="grant refused", status=403)]
        self.assertEqual(self.store.insert(evs, detail="counts"), (7, 0))
        self.assertEqual(self.store.insert(evs, detail="counts"), (0, 0))  # a repost adds nothing
        setup = json.loads(self.store.execute("SELECT extra FROM events WHERE kind = 'setup'")[0][0])["setup"]
        self.assertEqual(setup, {"model": "p:m/x", "menu": True, "max_steps": 8})
        out = query.run_query(self.store, {"measure": "ctx_history"})
        self.assertEqual(out["rows"][0]["value"], 9000)
        self.assertEqual(self.store.execute("SELECT code FROM events WHERE event = 'error'")[0][0], "TimeoutError")
        (s,) = query.seats(self.store, {})
        self.assertEqual((s["state"], s["errors"]), ("stopped", 1))
        self.assertEqual(helpers(self.store, {})["nudge_kinds"], [{"kind": "stuck", "n": 1}, {"kind": "quiet", "n": 1}])
        with self.assertRaisesRegex(query.QueryError, "Measures: events, seats, spend"):
            query.run_query(self.store, {"measure": "cost"})

    def test_a_behavior_graph_keeps_its_shape_at_counts(self):
        g = {"start": "spoken", "nodes": {
            "spoken": {"kind": "check", "if": "heard('salt', by='Joss')", "yes": "sell", "no": "SLEEP"},
            "sell": {"kind": "do", "do": "act", "verb": "give", "target": "Joss", "then": "mood"},
            "mood": {"kind": "choose", "q": "is Joss angry?", "options": {"angry": "WAKE", "calm": "SLEEP"}}},
            "fired": {"spoken": 3, "sell": 1}}
        r = normalize(ev("behavior", graph=g), detail="counts")
        shape = json.loads(r["extra"])["graph"]
        for words in ("salt", "Joss", "angry", "spoken", "mood"):
            self.assertNotIn(words, r["extra"])
        self.assertEqual(shape["start"], "n1")
        self.assertEqual(shape["nodes"]["n2"], {"kind": "do", "do": "act", "verb": "give", "then": "n3"})
        self.assertEqual(shape["nodes"]["n3"]["options"], {"a1": "WAKE", "a2": "SLEEP"})
        self.assertEqual(shape["fired"], {"n1": 3, "n2": 1})
        self.assertIn("Joss", normalize(ev("behavior", graph=g), detail="lines")["extra"])

    def test_flow_follows_calls_through_a_turn(self):
        calls = [("look", 1), ("act", 0), ("act", 1), ("sleep", 1), ("look", 1), ("sleep", 1)]
        self.store.insert([ev("tool_call", dt=i, turn=1 if i < 4 else 2, tool=t, ok=ok, verb="give" if t == "act" else None)
                           for i, (t, ok) in enumerate(calls)])
        f = flow(self.store, {"depth": "3"})
        self.assertEqual((f["paths"], f["turns"]), (2, 2))
        self.assertEqual([(n["name"], n["n"]) for n in f["steps"][1]], [("look", 2)])
        self.assertIn({"step": 1, "from": "look", "to": "act", "n": 1, "share": 0.5}, f["links"])
        f = flow(self.store, {"start": "act", "depth": "2", "node": "verb"})
        self.assertEqual(f["paths"], 2)
        self.assertEqual(f["steps"][0], [{"name": "act:give", "n": 2, "refused": 1}])
        self.assertIn({"step": 1, "from": "sleep", "to": "(turn ends)", "n": 1, "share": 1.0}, f["links"])

    def test_helpers_count_who_has_each_on_and_how_often_it_is_used(self):
        on = dict(LABELS["switches"], imagine=True, escalate=True)
        self.store.insert([ev("turn", dt=i, turn=i, switches=on, escalated=["p:big/m"] if i == 0 else [], ended="slept")
                           for i in range(4)] + [ev("tool_call", dt=i, turn=i, tool="imagine") for i in range(2)])
        h = {x["key"]: x for x in helpers(self.store, {})["helpers"]}
        self.assertEqual((h["imagine"]["seats_on"], h["imagine"]["uses"], h["imagine"]["per_100_turns"]), (1, 2, 50.0))
        self.assertEqual((h["escalate"]["uses"], h["sleep"]["uses"], h["think_tool"]["seats_on"]), (1, 4, 0))

    def test_import_reads_a_seat_and_an_eval_run_without_speech(self):
        root = Path(self.dir.name) / "evals" / "grid" / "20261126-120000"
        seat = root / "case-a" / "busy" / "V1" / "00" / "seats" / "fen"
        seat.mkdir(parents=True)
        (seat / "config.json").write_text(json.dumps({"model": "p:m/x", "menu": True, "max_steps": 8,
                                                       "extra_prompt": "a secret prompt", "key_env": "MY_KEY"}))
        (seat / "door.json").write_text("{\"token\": \"never read\"}")
        (seat / "usage.jsonl").write_text(json.dumps({"ts": 1795700000.0, "seat": "fen", "model": "p:m/x",
                                                      "reason": "heard someone", "calls": 1, "input_tokens": 900,
                                                      "cached_tokens": 0, "output_tokens": 50, "cost": 0.001}) + "\n")
        (seat / "calls.jsonl").write_text(
            json.dumps({"ts": 1795700001.0, "turn": 1, "tool": "act", "args": {"verb": "give", "target": "Ada"},
                        "out": '{"ok": false, "reason": "you hold no salt", "code": "no_item"}'}) + "\n" +
            json.dumps({"ts": 1795700002.0, "turn": 1, "tool": "say", "args": {"text": "hello"}, "out": "{}"}) + "\n")
        (seat / "ledger.jsonl").write_text(json.dumps({
            "ts": 1795700003.0, "turn": 1, "reason": "heard someone", "note": "a private note",
            "calls": [{"tool": "act", "out": '{"ok": false}'}, {"tool": "say", "out": "{}"},
                      {"tool": "sleep", "out": "{}"}], "nudges": ["steps_left: 3"],
            "usage": {"calls": 2, "input_tokens": 1800, "cost": 0.002}}) + "\n")
        (root / "results.jsonl").write_text(json.dumps({
            "case": "case-a", "variant": "busy/V1", "sample": 0, "dir": str(seat), "ok": True,
            "said": "words of the character", "tools": ["act", "say"], "nudges": [], "cost": 0.003}) + "\n")
        got = importer.import_path(self.store, Path(self.dir.name) / "evals", say=lambda s: None)
        self.assertEqual(got, {"model_call": 1, "tool_call": 2, "turn": 1, "eval_sample": 1, "setup": 1})
        setup = json.loads(self.store.execute("SELECT extra FROM events WHERE kind = 'setup'")[0][0])["setup"]
        self.assertEqual(setup, {"model": "p:m/x", "menu": True, "max_steps": 8})
        again = importer.import_path(self.store, Path(self.dir.name) / "evals", say=lambda s: None)
        self.assertEqual(sum(again.values()), 0)
        dump = json.dumps([dict(r) for r in self.store.execute("SELECT * FROM events")])
        for words in ("private note", "words of the character", "heard someone", "hello", "never read", "secret prompt",
                      "MY_KEY"):
            self.assertNotIn(words, dump)
        t = self.store.execute("SELECT * FROM events WHERE kind = 'turn'")[0]
        self.assertEqual((t["deeds"], t["says"], t["nudges"], t["ended"], t["case"], t["scene"], t["variant"],
                          t["run"], t["switches"]), (1, 1, 1, "slept", "case-a", "busy", "V1", "grid/20261126-120000",
                                                     "menu"))
        c = self.store.execute("SELECT * FROM events WHERE kind = 'tool_call' AND tool = 'act'")[0]
        self.assertEqual((c["ok"], c["code"], c["verb"], c["layer"]), (0, "no_item", "give", "verb"))
        s = self.store.execute("SELECT * FROM events WHERE kind = 'eval_sample'")[0]
        self.assertEqual((s["model"], s["passed"], s["trials"], s["scene"]), ("p:m/x", 1, 1, "busy"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.dir.name) / "t.db")
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.handler(server.App(self.store, key="k1")))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_port}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.store.close()
        self.dir.cleanup()

    def call(self, path, body=None, key="k1"):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_takes_a_batch_with_the_key_and_answers_queries(self):
        batch = {"events": [ev("model_call", dt=i, model="p:a/x", cost=0.5) for i in range(3)] + ["junk"]}
        self.assertEqual(self.call("/v1/events", batch, key="wrong")[0], 401)
        self.assertEqual(self.call("/v1/events", batch), (200, {"ok": True, "taken": 3, "new": 3, "skipped": 1}))
        self.assertEqual(self.call("/v1/events", batch)[1]["new"], 0)
        code, out = self.call("/v1/query?measure=spend&by=seat")
        self.assertEqual((code, out["rows"][0]["value"]), (200, 1.5))
        self.assertEqual(self.call("/v1/query?measure=nope")[0], 400)
        self.assertEqual(self.call("/v1/meta")[1]["kinds"], {"model_call": 3})
        code, out = self.call("/v1/events?measure=spend&seat=fen&limit=2")
        self.assertEqual((code, out["total"], len(out["rows"]), out["rows"][0]["cost"]), (200, 3, 2, 0.5))

    def test_will_not_listen_wide_without_a_key(self):
        self.assertEqual(server.serve(str(Path(self.dir.name) / "x.db"), "0.0.0.0", 0, key=""), 2)


if __name__ == "__main__":
    unittest.main()
