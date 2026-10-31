"""V2 PAPER admission and operation service.

The service only accepts active, version-bound qualifications.  It never
touches Legacy databases and never creates broker or real-order messages.
Historical PAPER steps are explicit about their supported data date; an
uncovered current date returns WAITING_DATA instead of replaying stale data as
today.  Current forward generations use a separate bounded ETF profile so old
continuous generations can remain visible without remaining executable.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, time
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from src.quantcore.accounting.storage import AccountingStore
from src.quantcore.activation.status import current_business_date
from src.quantcore.contracts.config import V2Config
from src.quantcore.portfolio.paper import PaperService, RiskMode
from src.quantcore.research.price_executor import (
    ETF3,
    _all_history,
    _date,
    _decimal,
    _next_session_factory,
    _publication,
)
from src.quantcore.strategies import STRATEGY_CATALOG, run_strategy
from src.quantcore.universe.models import Board, SecurityType
from src.quantcore.execution.formal import MarketState


INITIAL_CASH = Decimal("1000000.00")
SHANGHAI = ZoneInfo("Asia/Shanghai")
CURRENT_PAPER_PROFILE = "ETF3_FORWARD_CURRENT"
CURRENT_PRICE_DATASET = "etf3_current_prices"
CURRENT_LIMIT_DATASET = "etf3_current_limits"


def _control(cfg: V2Config) -> Path:
    return cfg.paths.state / "control.sqlite"


def _accounting(cfg: V2Config) -> Path:
    return cfg.paths.state / "accounting.sqlite"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _qualification(cfg: V2Config, strategy_version_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(_control(cfg))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT q.qualification_id,q.strategy_version_id,q.protocol_hash,q.snapshot_hash "
            "FROM paper_qualifications q JOIN qualification_events e "
            "ON e.qualification_id=q.qualification_id "
            "WHERE q.strategy_version_id=? AND e.rowid=(SELECT max(x.rowid) FROM qualification_events x WHERE x.qualification_id=q.qualification_id) "
            "AND e.status='ACTIVE' LIMIT 1",
            (strategy_version_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_dict(row)


def _binding(cfg: V2Config, strategy_version_id: str) -> dict[str, Any] | None:
    conn = sqlite3.connect(_control(cfg))
    conn.row_factory = sqlite3.Row
    try:
        generation = conn.execute(
            "SELECT g.account_id,g.strategy_version_id,g.qualification_id,g.initial_cash,"
            "g.generation_id,g.data_profile,g.start_session,q.snapshot_hash,e.status,e.action,e.reason,e.created_at "
            "FROM paper_generations g JOIN paper_qualifications q ON q.qualification_id=g.qualification_id "
            "JOIN paper_generation_events e ON e.generation_id=g.generation_id "
            "WHERE g.strategy_version_id=? AND e.rowid=(SELECT max(x.rowid) FROM paper_generation_events x WHERE x.generation_id=g.generation_id) "
            "AND e.status IN ('ACTIVE','PAUSED') ORDER BY g.created_at DESC LIMIT 1",
            (strategy_version_id,),
        ).fetchone()
        if generation is not None:
            return _row_dict(generation)
        row = conn.execute(
            "SELECT p.account_id,p.strategy_version_id,p.qualification_id,p.initial_cash,q.snapshot_hash,e.status,e.action,e.reason,e.created_at,"
            "NULL generation_id,NULL data_profile,NULL start_session "
            "FROM paper_accounts p JOIN paper_qualifications q ON q.qualification_id=p.qualification_id "
            "JOIN paper_account_events e ON e.account_id=p.account_id "
            "WHERE p.strategy_version_id=? AND e.rowid=(SELECT max(x.rowid) FROM paper_account_events x WHERE x.account_id=p.account_id) LIMIT 1",
            (strategy_version_id,),
        ).fetchone()
    finally:
        conn.close()
    return _row_dict(row)


def _account_id(strategy_version_id: str) -> str:
    safe = "".join(char if char.isalnum() else "-" for char in strategy_version_id).strip("-")
    return f"paper-v2-{safe}"


def _generation_ids(
    strategy_version_id: str,
    start_session: date,
    *,
    profile: str = "ETF3_CONTINUOUS",
) -> tuple[str, str]:
    safe = "".join(char if char.isalnum() else "-" for char in strategy_version_id).strip("-")
    suffix = start_session.strftime("%Y%m%d")
    if profile == CURRENT_PAPER_PROFILE:
        return (
            f"paper-generation-current-{safe}-{suffix}",
            f"paper-v2-current-{safe}-{suffix}",
        )
    return f"paper-generation-{safe}-{suffix}", f"paper-v2-clean-{safe}-{suffix}"


def _account_exists(cfg: V2Config, account_id: str) -> bool:
    conn = sqlite3.connect(_accounting(cfg))
    try:
        return conn.execute(
            "SELECT 1 FROM accounts WHERE account_id=?", (account_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def _ensure_initial_account(
    cfg: V2Config, account_id: str, initial_cash: Decimal
) -> None:
    # This is an atomic insert-if-absent.  It is deliberately not a normal
    # ledger commit because a retry must never reset an existing cash balance,
    # lots, receivables, NAV, or journal.
    AccountingStore(_accounting(cfg)).ensure_account(account_id, initial_cash)


def create_generation(
    cfg: V2Config,
    strategy_version_id: str,
    start_session: date,
    initial_cash: Decimal = INITIAL_CASH,
) -> dict[str, Any]:
    qualification = _qualification(cfg, strategy_version_id)
    if qualification is None:
        return {
            "status": "BLOCKED_DATA",
            "state": "NO_ACTIVE_QUALIFICATION",
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
        }
    readiness = current_paper_readiness(
        cfg, strategy_version_id, start_session=start_session
    )
    if not readiness.get("ready_for_generation"):
        return {
            "status": "WAITING_DATA",
            "state": readiness.get("state", "PAPER_GENERATION_NOT_READY"),
            "strategy_version_id": strategy_version_id,
            "start_session": start_session.isoformat(),
            "reason": readiness.get("reason"),
            "data_profile": CURRENT_PAPER_PROFILE,
            "latest_supported_session": readiness.get("latest_supported_session"),
            "lookback": readiness.get("lookback"),
            "missing_sessions": readiness.get("missing_sessions", []),
            "execution_allowed": False,
        }
    generation_id, account_id = _generation_ids(
        strategy_version_id, start_session, profile=CURRENT_PAPER_PROFILE
    )
    now = datetime.now(UTC).isoformat()
    existing_event_status: str | None = None
    conn = sqlite3.connect(_control(cfg))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO paper_generations VALUES (?,?,?,?,?,?,?,?)",
            (
                generation_id,
                account_id,
                strategy_version_id,
                qualification["qualification_id"],
                CURRENT_PAPER_PROFILE,
                start_session.isoformat(),
                format(initial_cash, "f"),
                now,
            ),
        )
        existing = conn.execute(
            "SELECT account_id,strategy_version_id,qualification_id,data_profile,start_session,initial_cash "
            "FROM paper_generations WHERE generation_id=?",
            (generation_id,),
        ).fetchone()
        expected = (
            account_id,
            strategy_version_id,
            qualification["qualification_id"],
            CURRENT_PAPER_PROFILE,
            start_session.isoformat(),
            format(initial_cash, "f"),
        )
        if existing is None or tuple(existing) != expected:
            raise RuntimeError("immutable PAPER generation binding conflict")
        event = conn.execute(
            "SELECT status FROM paper_generation_events WHERE generation_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (generation_id,),
        ).fetchone()
        existing_event_status = str(event[0]) if event is not None else None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if existing_event_status == "CLOSED":
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_GENERATION_CLOSED",
            "generation_id": generation_id,
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": "an append-only generation cannot be reopened; choose a new forward start session",
        }
    if existing_event_status in {"ACTIVE", "PAUSED"}:
        if not _account_exists(cfg, account_id):
            # An old/externally interrupted row with an ACTIVE event is not
            # silently repaired into an executable account.  It remains an
            # explicit initialization failure for operator review.
            return {
                "status": "BLOCKED_DATA",
                "state": "PAPER_GENERATION_ACCOUNT_MISSING",
                "generation_id": generation_id,
                "account_id": account_id,
                "strategy_version_id": strategy_version_id,
                "execution_allowed": False,
                "real_orders": False,
                "reason": "ACTIVE generation has no accounting row; no balance reset was attempted",
            }
        return {
            "status": "SUCCEEDED",
            "state": "PAPER_GENERATION_ALREADY_EXISTS",
            "generation_id": generation_id,
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "qualification_id": qualification["qualification_id"],
            "data_profile": CURRENT_PAPER_PROFILE,
            "start_session": start_session.isoformat(),
            "ready_for_first_forward_step": bool(readiness.get("first_forward_data_ready")),
            "idempotent": True,
            "execution_allowed": True,
            "real_orders": False,
        }
    try:
        _ensure_initial_account(cfg, account_id, initial_cash)
    except Exception as exc:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_GENERATION_INITIALIZATION_FAILED",
            "generation_id": generation_id,
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": f"account initialization failed ({type(exc).__name__}); retry is safe and no ACTIVE event was published",
        }
    # Publish executability only after the accounting row exists.  If this
    # second transaction is interrupted, the generation remains invisible to
    # _binding and the next create call can finish it without resetting money.
    conn = sqlite3.connect(_control(cfg))
    try:
        conn.execute("BEGIN IMMEDIATE")
        event_id = hashlib.sha256(f"{generation_id}|ACTIVE|CREATE".encode()).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO paper_generation_events VALUES (?,?,?,?,?,?)",
            (
                event_id,
                generation_id,
                "ACTIVE",
                "CREATE",
                "current-forward PAPER generation; historical continuity is deferred",
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_GENERATION_ACTIVATION_PENDING",
            "generation_id": generation_id,
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": "account exists but ACTIVE event was not published; retry create_generation",
        }
    finally:
        conn.close()
    return {
        "status": "SUCCEEDED",
        "state": "PAPER_GENERATION_CREATED",
        "generation_id": generation_id,
        "account_id": account_id,
        "strategy_version_id": strategy_version_id,
        "qualification_id": qualification["qualification_id"],
        "data_profile": CURRENT_PAPER_PROFILE,
        "start_session": start_session.isoformat(),
        "ready_for_first_forward_step": bool(
            readiness.get("first_forward_data_ready")
        ),
        "initialization": "READY",
        "idempotent": False,
        "execution_allowed": True,
        "real_orders": False,
    }


def admit(cfg: V2Config, strategy_version_id: str, initial_cash: Decimal = INITIAL_CASH) -> dict[str, Any]:
    qualification = _qualification(cfg, strategy_version_id)
    if qualification is None:
        return {
            "status": "BLOCKED_DATA",
            "state": "NO_ACTIVE_QUALIFICATION",
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "reason": "Admit requires active V2 Stage09 CANDIDATE + Stage10 PASS + qualification evidence.",
        }
    account_id = _account_id(strategy_version_id)
    now = datetime.now(UTC).isoformat()
    existing_event_status: str | None = None
    conn = sqlite3.connect(_control(cfg))
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO paper_accounts VALUES (?,?,?,?,?)",
            (account_id, strategy_version_id, qualification["qualification_id"], format(initial_cash, "f"), now),
        )
        existing = conn.execute(
            "SELECT strategy_version_id,qualification_id,initial_cash FROM paper_accounts WHERE account_id=?",
            (account_id,),
        ).fetchone()
        if existing is None or tuple(existing) != (strategy_version_id, qualification["qualification_id"], format(initial_cash, "f")):
            raise RuntimeError("immutable PAPER account binding conflict")
        event = conn.execute(
            "SELECT status FROM paper_account_events WHERE account_id=? "
            "ORDER BY rowid DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        existing_event_status = str(event[0]) if event is not None else None
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if existing_event_status == "CLOSED":
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_ACCOUNT_CLOSED",
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": "an append-only PAPER account cannot be reopened",
        }
    if existing_event_status in {"ACTIVE", "PAUSED"}:
        if not _account_exists(cfg, account_id):
            return {
                "status": "BLOCKED_DATA",
                "state": "PAPER_ACCOUNT_LEDGER_MISSING",
                "account_id": account_id,
                "strategy_version_id": strategy_version_id,
                "execution_allowed": False,
                "real_orders": False,
                "reason": "ACTIVE account has no accounting row; no balance reset was attempted",
            }
        return {
            "status": "SUCCEEDED",
            "state": "PAPER_ALREADY_ADMITTED",
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "qualification_id": qualification["qualification_id"],
            "snapshot_hash": qualification["snapshot_hash"],
            "idempotent": True,
            "execution_allowed": True,
            "real_orders": False,
        }
    try:
        _ensure_initial_account(cfg, account_id, initial_cash)
    except Exception as exc:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_ACCOUNT_INITIALIZATION_FAILED",
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": f"account initialization failed ({type(exc).__name__}); retry is safe and no ACTIVE event was published",
        }
    conn = sqlite3.connect(_control(cfg))
    try:
        conn.execute("BEGIN IMMEDIATE")
        event_id = hashlib.sha256(f"{account_id}|ACTIVE|ADMIT|{qualification['qualification_id']}".encode()).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO paper_account_events VALUES (?,?,?,?,?,?)",
            (event_id, account_id, "ACTIVE", "ADMIT", "qualification evidence verified", datetime.now(UTC).isoformat()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_ACCOUNT_ACTIVATION_PENDING",
            "account_id": account_id,
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": "account exists but ACTIVE event was not published; retry admit",
        }
    finally:
        conn.close()
    return {
        "status": "SUCCEEDED",
        "state": "PAPER_ADMITTED",
        "account_id": account_id,
        "strategy_version_id": strategy_version_id,
        "qualification_id": qualification["qualification_id"],
        "snapshot_hash": qualification["snapshot_hash"],
        "initialization": "READY",
        "idempotent": False,
        "execution_allowed": True,
        "real_orders": False,
    }


def change_state(cfg: V2Config, strategy_version_id: str, action: str) -> dict[str, Any]:
    binding = _binding(cfg, strategy_version_id)
    if binding is None:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_ACCOUNT_NOT_FOUND",
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "reason": "Admit the active V2 qualification before changing PAPER state.",
        }
    if binding.get("generation_id") and binding.get("data_profile") != CURRENT_PAPER_PROFILE:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_HISTORICAL_GENERATION_FROZEN",
            "account_id": binding.get("account_id"),
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": "historical ETF3_CONTINUOUS generations are frozen while historical continuity is deferred",
        }
    status = "PAUSED" if action == "pause" else "ACTIVE" if action == "resume" else None
    if status is None:
        raise ValueError(f"unsupported PAPER state action: {action}")
    now = datetime.now(UTC).isoformat()
    event_id = hashlib.sha256(f"{binding['account_id']}|{status}|{action}".encode()).hexdigest()
    conn = sqlite3.connect(_control(cfg))
    try:
        if binding.get("generation_id"):
            conn.execute(
                "INSERT OR IGNORE INTO paper_generation_events VALUES (?,?,?,?,?,?)",
                (
                    event_id,
                    binding["generation_id"],
                    status,
                    action.upper(),
                    f"user requested {action}",
                    now,
                ),
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO paper_account_events VALUES (?,?,?,?,?,?)",
                (event_id, binding["account_id"], status, action.upper(), f"user requested {action}", now),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "status": "SUCCEEDED",
        "state": f"PAPER_{status}",
        "account_id": binding["account_id"],
        "strategy_version_id": strategy_version_id,
        "execution_allowed": True,
        "risk_mode": "PAUSE_NEW_RISK" if status == "PAUSED" else "NORMAL",
    }


def _strategy_key(strategy_version_id: str) -> str:
    return {
        "etf_rotation.v2": "etf_rotation.v1",
        "etf_lowvol.v1": "etf_lowvol.v1",
        "etf_trend_defensive.v1": "etf_trend_defensive.v1",
    }.get(strategy_version_id, "")


def _context(cfg: V2Config, price_dataset: str, limit_dataset: str):
    price_manifest, price_rows = _publication(cfg, price_dataset)
    limit_manifest, limit_rows = _publication(cfg, limit_dataset)
    price_map = {
        (str(row["ts_code"]), _date(row["trade_date"])): row
        for row in price_rows
        if str(row.get("ts_code")) in ETF3
    }
    limit_map = {
        (str(row["ts_code"]), _date(row["trade_date"])): row
        for row in limit_rows
        if str(row.get("ts_code")) in ETF3
    }
    sessions = sorted(
        session
        for session in {key[1] for key in price_map}
        if all(
            (asset, session) in price_map and (asset, session) in limit_map
            for asset in ETF3
        )
    )
    return price_manifest, limit_manifest, price_rows, price_map, limit_map, sessions


def _contexts(cfg: V2Config) -> list[tuple[str, tuple[Any, ...]]]:
    """Load every published bounded ETF3 bundle that is available.

    The research bundle covers the long historical window and the known-only
    bundle covers the later verified sessions.  PAPER chooses by requested
    date, so a date in one bundle cannot silently be filled from another.
    """

    result: list[tuple[str, tuple[Any, ...]]] = []
    for label, prices, limits in (
        (CURRENT_PAPER_PROFILE, CURRENT_PRICE_DATASET, CURRENT_LIMIT_DATASET),
        ("ETF3_CONTINUOUS", "etf3_continuous_prices", "etf3_continuous_limits"),
        ("RESEARCH_ETF3", "research_etf3_prices", "research_etf3_limits"),
        ("KNOWN_ETF3", "known_etf3_prices", "known_etf3_limits"),
    ):
        try:
            result.append((label, _context(cfg, prices, limits)))
        except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError):
            continue
    return result


def _load_context(
    cfg: V2Config, on: date | None = None, *, profile: str | None = None
):
    contexts = [
        item for item in _contexts(cfg) if profile is None or item[0] == profile
    ]
    if not contexts:
        raise RuntimeError("no REAL_READY ETF3 PAPER data bundle is published")
    if on is not None:
        for _, context in contexts:
            if on in context[-1]:
                return context
    # For an uncovered date the newest bundle supplies the honest
    # latest_supported_date in the WAITING_DATA response.
    return max(contexts, key=lambda item: item[1][-1] if item[1][-1] else date.min)[1]


def supported_sessions(cfg: V2Config, *, profile: str | None = None) -> list[date]:
    sessions: set[date] = set()
    for label, context in _contexts(cfg):
        if profile is not None and label != profile:
            continue
        sessions.update(context[-1])
    return sorted(sessions)


def latest_supported_session(cfg: V2Config, *, profile: str | None = None) -> date | None:
    sessions = supported_sessions(cfg, profile=profile)
    return sessions[-1] if sessions else None


def _calendar_sessions(cfg: V2Config) -> list[date]:
    try:
        _, rows = _publication(cfg, "trade_cal")
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError):
        return []
    return sorted(
        {
            _date(str(row["cal_date"]))
            for row in rows
            if str(row.get("cal_date", "")).replace("-", "").isdigit()
            and int(row.get("is_open") or 0) == 1
        }
    )


def current_paper_readiness(
    cfg: V2Config,
    strategy_version_id: str,
    *,
    start_session: date | None = None,
) -> dict[str, Any]:
    """Assess whether a new forward PAPER generation can be created.

    The assessment is intentionally stricter than ``supported_sessions``: a
    strategy needs its actual causal lookback calendar sessions, not merely a
    count of rows and not just one available prior day.  The first forward
    session may be absent while the generation is prepared; no fill is allowed
    until that session is published.
    """

    strategy_key = _strategy_key(strategy_version_id)
    definition = STRATEGY_CATALOG.get(strategy_key)
    if definition is None:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_STRATEGY_EXECUTOR_UNAVAILABLE",
            "strategy_version_id": strategy_version_id,
            "ready_for_generation": False,
            "reason": "strategy version has no supported ETF PAPER executor",
        }
    try:
        context = _load_context(cfg, profile=CURRENT_PAPER_PROFILE)
    except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError) as exc:
        return {
            "status": "WAITING_DATA",
            "state": "PAPER_CURRENT_SCOPE_UNPUBLISHED",
            "strategy_version_id": strategy_version_id,
            "data_profile": CURRENT_PAPER_PROFILE,
            "ready_for_generation": False,
            "reason": str(exc),
            "lookback": definition.lookback,
            "missing_sessions": [],
        }
    price_manifest, _, _price_rows, _prices, _limits, complete_sessions = context
    calendar = _calendar_sessions(cfg)
    latest = complete_sessions[-1] if complete_sessions else None
    if latest is None:
        return {
            "status": "WAITING_DATA",
            "state": "PAPER_CURRENT_SCOPE_EMPTY",
            "strategy_version_id": strategy_version_id,
            "data_profile": CURRENT_PAPER_PROFILE,
            "ready_for_generation": False,
            "reason": "current ETF price+limit publication has no complete session",
            "lookback": definition.lookback,
            "missing_sessions": [],
        }
    requested_start = start_session
    current_date = current_business_date(cfg)
    if requested_start is None:
        anchor = max(latest, current_date) if current_date else latest
        requested_start = next((item for item in calendar if item > anchor), None)
    if requested_start is None:
        return {
            "status": "WAITING_DATA",
            "state": "PAPER_CURRENT_FORWARD_SESSION_UNRESOLVED",
            "strategy_version_id": strategy_version_id,
            "data_profile": CURRENT_PAPER_PROFILE,
            "latest_supported_session": latest.isoformat(),
            "current_business_date": current_date.isoformat() if current_date else None,
            "first_forward_data_ready": False,
            "ready_for_generation": False,
            "lookback_ready": False,
            "lookback": {"required_observations": definition.lookback + 1},
            "missing_sessions": [],
            "reason": "no future exchange-calendar session is published after the current business date; historical sessions cannot be replayed as forward PAPER",
        }
    prior_calendar = [item for item in calendar if item < requested_start]
    prior = prior_calendar[-1] if prior_calendar else None
    required_window = prior_calendar[-(definition.lookback + 1) :]
    complete_set = set(complete_sessions)
    missing = [item.isoformat() for item in required_window if item not in complete_set]
    available_count = sum(item in complete_set for item in required_window)
    first_forward_ready = requested_start in complete_set
    start_is_forward = requested_start > latest and (
        current_date is None or requested_start > current_date
    )
    price_snapshot = str(price_manifest.get("manifest_hash") or "")
    lookback = {
        "required_observations": definition.lookback + 1,
        "calendar_window_start": required_window[0].isoformat()
        if required_window
        else None,
        "calendar_window_end": required_window[-1].isoformat()
        if required_window
        else None,
        "available_complete_sessions": available_count,
        "missing_sessions": missing,
        "causal_prior_session": prior.isoformat() if prior else None,
        "strategy_key": strategy_key,
        "price_snapshot_hash": price_snapshot,
    }
    ready = bool(
        start_is_forward
        and prior is not None
        and prior in complete_set
        and not missing
    )
    if requested_start <= latest:
        state = "PAPER_START_MUST_BE_FORWARD"
        reason = "new PAPER generation must start after the latest complete current ETF session"
    elif current_date is not None and requested_start <= current_date:
        state = "PAPER_START_NOT_CURRENT_FORWARD"
        reason = "the requested session has already elapsed at startup; elapsed sessions are historical replay only and cannot initialize current forward PAPER"
    elif missing:
        state = "PAPER_LOOKBACK_INCOMPLETE"
        reason = "causal ETF lookback has missing calendar sessions; current PAPER is not admitted"
    elif not first_forward_ready:
        state = "PAPER_WAITING_FIRST_FORWARD_SESSION"
        reason = "generation may be prepared, but the first forward session has not been published"
    else:
        state = "PAPER_CURRENT_SCOPE_READY"
        reason = "current ETF scope and causal lookback are ready"
    return {
        "status": "PASS" if ready else "WAITING_DATA",
        "state": state,
        "strategy_version_id": strategy_version_id,
        "data_profile": CURRENT_PAPER_PROFILE,
        "requested_start_session": requested_start.isoformat(),
        "latest_supported_session": latest.isoformat(),
        "next_expected_session": requested_start.isoformat(),
        "first_forward_data_ready": first_forward_ready,
        "ready_for_generation": ready,
        "lookback_ready": bool(prior and not missing and prior in complete_set),
        "lookback": lookback,
        "missing_sessions": missing,
        "reason": reason,
    }


def _tradability_state(
    cfg: V2Config, asset_id: str, on: date
) -> dict[str, Any]:
    """Resolve execution tradability from PIT state plus explicit vendor evidence.

    A price bar and a limit row are execution inputs, not suspension evidence.
    ETF security-master rows currently carry ``suspended=NULL``; in that case a
    successful exact-date ``suspend_d`` negative-list probe is required.  An
    absent or failed probe remains UNKNOWN and is passed to FormalEngine as
    non-tradable, so it can block only the affected asset.
    """

    control = sqlite3.connect(_control(cfg))
    control.row_factory = sqlite3.Row
    try:
        row = control.execute(
            "SELECT state_id,list_date,delist_date,risk_status,suspended,source_ref,confidence "
            "FROM security_state_versions WHERE asset_id=? AND effective_from<=? "
            "AND (effective_to IS NULL OR effective_to>?) ORDER BY known_from DESC LIMIT 1",
            (asset_id, on.isoformat(), on.isoformat()),
        ).fetchone()
    finally:
        control.close()
    if row is None:
        return {
            "tradable": False,
            "status": "UNKNOWN",
            "reason": "PAPER_TRADABILITY_STATE_MISSING",
            "source_ref": "",
        }
    source_ref = str(row["source_ref"] or "")
    if str(row["confidence"] or "") != "VERIFIED":
        return {
            "tradable": False,
            "status": "UNKNOWN",
            "reason": "PAPER_TRADABILITY_STATE_NOT_VERIFIED",
            "source_ref": source_ref,
        }
    list_date = str(row["list_date"] or "")
    delist_date = str(row["delist_date"] or "")
    if not list_date or on.isoformat() < list_date:
        return {
            "tradable": False,
            "status": "NOT_LISTED",
            "reason": "PAPER_ASSET_NOT_LISTED_ON_SESSION",
            "source_ref": source_ref,
        }
    if delist_date and on.isoformat() > delist_date:
        return {
            "tradable": False,
            "status": "DELISTED",
            "reason": "PAPER_ASSET_DELISTED_ON_SESSION",
            "source_ref": source_ref,
        }
    if str(row["risk_status"] or "") != "NORMAL":
        return {
            "tradable": False,
            "status": "RISK_BLOCKED",
            "reason": "PAPER_ASSET_RISK_STATUS_NOT_NORMAL",
            "source_ref": source_ref,
        }
    if row["suspended"] == 1:
        return {
            "tradable": False,
            "status": "SUSPENDED",
            "reason": "PAPER_ASSET_SUSPENDED_BY_PIT_STATE",
            "source_ref": source_ref,
        }
    if row["suspended"] == 0:
        return {
            "tradable": True,
            "status": "TRADABLE",
            "reason": "PIT_STATE_VERIFIED_NOT_SUSPENDED",
            "source_ref": source_ref,
        }

    evidence_path = cfg.paths.artifacts / "evidence" / "FINAL_DAILY_USE" / "tradability_probe.json"
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        evidence = {}
    for item in evidence.get("requests", []) if isinstance(evidence, dict) else []:
        if (
            str(item.get("provider")) == "promax"
            and str(item.get("api")) == "suspend_d"
            and str(item.get("ts_code")) == asset_id
            and str(item.get("trade_date")) == on.strftime("%Y%m%d")
        ):
            if item.get("status") == "SUCCESS" and item.get("suspended_reported") is True:
                return {
                    "tradable": False,
                    "status": "SUSPENDED",
                    "reason": "PAPER_ASSET_SUSPENDED_BY_VENDOR_NEGATIVE_LIST",
                    "source_ref": f"vendor:suspend_d:{evidence_path.name}",
                }
            if (
                item.get("status") == "SUCCESS"
                and int(item.get("row_count") or 0) == 0
                and item.get("pagination_complete") is True
            ):
                return {
                    "tradable": True,
                    "status": "TRADABLE",
                    "reason": "VENDOR_SUSPEND_D_NEGATIVE_LIST_VERIFIED",
                    "source_ref": f"vendor:suspend_d:{evidence_path.name}",
                }
    return {
        "tradable": False,
        "status": "UNKNOWN",
        "reason": "PAPER_TRADABILITY_STATE_UNKNOWN",
        "source_ref": source_ref,
    }


def _step_one(cfg: V2Config, binding: dict[str, Any], on: date) -> dict[str, Any]:
    strategy_key = _strategy_key(str(binding["strategy_version_id"]))
    if not strategy_key:
        return {"status": "BLOCKED_DATA", "reason": "strategy version has no PAPER executor"}
    profile = str(binding.get("data_profile") or "") or None
    price_manifest, limit_manifest, price_rows, prices, limits, sessions = _load_context(
        cfg, on, profile=profile
    )
    start_session = binding.get("start_session")
    if start_session and on < _date(str(start_session)):
        return {
            "status": "WAITING_DATA",
            "trade_date": on.isoformat(),
            "state": "PAPER_BEFORE_GENERATION_START",
            "start_session": str(start_session),
        }
    if on not in sessions:
        return {
            "status": "WAITING_DATA",
            "trade_date": on.isoformat(),
            "latest_supported_date": sessions[-1].isoformat() if sessions else None,
            "reason": "The requested business date has no complete REAL price+limit publication; stale history is not replayed as today.",
        }
    accounting = AccountingStore(_accounting(cfg))
    last_nav_date: date | None = None
    conn = sqlite3.connect(_accounting(cfg))
    try:
        row = conn.execute(
            "SELECT max(nav_on) FROM daily_nav WHERE account_id=?",
            (binding["account_id"],),
        ).fetchone()
        if row and row[0]:
            last_nav_date = date.fromisoformat(str(row[0]))
    finally:
        conn.close()
    if last_nav_date is not None:
        if on < last_nav_date:
            return {
                "status": "WAITING_DATA",
                "trade_date": on.isoformat(),
                "last_trade_date": last_nav_date.isoformat(),
                "state": "PAPER_TIME_TRAVEL_BLOCKED",
                "reason": "PAPER cannot step backwards from its last persisted NAV date.",
            }
        if on != last_nav_date:
            if last_nav_date not in sessions:
                return {
                    "status": "WAITING_DATA",
                    "trade_date": on.isoformat(),
                    "last_trade_date": last_nav_date.isoformat(),
                    "state": "PAPER_DATASET_CONTINUITY_GAP",
                    "reason": "The last NAV belongs to a different published data bundle; step_span cannot bridge an uncovered date gap.",
                }
            next_index = sessions.index(last_nav_date) + 1
            expected = sessions[next_index] if next_index < len(sessions) else None
            if expected != on:
                return {
                    "status": "WAITING_DATA",
                    "trade_date": on.isoformat(),
                    "last_trade_date": last_nav_date.isoformat(),
                    "expected_next_date": expected.isoformat() if expected else None,
                    "state": "PAPER_CONTINUITY_REQUIRED",
                    "reason": "Run each supported session in order; missing sessions are never silently skipped.",
                }
    index = sessions.index(on)
    if index == 0:
        return {"status": "WAITING_DATA", "trade_date": on.isoformat(), "reason": "No prior session for a causal signal."}
    prior = sessions[index - 1]
    history = _all_history(price_rows)
    signal = run_strategy(
        strategy_key,
        history,
        datetime.combine(prior, time(17, 1), SHANGHAI),
        top_n=1,
    )
    state = accounting.load(str(binding["account_id"]))
    prior_closes = {asset: _decimal(prices[(asset, prior)], "close") for asset in ETF3}
    nav = state.nav(prior_closes)
    target_shares = {
        asset: int((nav * weight * Decimal("0.98") / prior_closes[asset] / Decimal(100)).to_integral_value(rounding=ROUND_FLOOR)) * 100
        for asset, weight in signal.targets.items()
    }
    boards = {asset: Board.SSE_MAIN if asset.endswith(".SH") else Board.SZSE_MAIN for asset in ETF3}
    types = {asset: SecurityType.ETF_DOMESTIC_EQUITY for asset in ETF3}
    tradability = {asset: _tradability_state(cfg, asset, on) for asset in ETF3}
    market_states = {
        asset: MarketState(
            bool(tradability[asset]["tradable"]),
            _decimal(limits[(asset, on)], "up_limit"),
            _decimal(limits[(asset, on)], "down_limit"),
            str(tradability[asset]["source_ref"] or limit_manifest["manifest_hash"]),
        )
        for asset in ETF3
    }
    paper = PaperService(_control(cfg), _accounting(cfg), _next_session_factory(sessions))
    result = paper.execute_targets(
        account_id=str(binding["account_id"]),
        qualification_id=str(binding["qualification_id"]),
        snapshot_hash=str(binding["snapshot_hash"]),
        on=on,
        targets=target_shares,
        boards=boards,
        security_types=types,
        prices={asset: _decimal(prices[(asset, on)], "open") for asset in ETF3},
        market_states=market_states,
        risk_mode=RiskMode.PAUSE_NEW_RISK if binding["status"] == "PAUSED" else RiskMode.NORMAL,
        initial_cash=Decimal(str(binding["initial_cash"])),
    )
    after = accounting.load(str(binding["account_id"]))
    mark_prices = {asset: _decimal(prices[(asset, on)], "close") for asset in ETF3}
    run_id = f"paper-{binding['account_id']}-{on.isoformat()}"
    accounting.record_nav(
        str(binding["account_id"]),
        on,
        after.nav(mark_prices),
        str(price_manifest["manifest_hash"]),
        run_id,
    )
    return {
        "status": "SUCCEEDED",
        "trade_date": on.isoformat(),
        "account_id": binding["account_id"],
        "strategy_version_id": binding["strategy_version_id"],
        "risk_mode": "PAUSE_NEW_RISK" if binding["status"] == "PAUSED" else "NORMAL",
        "signal": {"scores": {key: format(value, "f") for key, value in signal.scores.items()}, "targets": target_shares},
        "orders": [
            {"order_id": item.order_id, "asset_id": item.asset_id, "side": item.side, "shares": item.shares, "filled_shares": item.filled_shares, "status": item.status.value, "blocked_reason": item.blocked_reason}
            for item in result.orders
        ],
        "fills": [
            {"fill_id": item.fill_id, "order_id": item.order_id, "asset_id": item.asset_id, "side": item.side, "shares": item.shares, "price": format(item.price, "f"), "fee": format(item.fee, "f")}
            for item in result.new_fills
        ],
        "tradability": tradability,
        "nav": format(after.nav(mark_prices), "f"),
        "price_snapshot_hash": price_manifest["manifest_hash"],
        "real_orders": False,
    }


def _record_operation(cfg: V2Config, account_id: str, action: str, request_hash: str, trade_date: str | None, result: dict[str, Any]) -> dict[str, Any]:
    operation_id = hashlib.sha256(f"{account_id}|{action}|{request_hash}".encode()).hexdigest()
    stored_result = {**result, "operation_id": operation_id}
    conn = sqlite3.connect(_control(cfg))
    try:
        conn.execute("BEGIN IMMEDIATE")
        generation = conn.execute(
            "SELECT generation_id FROM paper_generations WHERE account_id=?",
            (account_id,),
        ).fetchone()
        table = (
            "paper_generation_operation_receipts"
            if generation is not None
            else "paper_operation_receipts"
        )
        existing = conn.execute(
            f"SELECT result_json FROM {table} WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            conn.rollback()
            return json.loads(str(existing[0]))
        if generation is not None:
            conn.execute(
                "INSERT INTO paper_generation_operation_receipts VALUES (?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    generation[0],
                    account_id,
                    action,
                    request_hash,
                    trade_date,
                    _json(stored_result),
                    datetime.now(UTC).isoformat(),
                ),
            )
        else:
            conn.execute(
                "INSERT INTO paper_operation_receipts VALUES (?,?,?,?,?,?,?)",
                (operation_id, account_id, action, request_hash, trade_date, _json(stored_result), datetime.now(UTC).isoformat()),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return stored_result


def operate(
    cfg: V2Config,
    strategy_version_id: str,
    action: str,
    *,
    trade_date: str = "",
    start_date: str = "",
    end_date: str = "",
    request_hash: str,
) -> dict[str, Any]:
    if action == "create_generation":
        selected_start = start_date or trade_date
        if not selected_start:
            readiness = current_paper_readiness(cfg, strategy_version_id)
            return {
                "status": readiness.get("status", "WAITING_DATA"),
                "state": readiness.get(
                    "state", "PAPER_CURRENT_FORWARD_SESSION_UNRESOLVED"
                ),
                "strategy_version_id": strategy_version_id,
                "data_profile": CURRENT_PAPER_PROFILE,
                "latest_supported_session": readiness.get(
                    "latest_supported_session"
                ),
                "reason": readiness.get(
                    "reason", "current forward PAPER session is not yet resolvable"
                ),
                "missing_sessions": readiness.get("missing_sessions", []),
                "execution_allowed": False,
                "real_orders": False,
            }
        return create_generation(cfg, strategy_version_id, _date(selected_start))
    binding = _binding(cfg, strategy_version_id)
    if action == "admit":
        return admit(cfg, strategy_version_id)
    if action in {"pause", "resume"}:
        return change_state(cfg, strategy_version_id, action)
    if binding is None:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_ACCOUNT_NOT_FOUND",
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "reason": "No admitted V2 PAPER account is available.",
        }
    # A pre-generation PAPER account is a historical/audit binding.  It may be
    # displayed, but it is not an executable clean generation and must never be
    # allowed to create fills through the step/reconcile paths.
    if not binding.get("generation_id") or binding.get("data_profile") != CURRENT_PAPER_PROFILE:
        return {
            "status": "BLOCKED_DATA",
            "state": "PAPER_ACCOUNT_AUDIT_ONLY",
            "account_id": binding.get("account_id"),
            "strategy_version_id": strategy_version_id,
            "execution_allowed": False,
            "real_orders": False,
            "reason": "Only an ACTIVE ETF3_FORWARD_CURRENT generation may execute current PAPER operations.",
        }
    if action == "reconcile":
        accounts = AccountingStore(_accounting(cfg))
        state = accounts.load(str(binding["account_id"]))
        orders = accounts.load_orders(str(binding["account_id"]))
        fills = accounts.load_fills(str(binding["account_id"]))
        result = {
            "status": "SUCCEEDED",
            "state": "PAPER_RECONCILED",
            "account_id": binding["account_id"],
            "journal_entries": len(state.journal),
            "orders": len(orders),
            "fills": len(fills),
            "journal_balanced": all(sum(x.debit for x in entry.lines) == sum(x.credit for x in entry.lines) for entry in state.journal),
            "cash": format(state.cash, "f"),
            "execution_allowed": True,
        }
        return _record_operation(cfg, str(binding["account_id"]), action.upper(), request_hash, trade_date or None, result)
    if action == "step":
        if not trade_date:
            return {"status": "WAITING_DATA", "state": "PAPER_DATE_REQUIRED", "reason": "Provide an explicit supported business date; current date is not silently substituted with history."}
        result = _step_one(cfg, binding, _date(trade_date))
        return _record_operation(cfg, str(binding["account_id"]), "STEP", request_hash, trade_date, result)
    if action == "step_span":
        if not start_date or not end_date:
            return {"status": "WAITING_DATA", "state": "PAPER_RANGE_REQUIRED", "reason": "step_span requires start_date and end_date."}
        profile = str(binding.get("data_profile") or "") or None
        sessions = supported_sessions(cfg, profile=profile)
        left, right = _date(start_date), _date(end_date)
        selected = [x for x in sessions if left <= x <= right]
        if len(selected) > 250:
            return {"status": "BLOCKED_DATA", "state": "PAPER_RANGE_TOO_LARGE", "reason": "step_span is bounded to 250 supported sessions per request."}
        if not selected:
            result = {
                "status": "WAITING_DATA",
                "state": "PAPER_FORWARD_DATA_NOT_PUBLISHED",
                "account_id": binding["account_id"],
                "start_date": start_date,
                "end_date": end_date,
                "latest_supported_date": sessions[-1].isoformat() if sessions else None,
                "reason": "no complete current ETF session exists in the requested range; no historical replay was run",
                "execution_allowed": True,
            }
            return _record_operation(
                cfg,
                str(binding["account_id"]),
                "STEP_SPAN",
                request_hash,
                start_date,
                result,
            )
        results = [_step_one(cfg, binding, on) for on in selected]
        result = {"status": "SUCCEEDED" if results and all(x.get("status") == "SUCCEEDED" for x in results) else "PARTIAL", "state": "PAPER_STEP_SPAN", "account_id": binding["account_id"], "start_date": start_date, "end_date": end_date, "days": results, "execution_allowed": True}
        return _record_operation(cfg, str(binding["account_id"]), "STEP_SPAN", request_hash, start_date, result)
    raise ValueError(f"unsupported PAPER action: {action}")


def v2_accounts(cfg: V2Config) -> list[dict[str, Any]]:
    conn = sqlite3.connect(_control(cfg))
    conn.row_factory = sqlite3.Row
    try:
        bindings = conn.execute(
            "SELECT p.account_id,p.strategy_version_id,p.qualification_id,p.initial_cash,p.created_at,e.status,e.reason,"
            "NULL generation_id,'AUDIT_ONLY' data_profile,NULL start_session "
            "FROM paper_accounts p JOIN paper_account_events e ON e.account_id=p.account_id "
            "WHERE e.rowid=(SELECT max(x.rowid) FROM paper_account_events x WHERE x.account_id=p.account_id) "
            "UNION ALL "
            "SELECT g.account_id,g.strategy_version_id,g.qualification_id,g.initial_cash,g.created_at,e.status,e.reason,"
            "g.generation_id,g.data_profile,g.start_session "
            "FROM paper_generations g JOIN paper_generation_events e ON e.generation_id=g.generation_id "
            "WHERE e.rowid=(SELECT max(x.rowid) FROM paper_generation_events x WHERE x.generation_id=g.generation_id) "
            "ORDER BY generation_id DESC,account_id"
        ).fetchall()
    finally:
        conn.close()
    accounting = sqlite3.connect(f"file:{_accounting(cfg).as_posix()}?mode=ro&immutable=1", uri=True)
    accounting.row_factory = sqlite3.Row
    try:
        output: list[dict[str, Any]] = []
        for binding in bindings:
            account = accounting.execute("SELECT cash,updated_at FROM accounts WHERE account_id=?", (binding["account_id"],)).fetchone()
            if account is None:
                continue
            lot_rows = [
                dict(row)
                for row in accounting.execute(
                    "SELECT asset_id,shares,unit_cost,available_on FROM lots "
                    "WHERE account_id=? ORDER BY asset_id,lot_id",
                    (binding["account_id"],),
                )
            ]
            nav = [
                dict(row)
                for row in accounting.execute(
                    "SELECT nav_on trade_date,nav,price_snapshot_hash,run_id "
                    "FROM daily_nav WHERE account_id=? ORDER BY nav_on,run_id",
                    (binding["account_id"],),
                )
            ]
            last_nav = nav[-1] if nav else None
            nav_date = date.fromisoformat(str(last_nav["trade_date"])) if last_nav else None
            price_map: dict[tuple[str, date], dict[str, Any]] = {}
            try:
                context = _load_context(cfg, nav_date)
                price_map = context[3]
            except (OSError, RuntimeError, sqlite3.DatabaseError, ValueError):
                pass
            grouped: dict[str, list[dict[str, Any]]] = {}
            for row in lot_rows:
                grouped.setdefault(str(row["asset_id"]), []).append(row)
            positions: list[dict[str, Any]] = []
            for asset_id, lots in grouped.items():
                shares = sum(int(row["shares"]) for row in lots)
                unit_cost_total = sum(
                    Decimal(str(row["unit_cost"])) * int(row["shares"])
                    for row in lots
                )
                available_shares = sum(
                    int(row["shares"])
                    for row in lots
                    if nav_date is not None
                    and date.fromisoformat(str(row["available_on"])) <= nav_date
                )
                mark = (
                    price_map.get((asset_id, nav_date), {}).get("close")
                    if nav_date is not None
                    else None
                )
                positions.append(
                    {
                        "asset_id": asset_id,
                        "ts_code": asset_id,
                        "name": asset_id,
                        "shares": shares,
                        "avg_cost": float(unit_cost_total / Decimal(shares)),
                        "last_price": float(mark) if mark is not None else None,
                        "available_shares": available_shares,
                        "available_on": min(str(row["available_on"]) for row in lots),
                    }
                )
            initial_cash = float(binding["initial_cash"])
            nav_value = float(last_nav["nav"]) if last_nav else float(account["cash"])
            output.append({
                "account_id": binding["account_id"],
                "strategy_id": binding["strategy_version_id"],
                "strategy_version_id": binding["strategy_version_id"],
                "qualification_id": binding["qualification_id"],
                "generation_id": binding["generation_id"],
                "data_profile": binding["data_profile"],
                "generation_start_session": binding["start_session"],
                "continuity_status": (
                    "AUDIT_ONLY_NON_CONTINUOUS"
                    if not binding["generation_id"]
                    else "HISTORICAL_CONTINUITY_DEFERRED"
                    if binding["data_profile"] != CURRENT_PAPER_PROFILE
                    else "PASS_TO_LATEST_SUPPORTED"
                    if last_nav is not None
                    and str(last_nav["trade_date"])
                    == (
                        latest_supported_session(cfg, profile=str(binding["data_profile"]))
                        or date.min
                    ).isoformat()
                    else "READY_TO_RUN"
                    if last_nav is None
                    else "IN_PROGRESS"
                ),
                "cash": float(account["cash"]),
                "positions": positions,
                "nav_series": [
                    {
                        **item,
                        "nav": float(item["nav"]),
                    }
                    for item in nav
                ],
                "nav": nav_value,
                "total_return": nav_value / initial_cash - 1 if initial_cash else None,
                "status": binding["status"],
                "reason": binding["reason"],
                "initial_cash": initial_cash,
                "as_of": account["updated_at"],
                "updated_at": account["updated_at"],
                "last_trade_date": str(last_nav["trade_date"]) if last_nav else None,
                "reconciliation_status": "PUBLISHED_PAPER_LEDGER",
                "execution_allowed": bool(
                    binding["generation_id"]
                    and binding["data_profile"] == CURRENT_PAPER_PROFILE
                    and binding["status"] in {"ACTIVE", "PAUSED"}
                ),
                "real_orders": False,
            })
        return output
    finally:
        accounting.close()


__all__ = [
    "CURRENT_LIMIT_DATASET",
    "CURRENT_PAPER_PROFILE",
    "CURRENT_PRICE_DATASET",
    "admit",
    "create_generation",
    "current_paper_readiness",
    "latest_supported_session",
    "operate",
    "supported_sessions",
    "v2_accounts",
]
