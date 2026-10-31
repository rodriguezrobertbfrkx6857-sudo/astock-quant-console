from datetime import UTC, datetime, timedelta

from src.quantcore.short_term.watch import assess_observation, receipt_key


def test_unverified_feed_blocks_signal() -> None:
    now = datetime.now(UTC)
    result = assess_observation(source_timestamp=now, observed_at=now, previous_source_timestamp=None, full_acceptance=False)
    assert result.state == "BLOCKED_UNVERIFIED"
    assert result.actionable is False


def test_stale_and_out_of_order_observations_block() -> None:
    now = datetime.now(UTC)
    stale = assess_observation(source_timestamp=now - timedelta(seconds=91), observed_at=now, previous_source_timestamp=None, full_acceptance=True)
    duplicate = assess_observation(source_timestamp=now, observed_at=now, previous_source_timestamp=now, full_acceptance=True)
    assert stale.state == "BLOCKED_STALE"
    assert duplicate.state == "BLOCKED_OUT_OF_ORDER"


def test_receipt_key_deduplicates_same_plan_event() -> None:
    first = receipt_key("plan-1", "ENTRY", "2026-09-11T14:00:00+08:00")
    second = receipt_key("plan-1", "ENTRY", "2026-09-11T14:00:00+08:00")
    assert first == second
