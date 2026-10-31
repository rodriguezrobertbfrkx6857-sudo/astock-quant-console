# -*- coding: utf-8 -*-
"""QForgeLab command line interface.

  python -m qforge doctor          environment & connectivity checks
  python -m qforge bootstrap       first-run: calendar/securities/benchmarks/ETF
  python -m qforge sync X          sync one dataset (checkpointed, resumable)
  python -m qforge build           rebuild curated layer
  python -m qforge quality         data quality checks
  python -m qforge features        compute & store factor features
  python -m qforge research        run research queue (factors + strategies)
  python -m qforge eval-strategy   single strategy evaluation (subprocess entry)
  python -m qforge backtest        one-off backtest
  python -m qforge oos             OOS registry status / consume
  python -m qforge paper           run paper step now
  python -m qforge events          rebuild event store & study
  python -m qforge eod             run the full EOD chain now
  python -m qforge runtime         start the long-running scheduler
  python -m qforge server          start the local dashboard (127.0.0.1)
  python -m qforge replay          historical daily replay QA (N sessions)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .config import ROOT, assert_safety, get_settings
from .util import get_logger

log = get_logger("qforge.cli")


def cmd_doctor(args):
    import platform
    s = get_settings()
    from . import secrets as sec
    from .vendor import available_sources
    print("QForgeLab doctor")
    print(f"  python        : {platform.python_version()} ({sys.executable})")
    print(f"  version       : {__version__}")
    print(f"  data dir      : {s.data_dir}")
    print("  safety        : REAL_TRADING_ENABLED=False REAL_ORDER_ENABLED=False "
          "BROKER_ACCESS=False ACCOUNT_ACCESS=False BROKER_AUTH=False LOCAL_ONLY=True")
    print(f"  vendor keys   : {sec.describe()}")
    print(f"  sources ready : {available_sources()}")
    for mod in ("pandas", "numpy", "fastapi", "uvicorn", "requests", "pytest"):
        try:
            m = __import__(mod)
            print(f"  dep {mod:<10} : {getattr(m, '__version__', 'ok')}")
        except ImportError:
            print(f"  dep {mod:<10} : MISSING")
    # calendar state
    from .data import calendar as cal
    last = cal.last_open()
    print(f"  calendar      : {'ok, last open ' + last if last else 'EMPTY (run bootstrap/sync trade_cal)'}")
    if args.probe and available_sources():
        from .vendor import get_client
        for src in available_sources():
            try:
                rows = get_client(src).fetch("trade_cal", {"exchange": "SSE",
                                                           "start_date": "20260101",
                                                           "end_date": "20260201"})
                print(f"  probe {src:<9} : ok ({len(rows)} rows)")
            except Exception as e:
                print(f"  probe {src:<9} : FAILED {e}")
    return 0


def cmd_bootstrap(args):
    from .data import sync as sync_mod
    print("[1/5] trade calendar ...")
    print(" ", sync_mod.sync_trade_cal())
    print("[2/5] securities (L/D/P) ...")
    print(" ", sync_mod.sync_stock_basic())
    print("[3/5] benchmark indices ...")
    print(" ", sync_mod.sync_index_daily())
    print("[4/5] ETF list & bars ...")
    print(" ", sync_mod.sync_fund_basic())
    print(" ", sync_mod.sync_etf_daily())
    print("[5/5] name change history (ST) ...")
    print(" ", sync_mod.sync_namechange())
    print("bootstrap done. Next: `python -m qforge sync daily` (long), then build/quality/features/research")
    return 0


def cmd_sync(args):
    from .data import sync as sync_mod
    res = sync_mod.run_sync(args.dataset, start=args.start, end=args.end, codes=args.codes)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_build(args):
    from .data import build as build_mod
    print(json.dumps(build_mod.build_all(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_quality(args):
    from .data import quality as quality_mod
    for r in quality_mod.run_checks():
        print(f"  {r['dataset']:<18} {r['status']:<6} rows={r['rows_total']:>9} "
              f"max={r['max_date']} stale={r['stale_days']}d score={r['quality_score']:.0f}")
    return 0


def cmd_features(args):
    from .factors import compute_and_store
    res = compute_and_store(start=args.start, end=args.end)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_research(args):
    raise RuntimeError("legacy research is disabled by S03; V2 Stage09 is introduced in S06")
    from .research import queue as rq
    rq.seed_queue()
    out = rq.process_queue(max_items=args.items)
    for r in out:
        item = r["item"]
        verdict = r["result"].get("verdict") if isinstance(r["result"], dict) else None
        print(f"  [{r['status']:<7}] {item['kind']:<8} {item['target']:<22} {verdict or ''}")
    return 0


def cmd_eval_strategy(args):
    raise RuntimeError("legacy evaluation is disabled by S03; V2 Stage09 is introduced in S06")
    from .research import evaluator
    res = evaluator.evaluate_strategy(args.id)
    print(json.dumps({"verdict": res.get("verdict"), "gates": res.get("gates")},
                     ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_backtest(args):
    raise RuntimeError("legacy backtest entry is disabled by S03 OOS policy")
    from .research import evaluator
    bt_id, res = evaluator.run_backtest(args.strategy, json.loads(args.params or "{}"),
                                        args.start, args.end, scope=args.scope,
                                        label=args.label, consume_oos=args.consume_oos,
                                        persist=not args.no_save)
    print(json.dumps(res.summary(), ensure_ascii=False, indent=2, default=str))
    print(f"bt_id={bt_id}")
    return 0


def cmd_oos(args):
    raise RuntimeError("legacy OOS registry is disabled; use V2 global exposure ledger")
    from .research import oos
    if args.consume:
        from .research import evaluator
        print(json.dumps(evaluator.consume_oos_and_promote(args.strategy),
                         ensure_ascii=False, indent=2, default=str))
    else:
        print(json.dumps(oos.summary(), ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_paper(args):
    from .paper import engine as paper
    out = paper.paper_step(args.date)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_events(args):
    raise RuntimeError("legacy event study is disabled until V2 event authorization is available")
    from .events import build_event_store, event_study
    print(json.dumps({"store": build_event_store(), "study": event_study()},
                     ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_eod(args):
    from .runtime.jobs import run_eod_chain
    out = run_eod_chain(args.date, jobs=args.jobs.split(",") if args.jobs else None)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if all(v.get("status") != "FAILED" for v in out["jobs"].values()) else 1


def cmd_runtime(args):
    assert_safety()
    from .runtime.scheduler import main as runtime_main
    return runtime_main()


def cmd_server(args):
    import os
    import uvicorn
    s = get_settings()
    host = "127.0.0.1"  # LOCAL_ONLY: never bind anything else
    port = args.port or s.dashboard_port
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "server.pid").write_text(str(os.getpid()), encoding="utf-8")
    print(f"QForgeLab dashboard: http://{host}:{port}  (Ctrl+C to stop)")
    uvicorn.run(app="qforge.api.app:app", host=host, port=port, log_level="warning")
    return 0


def cmd_replay(args):
    """Historical daily replay: run the paper logic over the last N sessions
    deterministically and verify ledger consistency (QA)."""
    from .data import calendar as cal
    from .paper import engine as paper
    od = cal.open_days()
    if len(od) < args.days + 1:
        print("not enough sessions for replay")
        return 1
    ok = True
    for d in od[-args.days:]:
        out = paper.paper_step(d)
        cons = paper.ledger_consistency()
        if not cons.get("ok", False):
            ok = False
            print(f"  replay {d}: INCONSISTENT {cons}")
        else:
            print(f"  replay {d}: ok")
    print("replay:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main(argv=None) -> int:
    assert_safety()
    p = argparse.ArgumentParser("qforge", description="A-share quant research & paper platform")
    p.add_argument("--version", action="version", version=f"qforge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def sp(name, fn, help_):
        q = sub.add_parser(name, help=help_)
        q.set_defaults(fn=fn)
        return q

    q = sp("doctor", cmd_doctor, "environment checks"); q.add_argument("--probe", action="store_true")
    sp("bootstrap", cmd_bootstrap, "first-run core data")
    q = sp("sync", cmd_sync, "sync one dataset"); q.add_argument("dataset")
    q.add_argument("--start"); q.add_argument("--end"); q.add_argument("--codes", nargs="*")
    sp("build", cmd_build, "rebuild curated layer")
    sp("quality", cmd_quality, "data quality checks")
    q = sp("features", cmd_features, "compute factor features")
    q.add_argument("--start"); q.add_argument("--end")
    q = sp("research", cmd_research, "process research queue"); q.add_argument("--items", type=int, default=10)
    q = sp("eval-strategy", cmd_eval_strategy, "evaluate one strategy"); q.add_argument("--id", required=True)
    q = sp("backtest", cmd_backtest, "one-off backtest")
    q.add_argument("--strategy", required=True); q.add_argument("--start", required=True)
    q.add_argument("--end", required=True); q.add_argument("--params", default="{}")
    q.add_argument("--scope", default="research"); q.add_argument("--label", default="adhoc")
    q.add_argument("--consume-oos", action="store_true"); q.add_argument("--no-save", action="store_true")
    q = sp("oos", cmd_oos, "OOS registry"); q.add_argument("--consume", action="store_true")
    q.add_argument("--strategy")
    q = sp("paper", cmd_paper, "run paper step"); q.add_argument("--date")
    sp("events", cmd_events, "rebuild event intelligence")
    q = sp("eod", cmd_eod, "run EOD chain now"); q.add_argument("--date")
    q.add_argument("--jobs", help="comma list subset: sync,build,quality,features,events,research,paper,validate")
    sp("runtime", cmd_runtime, "start scheduler runtime")
    q = sp("server", cmd_server, "start local dashboard"); q.add_argument("--port", type=int)
    q = sp("replay", cmd_replay, "historical daily replay QA"); q.add_argument("--days", type=int, default=5)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
