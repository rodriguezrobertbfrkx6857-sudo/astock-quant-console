from __future__ import annotations

from datetime import date, datetime

from .models import (
    Confidence,
    IdentifierVersion,
    SecurityIdentity,
    SecurityStateVersion,
    require_aware,
)


class HistoricalSecurityMaster:
    def __init__(
        self,
        identities: list[SecurityIdentity],
        identifiers: list[IdentifierVersion],
        states: list[SecurityStateVersion],
    ):
        self.identities = {item.asset_id: item for item in identities}
        if len(self.identities) != len(identities):
            raise ValueError("duplicate asset_id")
        self.identifiers = tuple(identifiers)
        self._states = tuple(states)
        for identifier in self.identifiers:
            if identifier.asset_id not in self.identities:
                raise ValueError(f"unknown asset_id: {identifier.asset_id}")
        for state in self._states:
            if state.asset_id not in self.identities:
                raise ValueError(f"unknown asset_id: {state.asset_id}")
        self._reject_ambiguous_states()

    def _reject_ambiguous_states(self) -> None:
        grouped: dict[str, list[SecurityStateVersion]] = {}
        for state in self._states:
            grouped.setdefault(state.asset_id, []).append(state)
        for asset_id, items in grouped.items():
            for index, left in enumerate(items):
                for right in items[index + 1 :]:
                    effective_overlap = (
                        left.effective_to is None
                        or right.effective_from < left.effective_to
                    ) and (
                        right.effective_to is None
                        or left.effective_from < right.effective_to
                    )
                    known_overlap = (
                        left.known_to is None or right.known_from < left.known_to
                    ) and (right.known_to is None or left.known_from < right.known_to)
                    if effective_overlap and known_overlap:
                        raise ValueError(f"ambiguous bitemporal states for {asset_id}")

    def state_as_of(
        self,
        asset_id: str,
        effective_date: date,
        decision_time: datetime,
        *,
        strict: bool = True,
    ) -> SecurityStateVersion:
        matches = [
            item
            for item in self._states
            if item.asset_id == asset_id
            and item.visible_at(effective_date, decision_time)
        ]
        if len(matches) != 1:
            raise LookupError(
                f"no unique state for {asset_id} at {effective_date}/{decision_time.isoformat()}"
            )
        state = matches[0]
        if strict and state.confidence is not Confidence.VERIFIED:
            raise LookupError(f"state is not verified for {asset_id}")
        return state

    def identifier_as_of(
        self, scheme: str, value: str, effective_date: date, decision_time: datetime
    ) -> str:
        decision_time = require_aware(decision_time, "decision_time")
        matches = [
            item
            for item in self.identifiers
            if item.scheme == scheme
            and item.value == value
            and item.effective_from <= effective_date
            and (item.effective_to is None or effective_date < item.effective_to)
            and item.known_from <= decision_time
            and (item.known_to is None or decision_time < item.known_to)
        ]
        if len(matches) != 1 or matches[0].confidence is not Confidence.VERIFIED:
            raise LookupError("identifier is unknown or ambiguous as-of")
        return matches[0].asset_id
