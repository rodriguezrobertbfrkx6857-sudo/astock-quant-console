# -*- coding: utf-8 -*-
"""Runtime job chain: Data Sync -> Quality -> Universe build -> Features ->
Events -> Research -> PAPER -> Validation.

Vendor failure isolation: each sync dataset fails independently; the chain
continues and alerts. The EOD chain runs fully offline (no realtime feed, no
LLM) -- it only needs already-synced local data to complete.
"""
from __future__ import annotations

import subprocess
import sys
import traceback

from .. import db
from ..config import get_settings
from ..data import calendar as cal
from ..data import build as build_mod
from ..data import sync as sync_mod
from ..data import quality as quality_mod
from ..util import get_logger, today_str, ymd
from ..vendor import VendorError
from .ledger import RunLedger

log = get_logger("qforge.jobs")

SYNC_RECHECK_DAYS = 4  # always re-sync the last few sessions (idempotent)

SYNC_DATASETS = [
    ("trade_cal", {}), ("stock_basic", {}), ("namechange", {}),
    ("daily", {}), ("adj_factor", {}), ("daily_basic", {}),
    ("stk_limit", {}), ("suspend_d", {}), ("index_daily", {}),
    ("fund_basic", {}), ("etf_daily", {}),
]

INCREMENTAL = {"daily", "adj_factor", "daily_basic", "stk_limit", "suspend_d"}


def _bdate_guard(bdate: str | None) -> str:
    if bdate:
        return ymd(bdate)
    last = cal.last_open()
    if not last:
        raise RuntimeError("empty calendar")
    return last


# ------------------------------------------------------------------ sync ----
def job_sync(bdate: str | None = None, ledger: RunLedger | None = None) -> dict:
    bdate = _bdate_guard(bdate)
    s = get_settings()
    results = {}
    # calendar first (everything depends on it)
    try:
        results["trade_cal"] = sync_mod.sync_trade_cal()
        cal.invalidate_cache()
    except Exception as e:
        log.error("trade_cal sync failed: %s", e)
        if ledger:
            ledger.alert("ERROR", "sync", f"trade_cal failed: {e}")
        raise
    # last few sessions are always re-fetched (idempotent): sessions whose data
    # was still unpublished at the previous run get picked up
    # weekly-refresh datasets (idempotent full fetches)
    wk = db.kv_get(db.connect_market(), "sync:weekly_done") or ""
    weekly = {"stock_basic", "namechange", "fund_basic"}
    for name, kw in SYNC_DATASETS:
        if name in weekly:
            import datetime as dt
            if wk >= (dt.date.today() - dt.timedelta(days=6)).isoformat():
                results[name] = {"skipped": "weekly fresh"}
                continue
        try:
            if name in INCREMENTAL:
                results[name] = sync_mod.run_sync(name, recheck_days=SYNC_RECHECK_DAYS)
            else:
                results[name] = sync_mod.run_sync(name)
        except VendorError as e:
            # vendor failure isolation: alert and continue
            results[name] = {"error": str(e)[:200]}
            if ledger:
                ledger.alert("ERROR", "sync", f"{name}: {e}")
            log.error("sync %s failed (isolated): %s", name, e)
        except Exception as e:
            results[name] = {"error": str(e)[:200]}
            if ledger:
                ledger.alert("ERROR", "sync", f"{name}: {e}")
            log.exception("sync %s failed", name)
    if all(not results.get(n, {}).get("error") for n in weekly):
        db.kv_set(db.connect_market(), "sync:weekly_done", str(__import__("datetime").date.today()))
    return {"bdate": bdate, "results": results}


# ------------------------------------------------------------------ build/quality ----
def job_build(bdate: str | None = None, **kw) -> dict:
    _bdate_guard(bdate)
    out = build_mod.build_all()
    return out


def job_quality(bdate: str | None = None, **kw) -> dict:
    _bdate_guard(bdate)
    checks = quality_mod.run_checks()
    return {"datasets": {c["dataset"]: c["status"] for c in checks}}


# ------------------------------------------------------------------ features ----
def job_features(bdate: str | None = None, **kw) -> dict:
    bdate = _bdate_guard(bdate)
    s = get_settings()
    from ..factors import compute_and_store
    mk = db.connect_market()
    try:
        wm = db.kv_get(mk, "features:last_date")
    finally:
        mk.close()
    # incremental: recompute a warm window ending at bdate
    if wm:
        import datetime as dt
        start = (dt.datetime.strptime(wm, "%Y%m%d") - dt.timedelta(days=400)).strftime("%Y%m%d")
    else:
        start = s.history_start
    res = compute_and_store(start=start, end=bdate)
    mk = db.connect_market()
    try:
        db.kv_set(mk, "features:last_date", bdate)
    finally:
        mk.close()
    return res


# ------------------------------------------------------------------ events ----
def job_events(bdate: str | None = None, ledger: RunLedger | None = None) -> dict:
    bdate = _bdate_guard(bdate)
    out = {}
    try:
        out["anns"] = sync_mod.sync_announcements()
    except Exception as e:
        out["anns"] = {"error": str(e)[:150]}
        if ledger:
            ledger.alert("WARN", "events", f"anns sync: {e}")
    try:
        out["news"] = sync_mod.sync_news()
    except Exception as e:
        out["news"] = {"error": str(e)[:150]}
    from ..events import build_event_store, event_study
    out["store"] = build_event_store()
    try:
        out["study"] = event_study()
    except Exception as e:
        out["study"] = {"error": str(e)[:150]}
    return out


