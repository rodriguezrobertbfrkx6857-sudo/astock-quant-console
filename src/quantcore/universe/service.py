from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum

from src.quantcore.data.hashing import canonical_hash

from .models import Board, RiskStatus, SecurityType, TradingStatus
from .repository import HistoricalSecurityMaster


class DecisionReason(StrEnum):
    INCLUDED = "INCLUDED"
    NOT_LISTED = "NOT_LISTED"
    DELISTED = "DELISTED"
    SUSPENDED = "SUSPENDED"
    RISK_EXCLUDED = "RISK_EXCLUDED"
    UNKNOWN_STATE = "UNKNOWN_STATE"
    BOARD_EXCLUDED = "BOARD_EXCLUDED"
    TYPE_EXCLUDED = "TYPE_EXCLUDED"
    MEMBERSHIP_EXCLUDED = "MEMBERSHIP_EXCLUDED"
    DATA_INSUFFICIENT = "DATA_INSUFFICIENT"
    HELD_FOR_VALUATION = "HELD_FOR_VALUATION"


@dataclass(frozen=True)
class UniversePolicy:
    security_types: frozenset[SecurityType] = frozenset({SecurityType.STOCK})
    boards: frozenset[Board] = frozenset(
        {Board.SSE_MAIN, Board.STAR, Board.SZSE_MAIN, Board.CHINEXT, Board.BSE}
    )
    exclude_risk: bool = True
    exclude_suspended: bool = True


@dataclass(frozen=True)
class MembershipVersion:
    group: str
    asset_id: str
    effective_from: date
    effective_to: date | None
    available_at: datetime
    source_ref: str
    verified: bool

    def __post_init__(self) -> None:
        from .models import require_aware

        require_aware(self.available_at, "available_at")
        if self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("membership effective interval is invalid")
        if not self.source_ref:
            raise ValueError("membership source_ref is required")

    def active(self, as_of: date, decision_time: datetime) -> bool:
        return (
            self.effective_from <= as_of
            and (self.effective_to is None or as_of < self.effective_to)
            and self.available_at <= decision_time
        )


@dataclass(frozen=True)
class UniverseDecision:
    asset_id: str
    included: bool
    reasons: tuple[DecisionReason, ...]


@dataclass(frozen=True)
class UniverseSnapshot:
    snapshot_id: str
    as_of: date
    decision_time: datetime
    selection_members: tuple[str, ...]
    valuation_assets: tuple[str, ...]
    decisions: tuple[UniverseDecision, ...]
    readiness: str
    content_hash: str


class HistoricalUniverseService:
    def __init__(
        self,
        master: HistoricalSecurityMaster,
        memberships: list[MembershipVersion] | None = None,
    ):
        self.master = master
        self.memberships = tuple(memberships or ())

    def snapshot(
        self,
        as_of: date,
        decision_time: datetime,
        policy: UniversePolicy,
        *,
        membership_group: str | None = None,
        held_assets: frozenset[str] = frozenset(),
    ) -> UniverseSnapshot:
        membership: set[str] | None = None
        global_blocker = False
        if membership_group:
            rows = [
                item
                for item in self.memberships
                if item.group == membership_group and item.active(as_of, decision_time)
            ]
            if not rows or any(not item.verified for item in rows):
                global_blocker = True
                membership = set()
            else:
                membership = {item.asset_id for item in rows}
        decisions = []
        for asset_id, identity in sorted(self.master.identities.items()):
            reasons: list[DecisionReason] = []
            try:
                state = self.master.state_as_of(asset_id, as_of, decision_time)
                status = state.trading_status(as_of)
                if status is TradingStatus.NOT_LISTED:
                    reasons.append(DecisionReason.NOT_LISTED)
                elif status is TradingStatus.DELISTED:
                    reasons.append(DecisionReason.DELISTED)
                elif status is TradingStatus.SUSPENDED and policy.exclude_suspended:
                    reasons.append(DecisionReason.SUSPENDED)
                elif status is TradingStatus.UNKNOWN:
                    reasons.append(DecisionReason.UNKNOWN_STATE)
                if state.board not in policy.boards:
                    reasons.append(DecisionReason.BOARD_EXCLUDED)
                if policy.exclude_risk and state.risk_status is not RiskStatus.NORMAL:
                    reasons.append(
                        DecisionReason.RISK_EXCLUDED
                        if state.risk_status is not RiskStatus.UNKNOWN
                        else DecisionReason.UNKNOWN_STATE
                    )
            except LookupError:
                reasons.append(DecisionReason.DATA_INSUFFICIENT)
            if identity.security_type not in policy.security_types:
                reasons.append(DecisionReason.TYPE_EXCLUDED)
            if membership is not None and asset_id not in membership:
                reasons.append(DecisionReason.MEMBERSHIP_EXCLUDED)
            decisions.append(
                UniverseDecision(
                    asset_id,
                    not reasons,
                    tuple(dict.fromkeys(reasons))
                    if reasons
                    else (DecisionReason.INCLUDED,),
                )
            )
        selected = tuple(item.asset_id for item in decisions if item.included)
        valuation = tuple(sorted(set(selected) | set(held_assets)))
        if held_assets:
            decisions = [
                UniverseDecision(
                    item.asset_id,
                    item.included,
                    item.reasons
                    + (
                        (DecisionReason.HELD_FOR_VALUATION,)
                        if item.asset_id in held_assets and not item.included
                        else ()
                    ),
                )
                for item in decisions
            ]
        blocked = global_blocker or any(
            DecisionReason.DATA_INSUFFICIENT in x.reasons
            or DecisionReason.UNKNOWN_STATE in x.reasons
            for x in decisions
        )
        payload = {
            "as_of": as_of.isoformat(),
            "decision_time": decision_time.isoformat(),
            "selected": selected,
            "valuation": valuation,
            "decisions": [
                (x.asset_id, x.included, [r.value for r in x.reasons])
                for x in decisions
            ],
        }
        digest = canonical_hash(payload)
        return UniverseSnapshot(
            f"universe-{digest[:20]}",
            as_of,
            decision_time,
            selected,
            valuation,
            tuple(decisions),
            "BLOCKED" if blocked else "READY",
            digest,
        )
