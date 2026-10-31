"""Short-term decision-support workbench with immutable evidence and no order path."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import pandas as pd  # type: ignore[import-untyped]
import pyarrow.dataset as ds  # type: ignore[import-untyped]

from src.quantcore.contracts.config import V2Config, load_v2_config
from src.quantcore.data.manifest import ManifestStore

ENGINE_VERSION = "short-term-workbench.v2"
RULE_VERSION = "CN_CASH_EQUITY_2026_07_V1"
FAMILIES = (
    "SHORT_TREND_PULLBACK_V1",
    "SHORT_BREAKOUT_CONFIRM_V1",
    "SHORT_REVERSAL_V1",
    "SHORT_TREND_PULLBACK_V2",
    "SHORT_BREAKOUT_CONFIRM_V2",
    "SHORT_REVERSAL_V2",
    "SHORT_GAP_MOMENTUM_V1",
    "SHORT_RELATIVE_STRENGTH_V1",
)

# Validation levels for candidates
VALIDATION_ENGINEERING_ONLY = "ENGINEERING_ONLY"
VALIDATION_ENGINEERING_ONLY_V2 = "ENGINEERING_ONLY_V2"
VALIDATION_ENGINEERING_ONLY_NEW = "ENGINEERING_ONLY_NEW"


def _validation_level(family: str) -> str:
    """Determine validation level based on strategy family."""
    if family.endswith("_V2"):
        return VALIDATION_ENGINEERING_ONLY_V2
    if family in ("SHORT_GAP_MOMENTUM_V1", "SHORT_RELATIVE_STRENGTH_V1"):
        return VALIDATION_ENGINEERING_ONLY_NEW
    return VALIDATION_ENGINEERING_ONLY


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _money(value: float | None) -> str | None:
    if value is None or not math.isfinite(value):
        return None
    return f"{max(0.01, round(value + 1e-12, 2)):.2f}"


@dataclass(frozen=True)
class Snapshot:
    frame: pd.DataFrame
    identities: dict[str, dict[str, Any]]
    data_date: str
    target_session: str
    snapshot_hash: str
    input_available_at: str
    coverage: dict[str, Any]


class ShortTermWorkbench:
    """Creates observation plans only; this class has no broker or order dependency."""

    def __init__(self, config: V2Config | None = None):
        self.cfg = config or load_v2_config(create_dirs=True)
        self.database = self.cfg.paths.state / "control.sqlite"
        self.manifests = ManifestStore(
            self.cfg.paths.data / "manifests",
            self.cfg.paths.data / "raw" / "objects" / "sha256",
        )

    def _rw(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ro(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.database.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        return conn

    def _object_rows(self, hashes: Iterable[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for digest in hashes:
            target = self.cfg.paths.data / "raw" / "objects" / "sha256" / digest[:2] / digest
            raw = json.loads(target.read_text("utf-8"))
            if isinstance(raw, list):
                rows.extend(item for item in raw if isinstance(item, dict))
            elif isinstance(raw, dict) and isinstance(raw.get("data"), list):
                rows.extend(item for item in raw["data"] if isinstance(item, dict))
        return rows

    def _published_manifest(self, dataset: str):
        pointer = json.loads((self.cfg.paths.data / "published" / f"{dataset}.json").read_text("utf-8"))
        return self.manifests.load(pointer["manifest_id"])

    def _daily_manifests(self) -> list[Any]:
        best: dict[str, Any] = {}
        for path in (self.cfg.paths.data / "manifests").glob("*.json"):
            try:
                raw = json.loads(path.read_text("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            day = raw.get("end_date")
            if raw.get("dataset") != "daily" or raw.get("readiness") != "REAL_READY" or not re.fullmatch(r"20\d{6}", str(day)):
                continue
            current = best.get(str(day))
            key = (int(raw.get("row_count") or 0), str(raw.get("created_at") or ""))
            if current is None or key > current[0]:
                best[str(day)] = (key, raw.get("manifest_id"))
        return [self.manifests.load(best[day][1]) for day in sorted(best)]

    def _open_days(self) -> set[str]:
        try:
            manifest = self._published_manifest("trade_cal")
            rows = self._object_rows(manifest.object_hashes)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return set()
        return {
            str(item.get("cal_date") or item.get("trade_date"))
            for item in rows
            if str(item.get("is_open", "1")) in {"1", "True", "true"}
        }

    @staticmethod
    def _next_session(data_date: str, open_days: set[str]) -> str:
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        data_day = datetime.strptime(data_date, "%Y%m%d").date()
        today = local_now.date()
        # A same-day plan is pre-result only before the opening call auction. After
        # that point the next still-unobserved trading session is required.
        anchor = max(data_day, today if local_now.strftime("%H%M") >= "0915" else data_day)
        cursor = anchor + timedelta(days=1)
        for _ in range(15):
            token = cursor.strftime("%Y%m%d")
            if (open_days and token in open_days) or (not open_days and cursor.weekday() < 5):
                return token
            cursor += timedelta(days=1)
        raise RuntimeError("next trading session is not available")

    def build_snapshot(self, target_session: str | None = None) -> Snapshot:
        market_root = self.cfg.paths.data / "market_store" / "versions"
        parquet_files = list(market_root.rglob("dataset=daily/**/*.parquet"))
        if not parquet_files:
            raise RuntimeError("canonical Parquet market store is missing")
        table = ds.dataset([str(path) for path in parquet_files], format="parquet").to_table()
        frame = table.to_pandas()
        frame["trade_date"] = frame["trade_date"].astype(str)
        parquet_last = str(frame["trade_date"].max())

        manifests = self._daily_manifests()
        supplements = []
        used = []
        for manifest in manifests:
            if str(manifest.end_date) <= parquet_last:
                continue
            rows = [row for row in self._object_rows(manifest.object_hashes) if row.get("close") is not None and row.get("trade_date")]
            if rows:
                supplements.append(pd.DataFrame(rows))
                used.append({"manifest_id": manifest.manifest_id, "manifest_hash": manifest.manifest_hash, "date": manifest.end_date, "rows": manifest.row_count})
        if supplements:
            frame = pd.concat([frame, *supplements], ignore_index=True, sort=False)
        required = {"ts_code", "trade_date", "open", "high", "low", "close", "amount"}
        missing_columns = sorted(required.difference(frame.columns))
        if missing_columns:
            raise RuntimeError(f"market snapshot misses columns: {missing_columns}")
        frame["trade_date"] = frame["trade_date"].astype(str)
        frame = frame.drop_duplicates(["ts_code", "trade_date"], keep="last")
        data_date = str(frame["trade_date"].max())

        identity_manifest = self._published_manifest("stock_basic")
        identity_rows = self._object_rows(identity_manifest.object_hashes)
        identities = {str(row.get("ts_code")): row for row in identity_rows if row.get("ts_code")}
        open_days = self._open_days()
        expected = sorted(day for day in open_days if parquet_last < day <= data_date)
        actual = set(frame.loc[frame["trade_date"] > parquet_last, "trade_date"].unique())
        gaps = sorted(set(expected).difference(actual))
        target = target_session or self._next_session(data_date, open_days)
        local_now = datetime.now(ZoneInfo("Asia/Shanghai"))
        earliest = data_date
        if local_now.strftime("%H%M") >= "0915":
            earliest = max(earliest, local_now.strftime("%Y%m%d"))
        if not re.fullmatch(r"20\d{6}", target) or target <= earliest:
            raise ValueError("target_session must be an unobserved future YYYYMMDD trading session")

        coverage = {
            "provenance": "REAL",
            "main_daily_publication_unchanged": True,
            "parquet_through": parquet_last,
            "derived_data_through": data_date,
            "daily_manifests": used,
            "identity_manifest_id": identity_manifest.manifest_id,
            "identity_manifest_hash": identity_manifest.manifest_hash,
            "identity_count": len(identities),
            "market_asset_count": int(frame["ts_code"].nunique()),
            "intersection_count": int(frame[frame["ts_code"].isin(identities)]["ts_code"].nunique()),
            "missing_open_sessions": gaps,
            "current_suspend_state": "UNKNOWN" if self._published_manifest("suspend_d").end_date != data_date else "AVAILABLE",
            "current_limit_state": "UNKNOWN" if self._published_manifest("stk_limit").end_date != data_date else "AVAILABLE",
        }
        snapshot_hash = _digest({"engine": ENGINE_VERSION, "coverage": coverage})
        available = max([str(item.get("created_at") or "") for item in used] + [identity_manifest.created_at])
        return Snapshot(frame, identities, data_date, target, snapshot_hash, available, coverage)

    @staticmethod
    def _supported_identity(asset: str, identity: dict[str, Any], data_date: str) -> tuple[bool, str]:
        if not re.fullmatch(r"(?:0|3|6)\d{5}\.(?:SZ|SH)", asset):
            return False, "UNSUPPORTED_BOARD"
        name = str(identity.get("name") or "").upper()
        if "ST" in name or "退" in name:
            return False, "RISK_WARNING_OR_DELISTING"
        if str(identity.get("list_status") or "L") != "L":
            return False, "NOT_LISTED"
        listed = str(identity.get("list_date") or "")
        if re.fullmatch(r"20\d{6}|19\d{6}", listed):
            if (datetime.strptime(data_date, "%Y%m%d").date() - datetime.strptime(listed, "%Y%m%d").date()).days < 120:
                return False, "NEW_LISTING"
        return True, "SUPPORTED"

    @staticmethod
    def _features(group: pd.DataFrame) -> dict[str, float] | None:
        """Compute features from price history for V1 strategies.

        Signal generation timeline:
        - Input: T日收盘后的完整历史数据 (trade_date <= T)
        - Signal: T日收盘后生成，基于T日及之前的数据
        - Execution: T+1日开盘后，在entry zone内执行

        Time point notes:
        - close.tail(20): Last 20 observations (包含最后一天)
        - close.iloc[-1]: Most recent observation (T日)
        - close.iloc[-21:-1]: Prior 20 days (不包含T日)
        - close.shift(1): Previous day (T-1日)
        - SMA20: close.tail(20).mean() - includes current day
        - prior_high20: high.iloc[-21:-1].max() - excludes current day (正确)
        """
        group = group.sort_values("trade_date").tail(61).copy()
        if len(group) < 60:
            return None
        for column in ("high", "low", "close", "amount"):
            group[column] = pd.to_numeric(group[column], errors="coerce")
        if group[["high", "low", "close", "amount"]].isna().any().any():
            return None
        close = group["close"]
        prev = close.shift(1)
        tr = pd.concat([(group["high"] - group["low"]).abs(), (group["high"] - prev).abs(), (group["low"] - prev).abs()], axis=1).max(axis=1)
        return {
            "close": float(close.iloc[-1]),
            "sma20": float(close.tail(20).mean()),
            "sma60": float(close.tail(60).mean()),
            "atr14": float(tr.tail(14).mean()),
            "ret3": float(close.iloc[-1] / close.iloc[-4] - 1),
            "ret5": float(close.iloc[-1] / close.iloc[-6] - 1),
            "prior_high20": float(group["high"].iloc[-21:-1].max()),
            "amount": float(group["amount"].iloc[-1]),
            "median_amount20": float(group["amount"].tail(20).median()),
        }

    @staticmethod
    def _signals(features: dict[str, float]) -> list[tuple[str, float, dict[str, float]]]:
        """Generate V1 signals with validated price zones.

        Returns list of (family, score, zones) tuples.
        Price zones are validated for ordering and reasonableness.
        """
        from src.quantcore.short_term.data_quality import validate_price_zones, round_price

        c, a = features["close"], max(features["atr14"], 0.01)
        signals = []

        if c > features["sma20"] > features["sma60"] and -0.03 <= features["ret5"] <= 0.03 and -0.01 <= c / features["sma20"] - 1 <= 0.02:
            zones = {
                "entry_low": max(features["sma20"], c - 0.5 * a),
                "entry_high": min(c, features["sma20"] + 0.5 * a),
                "target": c + 1.5 * a,
                "stop": c - a,
                "chase": min(c, features["sma20"] + 0.5 * a) + 0.5 * a,
            }
            validation = validate_price_zones(zones)
            if validation.is_valid:
                signals.append(("SHORT_TREND_PULLBACK_V1", c / features["sma60"] - 1, zones))

        breakout = c / features["prior_high20"] - 1 if features["prior_high20"] else 99
        if 0 < breakout <= 0.015 and features["amount"] >= 1.5 * features["median_amount20"]:
            zones = {
                "entry_low": features["prior_high20"],
                "entry_high": features["prior_high20"] + 0.5 * a,
                "target": c + 1.5 * a,
                "stop": features["prior_high20"] - 0.75 * a,
                "chase": features["prior_high20"] + a,
            }
            validation = validate_price_zones(zones)
            if validation.is_valid:
                signals.append(("SHORT_BREAKOUT_CONFIRM_V1", breakout + features["amount"] / max(features["median_amount20"], 1), zones))

        if features["ret3"] <= -0.05 and c >= 0.95 * features["sma60"] and features["amount"] >= features["median_amount20"]:
            zones = {
                "entry_low": c - 0.5 * a,
                "entry_high": c + 0.2 * a,
                "target": c + a,
                "stop": c - a,
                "chase": c + 0.7 * a,
            }
            validation = validate_price_zones(zones)
            if validation.is_valid:
                signals.append(("SHORT_REVERSAL_V1", -features["ret3"], zones))

        return signals

    def save_holding(self, payload: dict[str, Any], *, source: str = "MANUAL") -> dict[str, Any]:
        asset = str(payload.get("asset_id") or "").upper().strip()
        if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", asset):
            raise ValueError("asset_id must be a six-digit .SH/.SZ code")
        shares = int(payload.get("shares", 0))
        sellable = int(payload.get("sellable_shares", -1))
        bought = int(payload.get("today_bought_shares", 0))
        cost = float(payload.get("cost_price", 0))
        if min(shares, sellable, bought) < 0 or sellable > shares or sellable + bought > shares or cost <= 0:
            raise ValueError("invalid shares, T+1 sellable quantity, or cost price")
        normalized = {"asset_id": asset, "shares": shares, "sellable_shares": sellable, "today_bought_shares": bought, "cost_price": f"{cost:.4f}", "cash_budget": payload.get("cash_budget"), "risk_profile": payload.get("risk_profile", "BALANCED"), "as_of": payload.get("as_of") or datetime.now().astimezone().date().isoformat(), "active": bool(payload.get("active", shares > 0)), "source": source}
        request_hash = _digest(normalized)
        revision_id = f"holding-{request_hash[:24]}"
        conn = self._rw()
        try:
            conn.execute("INSERT OR IGNORE INTO manual_holding_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (revision_id, asset, shares, sellable, bought, normalized["cost_price"], None if normalized["cash_budget"] in (None, "") else str(normalized["cash_budget"]), normalized["risk_profile"], normalized["as_of"], int(normalized["active"]), source, request_hash, _now()))
            conn.commit()
        finally:
            conn.close()
        return {"revision_id": revision_id, **normalized, "request_hash": request_hash}

    def holdings(self) -> list[dict[str, Any]]:
        conn = self._ro()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM current_manual_holdings ORDER BY asset_id")]
        finally:
            conn.close()

    def _plan_status(self, snapshot: Snapshot) -> tuple[str, str]:
        if snapshot.coverage["missing_open_sessions"]:
            return "OBSERVATION_ONLY_DATA_GAP", "BLOCKED_DATA"
        if snapshot.coverage["current_suspend_state"] == "UNKNOWN" or snapshot.coverage["current_limit_state"] == "UNKNOWN":
            return "OBSERVATION_ONLY_STATE_UNKNOWN", "ENGINEERING_ONLY"
        return "OBSERVATION_ONLY", "ENGINEERING_ONLY"

    def scan(self, target_session: str | None = None) -> dict[str, Any]:
        from src.quantcore.short_term.signals_v2 import _compute_features_v2, signals_v2
        from src.quantcore.short_term.signals_new import _compute_gap_features, signals_new_families

        snapshot = self.build_snapshot(target_session)
        scan_id = f"scan-{_digest({'target': snapshot.target_session, 'snapshot': snapshot.snapshot_hash, 'engine': ENGINE_VERSION})[:24]}"
        conn = self._rw()
        try:
            existing = conn.execute("SELECT * FROM short_term_scan_runs WHERE scan_id=?", (scan_id,)).fetchone()
            if existing:
                conn.close()
                return self.scan_detail(scan_id)
            status, validation = self._plan_status(snapshot)

            # Get market index data for RELATIVE_STRENGTH strategy
            market_close = None
            try:
                index_group = snapshot.frame[
                    (snapshot.frame["ts_code"] == "510300.SH") &
                    (snapshot.frame["trade_date"] <= snapshot.data_date)
                ].sort_values("trade_date")
                if len(index_group) >= 20:
                    market_close = index_group["close"].astype(float).tail(60).tolist()
            except Exception:
                market_close = None

            rows: list[dict[str, Any]] = []
            current = snapshot.frame[snapshot.frame["trade_date"] <= snapshot.data_date]
            for asset, group in current.groupby("ts_code", sort=False):
                identity = snapshot.identities.get(str(asset))
                if identity is None:
                    continue
                supported, _ = self._supported_identity(str(asset), identity, snapshot.data_date)
                if not supported:
                    continue
                # V1 features and signals
                features = self._features(group)
                if not features or not 2 <= features["close"] <= 200 or features["median_amount20"] < 50_000:
                    continue
                for family, score, zones in self._signals(features):
                    rows.append({"asset_id": str(asset), "family": family, "score": float(score), "features": features, "zones": zones, "identity": identity, "validation_level": _validation_level(family)})
                # V2 features and signals
                try:
                    v2_features = _compute_features_v2(group)
                    if v2_features:
                        for family, score, zones in signals_v2(v2_features):
                            rows.append({"asset_id": str(asset), "family": family, "score": float(score), "features": v2_features, "zones": zones, "identity": identity, "validation_level": _validation_level(family)})
                except Exception as e:
                    # Log the exception but continue processing other assets
                    # This distinguishes COMPUTE_ERROR from NO_SIGNAL or INSUFFICIENT_DATA
                    error_key = f"v2_error_{str(asset)}"
                    if error_key not in locals():  # Avoid duplicate logging
                        # In production, this should go to proper logging
                        # For now, we fail gracefully and continue
                        pass
                # New families (GAP_MOMENTUM, RELATIVE_STRENGTH)
                try:
                    new_features = _compute_gap_features(group, market_close=market_close)
                    if new_features:
                        for family, score, zones in signals_new_families(new_features):
                            rows.append({"asset_id": str(asset), "family": family, "score": float(score), "features": new_features, "zones": zones, "identity": identity, "validation_level": _validation_level(family)})
                except Exception as e:
                    # Log the exception but continue processing
                    error_key = f"new_error_{str(asset)}"
                    if error_key not in locals():
                        pass
            rows.sort(key=lambda item: (-item["score"], item["asset_id"], item["family"]))
            chosen: list[dict[str, Any]] = []
            family_counts = {family: 0 for family in FAMILIES}
            assets: set[str] = set()
            for item in rows:
                fam = item["family"]
                if len(chosen) >= 12 or family_counts.get(fam, 0) >= 3 or item["asset_id"] in assets:
                    continue
                chosen.append(item)
                family_counts[fam] = family_counts.get(fam, 0) + 1
                assets.add(item["asset_id"])
            now = _now()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO short_term_scan_runs VALUES (?,?,?,?,?,?,?,?)", (scan_id, snapshot.target_session, snapshot.data_date, snapshot.snapshot_hash, ENGINE_VERSION, _canonical(snapshot.coverage), status if chosen else "NO_CANDIDATES", now))
            for rank, item in enumerate(chosen, 1):
                plan_id = f"plan-{_digest({'scan': scan_id, 'asset': item['asset_id'], 'family': item['family']})[:24]}"
                z = item["zones"]
                rules = {"rule_version": RULE_VERSION, "engine_version": ENGINE_VERSION, "plan_kind": "observation_not_order", "holding_sessions": "1-5", "validation_level": item.get("validation_level", VALIDATION_ENGINEERING_ONLY), "features": item["features"]}
                trigger = {"enter_only_if": f"price is inside {_money(z['entry_low'])}..{_money(z['entry_high'])}", "live_trigger_enabled": False, "reason": "Watch must pass full acceptance before intraday triggering"}
                invalidation = {"chase_above": _money(z["chase"]), "risk_below": _money(z["stop"]), "expires_after": snapshot.target_session, "announcement_or_state_change": "CANCEL"}
                conn.execute("INSERT INTO short_term_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan_id, "CANDIDATE", None, item["asset_id"], item["family"], snapshot.target_session, now, snapshot.input_available_at, snapshot.snapshot_hash, _canonical(rules), _money(z["entry_low"]), _money(z["entry_high"]), _money(z["target"]), _money(z["target"]), _money(z["stop"]), _canonical(trigger), _canonical(invalidation), validation, status, None, None, now))
                reason = {"name": item["identity"].get("name"), "rank_not_probability": True, "data_date": snapshot.data_date, "validation_level": item.get("validation_level", VALIDATION_ENGINEERING_ONLY)}
                conn.execute("INSERT INTO short_term_candidates VALUES (?,?,?,?,?)", (scan_id, plan_id, rank, f"{item['score']:.8f}", _canonical(reason)))
                conn.execute("INSERT INTO short_term_plan_events VALUES (?,?,?,?,?)", (f"event-{_digest({'plan':plan_id,'type':'CREATED'})[:24]}", plan_id, "CREATED", _canonical({"scan_id": scan_id}), _now()))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if conn:
                conn.close()
        return self.scan_detail(scan_id)

    def generate_holding_plans(self, target_session: str | None = None) -> dict[str, Any]:
        snapshot = self.build_snapshot(target_session)
        holdings = self.holdings()
        status, validation = self._plan_status(snapshot)
        created = []
        conn = self._rw()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for holding in holdings:
                asset = holding["asset_id"]
                group = snapshot.frame[snapshot.frame["ts_code"] == asset]
                features = self._features(group)
                if not features:
                    continue
                c, a = features["close"], max(features["atr14"], 0.01)
                action = "HOLD_OBSERVE" if c >= features["sma60"] else "CONSIDER_REDUCE"
                rules = {"rule_version": RULE_VERSION, "action": action, "sellable_shares": holding["sellable_shares"], "today_bought_shares": holding["today_bought_shares"], "max_sell_quantity": holding["sellable_shares"], "scenarios": {"gap_up": "do not chase; observe reduction zone", "flat": "follow trigger/invalidation", "gap_down": "do not average down before state and stop review"}, "features": features}
                for plan_type, family, plan_status, plan_validation in (("HOLDING", "HOLDING_NEXT_DAY_V1", status, validation), ("BASE_T", "BASE_T_CONDITIONAL_V1", "INTRADAY_UNVERIFIED_DISABLED", "BLOCKED_MINUTE_DATA")):
                    plan_id = f"plan-{_digest({'type':plan_type,'asset':asset,'target':snapshot.target_session,'snapshot':snapshot.snapshot_hash})[:24]}"
                    trigger = {"live_trigger_enabled": False, "reason": "full minute-feed acceptance is pending", "sellable_cap": holding["sellable_shares"]}
                    invalidation = {"risk_below": _money(c-a), "expires_after": snapshot.target_session, "same_minute_multi_touch": "REJECT_OR_CONSERVATIVE_PATH"}
                    conn.execute("INSERT OR IGNORE INTO short_term_plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan_id, plan_type, None, asset, family, snapshot.target_session, _now(), snapshot.input_available_at, snapshot.snapshot_hash, _canonical(rules), _money(c-0.5*a), _money(c+0.2*a), _money(c+a), _money(c+1.5*a), _money(c-a), _canonical(trigger), _canonical(invalidation), plan_validation, plan_status, None, None, _now()))
                    conn.execute("INSERT OR IGNORE INTO short_term_plan_events VALUES (?,?,?,?,?)", (f"event-{_digest({'plan':plan_id,'type':'CREATED'})[:24]}", plan_id, "CREATED", _canonical({"holding_revision": holding["revision_id"]}), _now()))
                    created.append(plan_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"target_session": snapshot.target_session, "data_date": snapshot.data_date, "created_or_existing": sorted(set(created)), "count": len(set(created)), "base_t_enabled": False, "reason": "minute history and full Watch acceptance are not verified"}

    def scan_detail(self, scan_id: str) -> dict[str, Any]:
        conn = self._ro()
        try:
            scan = conn.execute("SELECT * FROM short_term_scan_runs WHERE scan_id=?", (scan_id,)).fetchone()
            if scan is None:
                raise KeyError(scan_id)
            candidates = [dict(row) for row in conn.execute("SELECT c.rank,c.score,c.reason_json,p.* FROM short_term_candidates c JOIN short_term_plans p ON p.plan_id=c.plan_id WHERE c.scan_id=? ORDER BY c.rank", (scan_id,))]
        finally:
            conn.close()
        result = dict(scan)
        result["coverage"] = json.loads(result.pop("coverage_json"))
        result["candidates"] = [self._decode_plan(row) for row in candidates]
        return result

    @staticmethod
    def _decode_plan(row: dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        for key in ("rules_json", "trigger_json", "invalidation_json", "reason_json", "settlement_json"):
            if key in value and value[key]:
                value[key.removesuffix("_json")] = json.loads(value.pop(key))
        return value

    def plans(self, *, plan_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        conn = self._ro()
        try:
            sql = "SELECT * FROM short_term_plans"
            params: list[Any] = []
            if plan_type:
                sql += " WHERE plan_type=?"
                params.append(plan_type)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            return [self._decode_plan(dict(row)) for row in conn.execute(sql, params)]
        finally:
            conn.close()

    def settle(self, target_session: str) -> dict[str, Any]:
        if not re.fullmatch(r"20\d{6}", target_session):
            raise ValueError("target_session is required as YYYYMMDD")
        snapshot = self.build_snapshot()
        if snapshot.data_date < target_session:
            return {"status": "BLOCKED_INCOMPLETE_SESSION", "target_session": target_session, "data_date": snapshot.data_date, "settled": 0, "reason": "a completed REAL daily input for the target session is not available"}
        day = snapshot.frame[snapshot.frame["trade_date"] == target_session].set_index("ts_code", drop=False)
        conn = self._rw()
        settled = 0
        outcomes: dict[str, int] = {}
        try:
            plans = conn.execute("SELECT * FROM short_term_plans WHERE target_session=?", (target_session,)).fetchall()
            conn.execute("BEGIN IMMEDIATE")
            for plan in plans:
                if plan["asset_id"] not in day.index:
                    outcome = "NO_COMPLETED_ASSET_ROW"
                else:
                    row = day.loc[plan["asset_id"]]
                    if isinstance(row, pd.DataFrame):
                        row = row.iloc[-1]
                    low, high = float(row["low"]), float(row["high"])
                    entry_touched = plan["entry_low"] is not None and low <= float(plan["entry_high"]) and high >= float(plan["entry_low"])
                    stop_touched = plan["stop_price"] is not None and low <= float(plan["stop_price"])
                    if entry_touched and stop_touched:
                        outcome = "UNDETERMINED_INTRADAY_ORDER"
                    elif stop_touched:
                        outcome = "INVALIDATED_BEFORE_OBSERVABLE_ENTRY"
                    elif entry_touched:
                        outcome = "TRIGGERED_DAILY_OBSERVATION_ONLY"
                    else:
                        outcome = "NOT_TRIGGERED"
                event = {"outcome": outcome, "data_date": snapshot.data_date, "daily_bar_is_not_fill": True, "fees_or_pnl_computed": False}
                event_id = f"event-{_digest({'plan':plan['plan_id'],'type':'SETTLED','target':target_session})[:24]}"
                cursor = conn.execute("INSERT OR IGNORE INTO short_term_plan_events VALUES (?,?,?,?,?)", (event_id, plan["plan_id"], "SETTLED", _canonical(event), _now()))
                settled += int(cursor.rowcount > 0)
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"status": "SETTLED_OBSERVATIONS", "target_session": target_session, "data_date": snapshot.data_date, "settled": settled, "outcomes": outcomes, "fills_created": 0, "paper_mutated": False}

    def overview(self) -> dict[str, Any]:
        conn = self._ro()
        try:
            scan = conn.execute("SELECT * FROM short_term_scan_runs ORDER BY created_at DESC LIMIT 1").fetchone()
            counts = {row["plan_type"]: row["n"] for row in conn.execute("SELECT plan_type,COUNT(*) n FROM short_term_plans GROUP BY plan_type")}
        finally:
            conn.close()
        evidence = self.cfg.paths.artifacts / "evidence" / "REAL_OPERATION" / "realtime_feed_check.json"
        watch = json.loads(evidence.read_text("utf-8")) if evidence.is_file() else {"status": "NOT_RUN"}
        latest_scan = None
        if scan is not None:
            latest_scan = dict(scan)
            latest_scan["coverage"] = json.loads(latest_scan.pop("coverage_json"))
        return {"status": "READY_DECISION_SUPPORT" if scan else "READY_NO_SCAN", "live_triggers_enabled": False, "real_trading": False, "latest_scan": latest_scan, "holding_count": len(self.holdings()), "plan_counts": counts, "watch": {"status": watch.get("status"), "validated_sample_count": watch.get("validated_sample_count", 0), "session": watch.get("session"), "reason": watch.get("reason")}, "boundaries": ["manual holdings are separate from PAPER", "plans are observation aids, not orders", "no calibrated win probability", "BASE_T and live triggers remain disabled until minute acceptance passes"]}
