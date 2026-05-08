"""Tests for min_bot EVENT_CAP_UNDERFILL_THRESHOLD_USD exemption (2026-05-08).

The exemption: positions with actual_cost < $3 don't count toward
MAX_OPEN_PER_EVENT. This frees the slot when bot under-fills due to thin
KXLOW markets, allowing better entries on different brackets within the
same event.
"""
import os
import re
import sys
from unittest.mock import patch

# Allow importing the live source for unit tests
sys.path.insert(0, "/home/ubuntu/paper_min_bot")


def test_constant_present():
    src = open("/home/ubuntu/paper_min_bot/paper_min_bot.py").read()
    assert re.search(
        r"^EVENT_CAP_UNDERFILL_THRESHOLD_USD\s*=\s*3\.0",
        src, re.MULTILINE), \
        "EVENT_CAP_UNDERFILL_THRESHOLD_USD constant missing"


def test_threshold_documented():
    """Threshold should reference rationale (KXLOW thin markets)."""
    src = open("/home/ubuntu/paper_min_bot/paper_min_bot.py").read()
    # Look for the constant + accompanying comment
    idx = src.find("EVENT_CAP_UNDERFILL_THRESHOLD_USD")
    surrounding = src[max(0, idx - 1500):idx + 50]
    assert "thin" in surrounding.lower() or "underfill" in surrounding.lower() \
        or "under-fill" in surrounding.lower() or "under fill" in surrounding.lower(), \
        "threshold should be documented with thin-market / under-fill rationale"


def test_open_count_function_uses_threshold():
    src = open("/home/ubuntu/paper_min_bot/paper_min_bot.py").read()
    # Find _open_count_for_event function body
    m = re.search(
        r"def _open_count_for_event\(event_ticker.*?\n(?=def |\Z)",
        src, flags=re.DOTALL)
    assert m, "_open_count_for_event function not found"
    body = m.group(0)
    assert "EVENT_CAP_UNDERFILL_THRESHOLD_USD" in body, \
        "_open_count_for_event must reference the threshold constant"
    assert "actual_cost" in body, \
        "_open_count_for_event must read actual_cost from position"


def test_open_count_skips_tiny_positions():
    """Functional test: positions with cost < threshold don't count."""
    import paper_min_bot as mb

    # Mock _open_positions and _positions_lock
    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {
            "actual_cost": 0.41, "settled": False
        },
        "KXLOWTDAL-26MAY08-T61": {
            "actual_cost": 25.0, "settled": False
        },
    }
    with patch.object(mb, "_open_positions", fake_positions):
        # Tiny position should be excluded; full one counted
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 1, f"expected 1 (tiny excluded, full counted), got {n}"


def test_open_count_counts_full_positions():
    import paper_min_bot as mb

    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {"actual_cost": 25.0, "settled": False},
        "KXLOWTDAL-26MAY08-T61":   {"actual_cost": 30.0, "settled": False},
    }
    with patch.object(mb, "_open_positions", fake_positions):
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 2, f"expected 2 (both full), got {n}"


def test_open_count_skips_settled():
    import paper_min_bot as mb

    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {"actual_cost": 25.0, "settled": True},
        "KXLOWTDAL-26MAY08-T61":   {"actual_cost": 30.0, "settled": False},
    }
    with patch.object(mb, "_open_positions", fake_positions):
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 1, f"expected 1 (settled excluded), got {n}"


def test_open_count_skips_other_events():
    import paper_min_bot as mb

    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {"actual_cost": 25.0, "settled": False},
        "KXLOWTAUS-26MAY08-T58":   {"actual_cost": 30.0, "settled": False},
    }
    with patch.object(mb, "_open_positions", fake_positions):
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 1, f"expected 1 (only DAL counted), got {n}"


def test_open_count_falls_back_to_cost_field():
    """If actual_cost missing, fall back to 'cost' field (legacy positions)."""
    import paper_min_bot as mb

    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {"cost": 25.0, "settled": False},
    }
    with patch.object(mb, "_open_positions", fake_positions):
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 1, f"expected 1 (cost fallback), got {n}"


def test_open_count_handles_missing_cost():
    """If both actual_cost and cost are None, treat as 0 (tiny → excluded)."""
    import paper_min_bot as mb

    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {"settled": False},  # no cost field at all
    }
    with patch.object(mb, "_open_positions", fake_positions):
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 0, f"expected 0 (missing cost = tiny = excluded), got {n}"


def test_threshold_value_is_3_dollars():
    """Threshold value is exactly $3.00 — matches backtest design."""
    import paper_min_bot as mb
    assert mb.EVENT_CAP_UNDERFILL_THRESHOLD_USD == 3.0


def test_open_count_returns_zero_for_empty_event_ticker():
    import paper_min_bot as mb
    assert mb._open_count_for_event("") == 0
    assert mb._open_count_for_event(None) == 0


def test_real_world_today_scenario():
    """Reproduce today's TDAL-MAY08 scenario: tiny B60.5 + blocked T61."""
    import paper_min_bot as mb

    fake_positions = {
        "KXLOWTDAL-26MAY08-B60.5": {"actual_cost": 0.41, "settled": False},
    }
    with patch.object(mb, "_open_positions", fake_positions):
        # Without exemption: count=1, would block new entries
        # With exemption (this commit): count=0, slot available for T61
        n = mb._open_count_for_event("KXLOWTDAL-26MAY08")
        assert n == 0, \
            f"tiny $0.41 position should NOT lock the event slot, got count={n}"


def test_max_open_per_event_unchanged():
    """MAX_OPEN_PER_EVENT itself stays at 1; exemption only changes counting."""
    import paper_min_bot as mb
    assert mb.MAX_OPEN_PER_EVENT == 1, \
        "MAX_OPEN_PER_EVENT must stay 1; exemption is the only change"


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