# ------------------------------------------------------------------ research ----
def job_research(bdate: str | None = None, ledger: RunLedger | None = None) -> dict:
    """Queue-driven research with subprocess isolation for strategy evals."""
    _bdate_guard(bdate)
    from ..research import queue as rq
    rq.seed_queue()
    items = rq.pending_items(30)
    out = {"processed": [], "subprocess": True}
    ran = 0
    for item in items:
        if ran >= 8:
            break
        kind, target = item["kind"], item["target"]
        if kind == "factor":
            try:
                res = rq.analyze_factor(target)
                _finish_item(item, "DONE", res)
                out["processed"].append({"item": target, "ok": True})
            except Exception as e:
                _finish_item(item, "FAILED", {"error": str(e)[:200]})
                out["processed"].append({"item": target, "ok": False, "error": str(e)[:100]})
            ran += 1
        elif kind == "strategy":
            code = _run_isolated_eval(target)
            ran += 1
            status = "DONE" if code == 0 else "FAILED"
            _finish_item(item, status, {"subprocess_exit": code})
            out["processed"].append({"item": target, "subprocess_exit": code})
    return out


def _finish_item(item: dict, status: str, result: dict) -> None:
    lab = db.connect_lab()
    try:
        lab.execute("UPDATE research_queue SET status=?, result_json=?, updated_at=? WHERE item_id=?",
                    (status, db.json_dumps(result)[:4000], today_str(), item["item_id"]))
    finally:
        lab.close()


def _run_isolated_eval(strategy_id: str) -> int:
    """Strategy evaluation in a subprocess: a crash cannot take the pipeline down."""
    python = sys.executable
    try:
        proc = subprocess.run(
            [python, "-m", "qforge", "eval-strategy", "--id", strategy_id],
            capture_output=True, text=True,
            timeout=get_settings().strategy_subprocess_timeout, cwd=str(
                __import__("pathlib").Path(__file__).resolve().parents[2]))
        if proc.returncode != 0:
            if proc.stderr:
                log.error("eval subprocess %s stderr: %s", strategy_id, proc.stderr[-500:])
        return proc.returncode
    except subprocess.TimeoutExpired:
        log.error("eval subprocess %s timed out", strategy_id)
        return 124
    except Exception as e:
        log.error("eval subprocess %s failed to launch: %s", strategy_id, e)
        return 1


# ------------------------------------------------------------------ paper ----
def job_paper(bdate: str | None = None, **kw) -> dict:
    bdate = _bdate_guard(bdate)
    from ..paper import engine as paper
    out = paper.paper_step(bdate)
    out["consistency"] = paper.ledger_consistency()
    return out


# ------------------------------------------------------------------ validation ----
def job_validate(bdate: str | None = None, **kw) -> dict:
    bdate = _bdate_guard(bdate)
    from ..validate import reference as ref
    lab = db.connect_lab()
    try:
        rows = db.q(lab, """
            SELECT b.bt_id FROM backtests b
            JOIN evaluations e ON e.strategy_id = b.strategy_id
            WHERE b.scope='research' AND e.verdict IN ('PAPER','CANDIDATE')
            GROUP BY b.strategy_id HAVING MAX(b.created_at)""")
        ids = [r["bt_id"] for r in rows][:5]
    finally:
        lab.close()
    out = {}
    for bt_id in ids:
        try:
            out[bt_id] = ref.validate_backtest(bt_id)["verdict"]
        except Exception as e:
            out[bt_id] = f"ERROR: {e}"
    return {"validated": out}


# ------------------------------------------------------------------ EOD chain ----
EOD_CHAIN = ["sync", "build", "quality", "features", "events", "research", "paper", "validate"]


def run_eod_chain(bdate: str | None = None, jobs: list[str] | None = None) -> dict:
    bdate = _bdate_guard(bdate)
    ledger = RunLedger()
    out = {"bdate": bdate, "jobs": {}}
    try:
        ledger.recover_stale()
        for name in EOD_CHAIN:
            if jobs and name not in jobs:
                continue
            # resolve at run time so tests can isolate/monkeypatch individual jobs
            fn = globals()[f"job_{name}"]
            run_id, action = ledger.claim(name, bdate)
            if action != "run":
                out["jobs"][name] = {"status": action}
                continue
            ledger.start(run_id)
            try:
                if name == "sync":
                    res = fn(bdate, ledger=ledger)
                else:
                    res = fn(bdate)
                ledger.heartbeat(run_id, checkpoint={"done": True})
                ledger.finish(run_id, ok=True)
                out["jobs"][name] = {"status": "SUCCEEDED", "result": _safe_small(res)}
            except Exception as e:
                log.exception("job %s failed", name)
                ledger.finish(run_id, ok=False, error=str(e)[:500],
                              checkpoint={"traceback": traceback.format_exc()[-1000:]})
                ledger.alert("ERROR", name, str(e)[:300])
                out["jobs"][name] = {"status": "FAILED", "error": str(e)[:200]}
    finally:
        ledger.close()
    try:
        from .notify import notify_eod_report
        notify_eod_report(bdate, out)
    except Exception:
        pass
    return out


def _safe_small(res) -> dict:
    """Trim job results for ledger storage."""
    try:
        s = db.json_dumps(res)
        if len(s) > 3000:
            return {"truncated": s[:3000]}
        return res if isinstance(res, dict) else {"value": str(res)[:500]}
    except Exception:
        return {"unserializable": True}
