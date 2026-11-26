"""body-telemetry: serve, import, query.

    body-telemetry serve  [--db telemetry.db] [--host 127.0.0.1] [--port 8899] [--key-env BODY_TELEMETRY_KEY]
    body-telemetry import <folder> [--db telemetry.db] [--detail counts|lines|full] [--machine LABEL] [--run ID]
    body-telemetry query  measure=spend by=seat,model bucket=day since=7d [--db telemetry.db]
    body-telemetry stats  [--db telemetry.db]"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import __version__
from .importer import import_path
from .query import MEASURES, QueryError, run_query
from .store import DETAIL, Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="body-telemetry", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    db_default = os.environ.get("BODY_TELEMETRY_DB", "telemetry.db")

    s = sub.add_parser("serve", help="take events and serve queries and the dashboard")
    s.add_argument("--db", default=db_default)
    s.add_argument("--host", default=os.environ.get("BODY_TELEMETRY_HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT") or os.environ.get("BODY_TELEMETRY_PORT")
                                                    or 8899))
    s.add_argument("--key-env", default="BODY_TELEMETRY_KEY", help="the environment variable holding the key")
    s.add_argument("--max-detail", choices=DETAIL, default="full",
                   help="cut words in what arrives to this level before it is stored (default: keep what is sent)")
    s.add_argument("--open-read", action="store_true", help="let anyone read the dashboard and queries; posting "
                                                               "still needs the key")

    i = sub.add_parser("import", help="read the files body-harness already writes")
    i.add_argument("folder", nargs="+")
    i.add_argument("--db", default=db_default)
    i.add_argument("--detail", choices=DETAIL, default="counts")
    i.add_argument("--machine", default="", help="a label for the machine these files came from")
    i.add_argument("--run", default="", help="a run id for every row (eval folders get their own by default)")

    qp = sub.add_parser("query", help="one query, printed as JSON")
    qp.add_argument("params", nargs="*", help="name=value pairs, as GET /v1/query takes them")
    qp.add_argument("--db", default=db_default)

    st = sub.add_parser("stats", help="rows by kind and source")
    st.add_argument("--db", default=db_default)

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from .server import serve
        return serve(a.db, a.host, a.port, os.environ.get(a.key_env, ""), a.max_detail, a.open_read)
    store = Store(a.db)
    if a.cmd == "import":
        t = time.time()
        total: dict[str, int] = {}
        for f in a.folder:
            got = import_path(store, f, a.detail, a.machine, a.run)
            for k, v in got.items():
                total[k] = total.get(k, 0) + v
        print(f"new rows: {sum(total.values())} " + json.dumps(dict(sorted(total.items())))
              + f" in {time.time() - t:.0f}s; {store.count()} rows in {a.db}")
        return 0
    if a.cmd == "query":
        params = dict(p.split("=", 1) for p in a.params if "=" in p)
        try:
            print(json.dumps(run_query(store, params), indent=1))
        except QueryError as e:
            print(f"error: {e}\nmeasures: {', '.join(MEASURES)}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "stats":
        for r in store.execute("SELECT kind, source, COUNT(*) n, MIN(ts) a, MAX(ts) b FROM events "
                               "GROUP BY kind, source ORDER BY n DESC"):
            print(f"{r['kind']:12} {r['source']:7} {r['n']:>9}  "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['a']))} .. "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(r['b']))} UTC")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
