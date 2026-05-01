"""Tests for min_bot entry-awareness fields on trade + settlement records.

Added 2026-05-01 to give min_bot V2-parity for retro-evaluation. Without
these fields, catching-knife / late-day / bias-correction filters cannot
be backtested against min_bot's settled pool — the per-trade context is
needed.

Fields added to trade_record (entry) AND settlement record:
  - entry_local_hour, entry_local_dow, entry_local_ts, entry_tz
  - entry_hours_to_sunrise
  - entry_yes_bid_cents / yes_ask_cents / no_bid_cents / no_ask_cents
  - entry_volume, entry_spread_cents
  - mu_nbp_at_entry, sigma_nbp_at_entry
  - mu_nbm_om_at_entry, mu_hrrr_at_entry
  - disagreement_at_entry
  - post_sunrise_lock_at_entry, is_today_at_entry
  - yes_ask_frac_at_entry, no_ask_frac_at_entry
  - edge (already present at entry, now also flows to settlement)
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))


REQUIRED_ENTRY_FIELDS = [
    "entry_local_hour", "entry_local_dow", "entry_local_ts",
    "entry_hours_to_sunrise", "entry_tz",
    "entry_yes_bid_cents", "entry_yes_ask_cents",
    "entry_no_bid_cents", "entry_no_ask_cents",
    "entry_volume", "entry_spread_cents",
    "mu_nbp_at_entry", "sigma_nbp_at_entry",
    "mu_nbm_om_at_entry", "mu_hrrr_at_entry",
    "disagreement_at_entry",
    "post_sunrise_lock_at_entry", "is_today_at_entry",
    "yes_ask_frac_at_entry", "no_ask_frac_at_entry",
]


def _read_source():
    return (Path(__file__).parent.parent / "paper_min_bot.py").read_text()


# ─────────────────────────────────────────────────────────────────────────────
# Source-level invariants — verify the fields are being written
# ─────────────────────────────────────────────────────────────────────────────
def test_trade_record_includes_entry_local_hour():
    src = _read_source()
    assert '"entry_local_hour":' in src
    # The entry-write site is `trade_record = {` followed by _append_jsonl(_trades_file_today()
    e = src.index("trade_record = {")
    a = src.index("_append_jsonl(_trades_file_today()", e)
    block = src[e:a]
    for field in REQUIRED_ENTRY_FIELDS:
        assert f'"{field}":' in block, f"trade_record missing {field}"


def test_kalshi_fallback_settlement_includes_entry_fields():
    """Kalshi-fallback settlement path must propagate every entry-awareness
    field via pos → settlement record."""
    src = _read_source()
    # Find the kalshi-fallback settlement dict — has `"source": "kalshi"`
    pos = src.index('"source": "kalshi"')
    head = src[max(0, pos - 4000):pos]
    for field in REQUIRED_ENTRY_FIELDS:
        assert f'"{field}":' in head, f"kalshi settlement missing {field}"


def test_obs_pipeline_settlement_includes_entry_fields():
    """obs-pipeline (CLI-based) settlement path must also propagate fields."""
    src = _read_source()
    pos = src.index('"source": "obs_pipeline"')
    head = src[max(0, pos - 4000):pos]
    for field in REQUIRED_ENTRY_FIELDS:
        assert f'"{field}":' in head, f"obs_pipeline settlement missing {field}"


def test_entry_local_hour_uses_zoneinfo():
    """Local hour must be computed from the position's tz, not UTC."""
    src = _read_source()
    e = src.index("trade_record = {")
    block = src[max(0, e - 1500):e]
    assert "ZoneInfo(_entry_tz_name)" in block, \
        "entry_local_hour must use ZoneInfo on opp.tz"


def test_settlement_includes_edge():
    """The bot's `edge` (computed at entry) was previously stored only in
    trade_record; settlement record now mirrors it for retro-analysis."""
    src = _read_source()
    for marker in ('"source": "kalshi"', '"source": "obs_pipeline"'):
        pos = src.index(marker)
        head = src[max(0, pos - 3000):pos]
        assert '"edge":' in head, f"settlement at {marker} missing edge"


# ─────────────────────────────────────────────────────────────────────────────
# Behavioral: simulate a settlement record by reading source patterns
# ─────────────────────────────────────────────────────────────────────────────
def test_running_min_at_entry_still_present():
    """Backward-compat: existing readers depend on `running_min_at_entry`
    being populated. New fields are additive, not replacing."""
    src = _read_source()
    for marker in ('"source": "kalshi"', '"source": "obs_pipeline"'):
        pos = src.index(marker)
        head = src[max(0, pos - 3000):pos]
        assert '"running_min_at_entry":' in head, \
            f"running_min_at_entry missing at {marker} — would break backtests"


def test_no_field_removed_from_settlement():
    """Original settlement fields must still be present."""
    src = _read_source()
    for marker in ('"source": "kalshi"', '"source": "obs_pipeline"'):
        pos = src.index(marker)
        head = src[max(0, pos - 4500):pos]
        for f in ('"market_ticker":', '"action":', '"entry_price":', '"count":',
                  '"cost":', '"floor":', '"cap":', '"in_bracket":', '"won":',
                  '"revenue":', '"pnl":', '"model_prob":', '"mu":', '"sigma":',
                  '"mu_source":', '"station":', '"date_str":', '"label":'):
            assert f in head, f"settlement at {marker} dropped {f}"
