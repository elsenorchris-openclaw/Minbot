#!/usr/bin/env python3.12
"""
min_bot — Kalshi daily-low temperature live trading bot.

Trades real money on KXLOWT* markets (live since 2026-04-25). Uses the
obs-pipeline's running_min tracking + forecast fetchers to predict overnight
lows, compares against Kalshi's live bracket quotes, places limit-buy
orders with hard caps on bet size, per-cycle entries, daily exposure, and
edge sanity gates.

ARCHITECTURE:
  Single live executor. WS-based market discovery via kalshi_ws.py.
  REST POST for order placement; WS fill cache for sub-second confirmation;
  REST GET as authoritative fallback.

SAFETY:
  - Wallet selectable via WALLET ('v1' = ~/.env+~/kalshi_key.pem; 'v2' =
    obs-pipeline-bot/kalshi_key_v2_account2.pem)
  - Separate systemd unit (paper-min-bot.service — name kept for backward
    compat with backups; bot is live, not paper)
  - Separate data dir; does NOT touch V1/V2 max-bot positions, trades, stats

DATA:
  - Reads obs from obs-pipeline sqlite (/home/ubuntu/obs-pipeline/data/obs.sqlite)
  - Fetches NBP sigma + mu from NBM Probabilistic text bulletins on AWS S3
  - Fetches NBM hourly min via Open-Meteo
  - Writes orders + settlement outcomes to data/trades.jsonl
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import math
import os
import re
import signal
import sqlite3
import statistics
import sys
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import kalshi_ws  # WebSocket BBO client (V2 pattern, ported 2026-04-24)

# 2026-05-04 evening: cross-bot decision audit log (Phase B min_bot wiring).
# Records full state at every fork into shared_tools/data/bot_decisions.sqlite.
# Failures never propagate — record() returns False on error, caller continues.
sys.path.insert(0, '/home/ubuntu/shared_tools')
try:
    import decision_log as _dlog
except Exception:
    _dlog = None  # bot operates normally without audit if library unavailable

# 2026-05-05 evening: per-station bias correction (Phase B min_bot wiring).
# Reads from shared_tools/data/per_station_bias.json (refreshed nightly by
# per_station_bias.py at 14:35 UTC). Returns None for non-applicable cells
# so this is a SOFT augmentation: missing/stale/malformed table → no-op.
# Sign convention: positive bias = forecast warmer than actual; corrected
# mu = raw_mu - bias.
#
# Rollout: as of 2026-05-05, the bias table has NO applicable min cells
# (min_bot is the only contributor; n=5-6 < min_n=7 threshold). KAUS d-0
# min/HRRR (bias=+3.37, n=6) is one day shy of activation. Auto-activates
# without code change as cells reach n=7+ over the next 1-7 days.
try:
    import bias_correction as _bias_correction
except Exception:
    _bias_correction = None  # bot operates normally without bias correction
USE_BIAS_CORRECTION = False  # 2026-05-08: disabled (n=4 trades, mechanism too weak to flip [0.05, 0.85] BUY_NO band — only nudges position size by a few %; cron table cont.refresh for re-enable)
                             # → zero behavioral impact; auto-activates as
                             # data accumulates).

# ═══════════════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════════════

# Wallet selection. "v1" → ~/.env KALSHI_KEY_ID + ~/kalshi_key.pem (account 1).
# "v2" → _KALSHI_KEY_ID_V2 const + obs-pipeline-bot/kalshi_key_v2_account2.pem
# min_bot trades KXLOWT* — distinct from V1's KXHIGH* — so wallet sharing
# is fine.
WALLET = "v1"

# Path to the obs-pipeline sqlite DB (read-only queries for running_min).
OBS_DB_PATH = "/home/ubuntu/obs-pipeline/data/obs.sqlite"
OBS_SNAPSHOT_URL = "http://127.0.0.1:8089/snapshot"

# 20-city roster — matches V1/V2. Series ticker is the KXLOWT* prefix.
# station: the ASOS ICAO that Kalshi settles against.
CITIES: dict[str, dict[str, Any]] = {
    "KXLOWTNYC":  {"station": "KNYC", "lat": 40.7833, "lon": -73.9667, "tz": "America/New_York",    "label": "NYC"},
    "KXLOWTCHI":  {"station": "KMDW", "lat": 41.7842, "lon": -87.7553, "tz": "America/Chicago",     "label": "Chicago"},
    "KXLOWTMIA":  {"station": "KMIA", "lat": 25.7906, "lon": -80.3164, "tz": "America/New_York",    "label": "Miami"},
    "KXLOWTLAX":  {"station": "KLAX", "lat": 33.9381, "lon": -118.3889,"tz": "America/Los_Angeles", "label": "LA"},
    "KXLOWTDEN":  {"station": "KDEN", "lat": 39.8466, "lon": -104.6562,"tz": "America/Denver",      "label": "Denver"},
    "KXLOWTAUS":  {"station": "KAUS", "lat": 30.1830, "lon": -97.6799, "tz": "America/Chicago",     "label": "Austin"},
    "KXLOWTPHIL": {"station": "KPHL", "lat": 39.8733, "lon": -75.2268, "tz": "America/New_York",    "label": "Philadelphia"},
    "KXLOWTATL":  {"station": "KATL", "lat": 33.6407, "lon": -84.4277, "tz": "America/New_York",    "label": "Atlanta"},
    "KXLOWTBOS":  {"station": "KBOS", "lat": 42.3606, "lon": -71.0106, "tz": "America/New_York",    "label": "Boston"},
    "KXLOWTDAL":  {"station": "KDFW", "lat": 32.8974, "lon": -97.0220, "tz": "America/Chicago",     "label": "Dallas"},
    "KXLOWTDC":   {"station": "KDCA", "lat": 38.8483, "lon": -77.0342, "tz": "America/New_York",    "label": "Washington DC"},
    "KXLOWTHOU":  {"station": "KHOU", "lat": 29.6375, "lon": -95.2825, "tz": "America/Chicago",     "label": "Houston"},
    "KXLOWTLV":   {"station": "KLAS", "lat": 36.0719, "lon": -115.1634,"tz": "America/Los_Angeles", "label": "Las Vegas"},
    "KXLOWTMIN":  {"station": "KMSP", "lat": 44.8831, "lon": -93.2289, "tz": "America/Chicago",     "label": "Minneapolis"},
    "KXLOWTNOLA": {"station": "KMSY", "lat": 29.9928, "lon": -90.2508, "tz": "America/Chicago",     "label": "New Orleans"},
    "KXLOWTOKC":  {"station": "KOKC", "lat": 35.3931, "lon": -97.6007, "tz": "America/Chicago",     "label": "Oklahoma City"},
    "KXLOWTPHX":  {"station": "KPHX", "lat": 33.4373, "lon": -112.008, "tz": "America/Phoenix",     "label": "Phoenix"},
    "KXLOWTSATX": {"station": "KSAT", "lat": 29.5328, "lon": -98.4636, "tz": "America/Chicago",     "label": "San Antonio"},
    "KXLOWTSEA":  {"station": "KSEA", "lat": 47.4447, "lon": -122.3136,"tz": "America/Los_Angeles", "label": "Seattle"},
    "KXLOWTSFO":  {"station": "KSFO", "lat": 37.6196, "lon": -122.3656,"tz": "America/Los_Angeles", "label": "San Francisco"},
}

# Station → IANA tz lookup (used by _cli_is_final to compute climate-day end).
_STATION_TZ: dict[str, str] = {info["station"]: info["tz"] for info in CITIES.values()}

# NBM Probabilistic product station IDs (same as v1/v2 use for max).
# These are the 3-letter NBP identifiers; mapping from series ticker.
# Source: NBM bulletin headers (NBPCWL, NBPWD, etc.)
NBP_STATION_MAP: dict[str, str] = {
    # NBM NBP bulletins identify stations by 4-letter ICAO (e.g. KAUS, KNYC).
    "KXLOWTNYC":  "KNYC", "KXLOWTCHI":  "KMDW", "KXLOWTMIA":  "KMIA",
    "KXLOWTLAX":  "KLAX", "KXLOWTDEN":  "KDEN", "KXLOWTAUS":  "KAUS",
    "KXLOWTPHIL": "KPHL", "KXLOWTATL":  "KATL", "KXLOWTBOS":  "KBOS",
    "KXLOWTDAL":  "KDFW", "KXLOWTDC":   "KDCA", "KXLOWTHOU":  "KHOU",
    "KXLOWTLV":   "KLAS", "KXLOWTMIN":  "KMSP", "KXLOWTNOLA": "KMSY",
    "KXLOWTOKC":  "KOKC", "KXLOWTPHX":  "KPHX", "KXLOWTSATX": "KSAT",
    "KXLOWTSEA":  "KSEA", "KXLOWTSFO":  "KSFO",
}

# Scan cadence
SCAN_INTERVAL_SEC = 60              # 60s normal; low-temp markets move slowly
FAST_SCAN_INTERVAL_SEC = 15         # pre-dawn (1h before sunrise → 1h after)
LOG_HEARTBEAT_SEC = 600             # emit summary stats every 10 min

# Opportunity filters
MIN_EDGE = 0.20                     # min edge to take a trade
MAX_EDGE = 0.70                     # 0.40 → 0.42 (4/27) → 0.55 (4/28) → 0.45 (4/29) →
                                    # 0.50 (5/6 morning) → 0.70 (5/6 mid-morning, this change).
                                    # Sweep over 8d of resolved candidates with new MP_RANGE
                                    # [0.05, 0.85] in place: cumulative BUY_NO sum_pnl was +27.94
                                    # at MAX_EDGE=0.50, peaks at +28.89 from 0.70 onward (flat
                                    # through 1.00). The high-edge BUY_NO tail above 0.50 has
                                    # 1 freed candidate at edge 0.65-0.70 — that one won. No
                                    # losers surface above 0.50 within the [0.05, 0.85] mp band.
                                    # Per-mp sample is small but no-downside: every cell in the
                                    # MIN x MAX grid above MAX_EDGE=0.50 is monotonically >= 0.50.
                                    # Re-evaluate ~2026-05-20.
MIN_MODEL_PROB = 0.05               # 2026-05-06: 0.15 → 0.05. 5/4-5/6 audit found
                                    # 8,022 candidates blocked / 3 days, ~90% would have
                                    # WON. min_bot was killing its own deep-tail BUY_NO
                                    # channel where the model is most confident (mp 0-15%
                                    # = model says 0-15% chance in bracket; usually right).
                                    # V2 uses MIN_MODEL_PROB=0.03; min_bot now at 0.05
                                    # (slightly more conservative). Re-evaluate ~2026-05-20.
MAX_MODEL_PROB = 0.85               # skip model_prob > 85% (crowded / low payout)
MIN_ORDER_PRICE = 0.05              # don't bet contracts priced < 5¢
MAX_MODEL_PROB_MINUS_MARKET_FLOOR = 0.30  # sanity check on edge magnitude
# Directional consistency thresholds. BUY_NO requires mp ≤ MAX_MP; BUY_YES
# requires mp ≥ MIN_MP. BUY_NO tightened 2026-04-29 night from 0.40 → 0.20
# (V2 deployed the same tighter threshold; min_bot deduped pool n=42 confirmed
# h:h 12:1 with lift +$5.16 over current 0.40 cap). BUY_YES kept at 0.60 —
# only the BUY_NO side was validated this round; symmetric tightening to 0.80
# would need its own backtest. Bot's mp_range_bypass (NBP-CLI consistency)
# does NOT bypass directional consistency — directional and mp_range are
# independent gates.
DIRECTIONAL_BUY_NO_MAX_MP = 0.25    # 2026-05-11 eve: 0.30 → 0.25 REVERT.
                                     # Stack-interaction failure surfaced today:
                                     # same-session H_2_0 2.0→4.5 + DIRECTIONAL
                                     # 0.25→0.30 removed both layers protecting the
                                     # BUY_NO mp 0.25-0.30 + disag 2-4.5°F band.
                                     # KSEA-26MAY11-B50.5 (-$50 MTM) and 2 others
                                     # entered through the gap. Compared MMD GAP
                                     # tighten 0.25→0.20 (commit 847d18e earlier
                                     # today) vs revert here on n=4 today: revert
                                     # strictly better (h:h 3:0 vs 3:1, lift +$52.76
                                     # vs +$47.72 — MMD-tighten kills MIN-B37.5
                                     # winner mp=0.227 that this revert preserves).
                                     # MMD was reverted same commit. V2 audit of the
                                     # 0.25→0.30 change had REJECTED it.
                                     # was 0.40 (2026-04-27) → 0.20 (2026-04-29 night)
                                     # → 0.25 (2026-05-07) → 0.30 (2026-05-10)
                                     # → 0.25 (2026-05-11 eve REVERT)
DIRECTIONAL_BUY_YES_MIN_MP = 0.65   # 2026-05-04 night: was 0.60. BUY_YES T-floor mp 60-65% bucket bled -$34.28 over 7 trades (2W:5L) Apr 25-May 4 — model systematically overconfident at the 0.60-0.65 calibration boundary. 4 of 6 most-recent BUY_YES losses (DC-T46 -$24, OKC-T47 -$4.68, ATL-T53 -$4.56, OKC-T51 -$4.68) were in this bucket. Tightening blocks 7 of 21 T-floor BUY_YES historical trades, sacrificing 1 small winner (ATL-T60 +$0.64) to prevent 6 losses. Net lift +$34.28/10d on small sample. 6:1 helps:hurts. Re-evaluate ~2026-05-18 with fresh data.

# 2026-05-08: BUY_NO_EXTREME_SIGMA gate. Block BUY_NO when bot's own
# inflated sigma is >= 8.0°F AND model_prob < 10%. Mechanism: sigma is
# inflated by PER_SERIES_SIGMA_MULT (KLAX/KPHX/KDEN/KLV) * disagreement
# inflation * NBP staleness. When all three compound, sigma >= 8 marks
# the WORST-known-MAE configuration — yet Gaussian CDF makes mp DROP as
# sigma rises (probability mass spreads thin), so the bot becomes MORE
# confident in BUY_NO precisely when uncertainty is highest. Pair with
# mp<10% to catch the Gaussian-flattening artifact specifically.
# Backtest n=2 (PHX-26MAY05 -$6.70 + DEN-26MAY08 -$1.34, 0W:2L, lift
# +$8.04). Below sample-size playbook bar but mechanism is structurally
# clean: σ inflation exists to FLAG uncertainty but propagates into mp
# calc backwards. Re-evaluate at n>=5 firings.
BUY_NO_EXTREME_SIGMA_THRESHOLD = 8.0
BUY_NO_EXTREME_SIGMA_MAX_MP    = 0.10

# 2026-05-08: KLAX-specific BUY_NO σ gate. Pacific coastal microclimate
# (marine layer, fog) is poorly modeled by HRRR/NBP/NBM, manifesting as
# elevated σ. PER_SERIES_SIGMA_MULT=2.5 captures the historical-MAE tax
# but doesn't gate entries. Backtest 14d on extended pool (settled +
# exited + locked-win + in-bracket, n=71): 4 KLAX BUY_NO B-bracket
# entries, 1 won at σ=1.67 (+$16), 3 lost at σ=2.50/3.00/5.00 (−$70).
# Threshold 2.5°F catches all 3 losers, preserves σ<2.5 winner.
# n=3 below standard sample bar but: robust=+$40.74, helps:hurts=3:0,
# lift=+$69.90, mechanism is structurally clean (LAX known-bad MAE
# AND elevated σ at entry = Pacific-microclimate uncertainty regime
# the bot's Gaussian model doesn't capture). KLAX-only because cross-
# station σ>=2.5 is mixed (KMSP +$65/3W:0L, KDFW +$15/3W:1L), so
# generalization would block winners.
LAX_BUY_NO_HIGH_SIGMA_THRESHOLD = 2.5

# 2026-05-15: BUY_NO_LOW_BRACKET_TRAP. Block BUY_NO on LOW B-bracket in
# two specific configurations identified from V1-min 5/14 losses:
#   DAL-26MAY14-B67.5 -$31.64, DC-26MAY14-B52.5 -$61.59,
#   MIN-26MAY14-B49.5 -$7.24  →  combined -$100.47.
# Mechanism A (CONSENSUS_NEAR_CAP): mu > cap AND disagreement < 2.5°F.
#   All three forecasts (NBP/HRRR/NBM) cluster within ~1°F above the
#   bracket cap. With small buffer and consensus bias, the day's low
#   can drop into bracket.
#   - DAL-5/14: mNBP=69.0 mHRR=67.8 (already inside [67,68]!) mNBM=69.2
#     disg=1.40 → SKIP
#   - DC-5/14:  mNBP=55.0 mHRR=54.1 mNBM=54.7 disg=0.90 → SKIP
# Mechanism B (RM_ABOVE_BRACKET): mu < floor AND running_min >= cap.
#   Entered after the day's low began forming but rm hasn't crossed the
#   bracket from above. Bot is betting low will pass *through* the
#   bracket on its way to forecast.
#   - MIN-5/14: mu=46.1 (HRRR cold outlier) rm=51.1 >= cap=50 → SKIP
# Backtest 5/12-5/14 (n=15 settled BUY_NO trades): catches all 3 5/14
# losers (lift +$100.47), blocks 0 of 9 historical winners — winners
# with mu>cap all had disagreement >= 2.9°F (DEN 4.20, DC-5/13 3.10,
# SFO 2.90, LV 3.30, NYC-5/13 4.20); winners with mu<floor (CHI/MIN-5/12,
# MIN-5/13) all entered before sunrise with rm=None.
BUY_NO_LOW_BRACKET_TRAP_DISAGREE_MAX = 2.5

# 2026-05-01: BUY_YES tail margin gate. Live-pool loser pattern:
# losers had margin median = -0.50°F (μ on wrong side / barely across the
# threshold), winners had margin = +0.90°F. DC-T46 today (μ=47, floor=46.5,
# margin +0.5°F → -$24 stuck) fits the loser pattern exactly. Skip BUY_YES
# tail when |μ - threshold| < 1.0°F into the YES region. Defense-in-depth
# beyond DIRECTIONAL_BUY_YES_MIN_MP=0.60 — that gate misses cases like
# DC-T46 where mp=62% just barely passes.
YES_TAIL_MIN_MARGIN_F = 1.0

# Hard safety gates (HIGH-impact: prevent the patterns that lost money on the
# 04:09 UTC live cycle and that V1/V2 had to fix in production).
_DISAGREEMENT_ENABLED = False       # 2026-05-07: disabled. 30d audit found 9
                                     # unique tickers blocked; 7 resolved via
                                     # final running_min: 6 hurts (would have
                                     # WON), 1 help — 1:6 helps:hurts, lift
                                     # est −$22.98. Pattern: filter overrides
                                     # high-confidence model_prob (all 6 hurt
                                     # cases had mp ≤ 0.20 = model 80%+
                                     # confident NO wins, model was right
                                     # despite multi-source disagreement).
                                     # Predicate preserved for fresh re-enable.
                                     # Re-evaluate ~2026-05-21 if blocked
                                     # ticker outcomes shift.
MAX_DISAGREEMENT_F = 5.0            # skip if HRRR vs NBP / NBP vs NBM disagree > this
MAX_SPREAD_CENTS = 10               # skip if (yes_ask − yes_bid) > 10c on the active side
MAX_MU_VS_RM_DIFF_F = 5.0           # pre-sunrise sanity: skip if forecast μ disagrees with
                                    # observed running_min by more than this — model is wrong
# 2026-05-08: positions with actual_cost below this threshold don't count toward
# MAX_OPEN_PER_EVENT. KXLOW markets often only fill 1 contract ($0.40-$1.50)
# at the maker bid in thin orderbooks; without the exemption, those tiny first
# fills lock the entire (city,date) event slot, blocking legit entries on
# better brackets within the same event for the rest of the day. Backtest
# (14d, n=8 event_cap blocks): 2 tiny-blocker cases would be unblocked, 5
# moderate-blocker cases ($4-7) untouched, 1 full-blocker case ($29.58)
# correlated-bet protection preserved. Threshold $3 mirrors V1's NO_CASCADE
# under-fill exemption philosophy (kept tighter for min_bot's smaller bet sizes).
EVENT_CAP_UNDERFILL_THRESHOLD_USD = 3.0

MAX_OPEN_PER_EVENT = 1              # at most this many *open* positions per event_ticker.
                                    # Lifetime cap (counts against _open_positions, not just
                                    # this cycle). Prior per-cycle version let CHI-26APR25
                                    # accumulate 4 brackets across cycles 2026-04-25.
                                    # Correlated bets — if forecast is wrong, all lose.

# Kelly sizing
MAX_BET_USD = 20.00                 # 2026-05-27: $80 -> $20 per Chris (tail control; also caps add-on growth). $80 8x'd correlated-bust drawdown -> 5/27 -$357 night wiped ~a month. Was $50->$80 on 5/26.
                                    # 2026-05-20: $25 → $50 per Chris (raise after 5/19 net +$49 settle, post-tail_risk + post-hrrr_in_bracket_warm).
                                    # 2026-05-17: $25 → $50 per Chris (rolled back same day).
                                    # 2026-05-15 PM: $60 → $25 per Chris.
                                    # Risk reduction after the 5/14 -$100.47 cluster (DAL/DC/MIN all losers
                                    # at near-$60 cap each). New BUY_NO_LOW_BRACKET_TRAP filter blocks the
                                    # exact replay, but cap reduction limits blast radius on whatever pattern
                                    # gets through next. Re-evaluate after 2 weeks of post-cap data.
                                    # 2026-05-12: $80 → $60 REVERT after NYC-26MAY12-B46.5 stacked $74.57 loss.
                                    # History: $1 (live launch) → $3 (2026-04-26) → ... → $30 (2026-04-29 eve)
                                    # → $45 (2026-05-05) → $60 (2026-05-10) → $80 (2026-05-12) → $60 (2026-05-12 eve)
                                    # → $25 (2026-05-15 PM) → $50 (2026-05-17). Default cap.

# 2026-05-01: BUY_YES entries get a tighter $5 cap (was tail-only originally).
# Asymmetric blast-radius limit. Historical wins on these were ALL ≤ $4.90
# cost (max KSAT-T73 win at $4.90), so the cap doesn't change wins. DC-T46
# (cost $24, -$24 loss) showed the loss potential when uncapped — same
# forecast wrong by 2.5°F = full position loss.
#
# 2026-05-02: extended from BUY_YES TAIL only to ALL BUY_YES (B-bracket
# included). PHIL-26MAY02-B49.5 BUY_YES this morning was a B-bracket inside-
# bracket trade made newly-viable by the bracket-math fix; market liquidity
# happened to keep the actual fill tiny ($0.38) but Kelly wanted ~$30, so
# the cap matters going forward. BUY_YES asymmetry holds across all
# bracket types: small wins (price ≤ 50c → max payout 50c per dollar),
# full-cost losses on forecast misses.
MAX_BET_BUY_YES_USD = 1.00       # 2026-05-10 PM: $5 -> $1 (per Chris). YES held to tiny exploratory bets.
# 2026-05-07: V2-port adverse-addon gate. Block addons if entry_price has
# risen >= 5pp since first fill (action-agnostic; entry_price = what we pay,
# whether BUY_NO or BUY_YES). V2 backtest (commit 4ebfa2b, 2026-05-06):
# n=30, lift +$190, LOO min +$124, 0 negative LOO removals, h:hu 18:12.
# Triggered by min_bot PHX-T66 catastrophe 5/7: 5 addons laddered as fill
# price went 0.42 -> 0.69 (+27pp), now MTM -98% on $28.66.
# 2026-05-07: tightened from 0.05 -> 0.15 after min_bot-specific backtest.
# 0.05 (V2 default): blocked 8 historical addons, 4 settled = 4W/0L = -$4.40
# 0.10: blocked 5, 1W/0L = -$1.81
# 0.15: blocked 3 (all 3 are today's PHX-T66 worst addons), 0W/0L = $0.00
# Min_bot edge is wider than V2 — small adverse moves often still favor the bot
# (SEA-B47.5 5/2: 4 winning addons at +0.07-0.15 rise). 0.15 still catches
# catastrophic moves (PHX-T66 5/7: addons at +0.16, +0.17, +0.27, $28.98 saved).
_FIRST_ENTRY_ADVERSE_ABS_THRESHOLD = 0.15
                                    # → $5 (2026-04-27 PM) → $10 (2026-04-27 evening) → $15
                                    # (2026-04-28) → $20 (2026-04-28 night, paired with bankroll
                                    # add to ~$279) → $30 (2026-04-29 evening, per Chris). Kelly
                                    # @ 25% on 25% edge × 50c price wants ~$35/bet on $279
                                    # bankroll; $30 cap finally lets full Kelly run on the
                                    # highest-conviction trades while still bounding per-position
                                    # blast radius.
KELLY_FRACTION = 0.25
MIN_BET_USD = 0.50
MIN_COST_USD = 1.00                 # cost floor: ceil(MIN_COST_USD / price) bumps `count` so
                                    # every fill deploys ≥ $1. Prior int-rounded Kelly produced
                                    # 96% sub-$1 fills on 2026-04-25/26 (avg $0.45). Capped by
                                    # MAX_BET_USD downstream so the floor can't blow the ceiling.

# ─── Flat sizing for BUY_NO (2026-05-26) ──────────────────────────────────
# Replace Kelly×bankroll variable sizing with a flat per-trade stake for
# BUY_NO. Validated 2026-05-26 on the realized (settle∪exit) pool: the
# model's confidence signals do NOT predict per-contract outcome
# (corr(edge, per-contract pnl)=+0.02), so varying bet size by them injects
# pure noise — and that noise landed on losers: corr(bet $, per-contract
# pnl)=−0.11; big bets (≥$40) 48% WR/−$198 vs small (<$40) 57% WR/−$51.
# Flat sizing alone took the realized 4wk book −$250 → ~−$23; flat $10 +
# THIN_MARGIN → +$36. BUY_YES is unaffected (stays at MAX_BET_BUY_YES_USD).
FLAT_SIZING_NO_ENABLED = True
FLAT_BET_NO_USD = 20.00          # 2026-05-27: $80 -> $20 per Chris (reduce correlated-tail drawdown after 5/27 -$357). Was $10->$80 on 5/26.

# 2026-05-29: gentle σ-haircut on the flat BUY_NO stake (Chris). Flat $20
# ignored forecast reliability — high-σ NO bets (wide-forecast σ-artifacts,
# e.g. μ sitting near the bracket with σ=6) got the same $20 as a tight σ=2.5
# forecast and detonate together on correlated regime-bust nights (5/27 −$357
# across 6 stations; 5/29 LV/DAL/OKC). Shrink the unreliable ones: σ∈[4,5)→×0.5,
# σ≥5→×0.25. Realized-pool (settle∪exit, post-live-chain) before/after: protects
# the worst nights (5/27/28/29), ≈breakeven ex-tail — TAIL-VARIANCE control, not
# alpha (consistent with the flat-sizing finding that size-by-signal adds no
# edge). Graduated cousin of the Kelly path's sigma_shrink=(2.5/σ)², which the
# flat NO path bypasses. Rollback: MIN_NO_SIGMA_HAIRCUT_ENABLED=False.
MIN_NO_SIGMA_HAIRCUT_ENABLED = True
MIN_NO_SIGMA_HAIRCUT_LO_THRESH = 4.0   # σ in [LO_THRESH, HI_THRESH) → LO_MULT
MIN_NO_SIGMA_HAIRCUT_LO_MULT   = 0.50
MIN_NO_SIGMA_HAIRCUT_HI_THRESH = 5.0   # σ ≥ HI_THRESH → HI_MULT
MIN_NO_SIGMA_HAIRCUT_HI_MULT   = 0.25

# 2026-05-10: DISABLED. 14d audit (n=16 settled): 5 helps / 11 hurts (31%
# accuracy, well below random). Net -$93 raw / -$31 adj. Today (May 10)
# stack-aware re-audit on n=10 fires showed 7 would actually buy if gate
# off (other gates pass): 4 confirmed winners + 1 confirmed loser + 2 TBD
# = 80% W:L. The 3 obvious losers (MIA-78.5, PHX-74.5, AUS-65.5) were
# already caught by OBS_CONFIRMED_LOSER, BUY_NO_EXTREME_SIGMA, and
# MU_NEAR_BELOW_BRACKET — ABS_DIST was redundant on those. Mechanism
# was "μ near bracket center means low will land in bracket" but the
# bot.s σ accounts for that and the bracket is only 1°F wide; reality
# came in 1-3°F colder than μ on most blocked tickers. Predicate
# preserved for fresh re-enable. Re-evaluate ~2026-05-31 at n>=20.
ABS_DISTANCE_ENABLED = False
MIN_ABS_DISTANCE_F = 0.5            # BUY_NO only: floor for |mu − bracket_mid|.
                                    # 1.0 → 1.5 (2026-04-27 AM) → reverted to 0.5 (2026-04-27 PM)
                                    # after Kalshi-truth audit on n=15: at 1.5°F we'd block 9
                                    # winners with `dist 0.5–1.5°F` (PHX-B65.5, LAX-B56.5,
                                    # SFO-B51.5, MIA-B69.5, CHI-B47.5 — all BUY_NO with mu *at the
                                    # bracket edge*, not inside). PHIL-B44.5 (0.1°F, mu *inside*
                                    # bracket, the only real loser) is still caught at 0.5°F.
                                    # 2026-04-30 PM: kept floor at 0.5 but added σ-relative
                                    # tightening — see MIN_ABS_DISTANCE_SIGMA_K below.

MIN_ABS_DISTANCE_SIGMA_K = 0.25     # σ-relative tightening on top of the 0.5°F floor.
                                    # Effective threshold = max(0.5, 0.25 × σ). Wide-σ trades
                                    # need proportionally more distance from bracket center.
                                    # Audit 2026-04-30 PM (n=21 post-Apr-29 BUY_NO B-bracket
                                    # entries with blocked_by=null): rule blocks 3 confirmed
                                    # losers (DC-B52.5 σ=2.1 dist=0.5 → cli=52 LOSS;
                                    # MIN-B36.5 σ=3.8 dist=0.5 → cli=36 LOSS; DEN-B38.5 σ=6.0
                                    # dist=0.9 → cli=38 LOSS), keeps every winner including
                                    # OKC-B48.5 σ=2.3 dist=0.6 (cli=51 WIN) and HOU-B68.5
                                    # σ=2.8 dist=0.9 (cli=74 WIN). Net lift swing: +$18.79
                                    # vs current. Zero false positives in available sample.

# ─── THIN_MARGIN gate (2026-05-26) ────────────────────────────────────────
# Skip BUY_NO when the primary forecast μ sits within THIN_MARGIN_MIN_F of
# the NEAR bracket edge (μ−cap if μ>cap, else floor−μ). Distinct from the
# disabled ABS_DIST (|μ−bracket_MID|, 0.5°F floor): this measures distance to
# the EDGE the low must clear, at a wider 2.0°F. Mechanism: a BUY_NO at
# ~$0.59 needs ~59% WR (breakeven WR == price); μ within ~2°F of the edge is
# a boundary coin-flip (~51% WR) that loses on the NO payoff asymmetry.
# Validated 2026-05-26 on the realized (settle∪exit) pool via
# tools/backtest_filters.py --stack live: lift_inc +$222.6, robust_lift_inc
# +$153.6, n_blocked 49; net-negative removed in all 3 time folds. (Required
# the survivorship-bias harness fix to see — the settlements-only pool hid
# 19 of 28 thin-margin losers behind intraday stop-outs.)
THIN_MARGIN_GATE_ENABLED = True
THIN_MARGIN_MIN_F = 2.0

# ─── Per-station σ multiplier (2026-04-29) ────────────────────────────────
# σ multiplier applied to the bot's pre-mult σ in find_opportunities.
# Targets stations where the chosen μ source's empirical MAE (per
# source_audit) is materially wider than the σ the bot has been using.
# Multiplier ≈ (empirical σ) / (typical bot σ at that station).
#   - empirical σ ≈ MAE × √(π/2) ≈ 1.25 × MAE (Gaussian)
# Listed cities all have HRRR MAE > 4°F per source_audit n=336 audit:
#   KLAX  HRRR 5.90°F (n=12)  → bot σ ~2.4 → 2.5× → ~6.0°F (still tight, but
#                                              empirical 7.4°F so close)
#   KPHX  HRRR 5.67°F (n=24)  → bot σ ~3.7 → 2.0× → ~7.4°F (matches empirical)
#   KDEN  HRRR 4.47°F (n=18)  → bot σ ~3.0 → 1.5× → ~4.5°F (matches empirical)
#   KLAS  HRRR 4.48°F (n=24)  → bot σ ~2.5 → 1.5× → ~3.8°F (slightly tight)
# Multiplier applies to all μ sources at that station, but at PHX/LAS where
# NBP's MAE is narrow (1.75°F), the inflated σ is conservatively wider on
# those NBP entries — slightly under-confident but won't cause losses.
# Borderline cities (ATL/NYC/DC/BOS, HRRR MAE 2.6-4.4°F) deferred until
# the full empirical-σ-table backtest is built (see backlog).
PER_SERIES_SIGMA_MULT: dict[str, float] = {
    "KXLOWTLAX": 2.5,  # was 1.5 (2026-04-29 evening); HRRR MAE 5.9°F
    "KXLOWTPHX": 2.0,  # 2026-04-29 evening; HRRR MAE 5.67°F (n=24)
    "KXLOWTDEN": 1.5,  # 2026-04-29 evening; HRRR MAE 4.47°F (n=18)
    "KXLOWTLV":  1.5,  # 2026-04-29 evening; HRRR MAE 4.48°F (n=24)
}
# BUY_NO T-high block list. KLAX hit n=3 wr=0% on this pattern (2026-04-25
# / 04-26 / 04-27 entries, all BUY_NO at thresholds 53–57 with NBP μ 53–56,
# all lost when actual cli came in 58–60). Mechanism: NBP runs 2.5–4°F cool
# on KLAX in this regime, so betting "low won't reach X" is structurally
# the wrong direction at any threshold inside KLAX's 54-60°F observed range.
# B-bracket BUY_NO at LAX is unaffected (n=5 wr=60% there) — only T-high.
#
# 2026-05-01: BUY_NO_T_HIGH_BLOCK_SERIES set REMOVED — block now applies
# globally (any station). Backtest n=82 settlements: 7 historical T-high
# BUY_NO entries → 6 losses prevented, 1 win forfeited (helps:hurts 6:1,
# net lift +$2.93, LOO-robust +$1.74). Mechanism is universal across
# stations: BUY_NO on T-high bets the daily low will drop below floor —
# fights the dominant nighttime-cooling pattern that Kalshi's bracket
# heuristics already account for. Forward audit via candidate logs:
# blocked_by="NO_THIGH" entries can be cross-referenced with subsequent
# CLI lows to verify we are not killing an emerging winner regime.

# Per-city d-1+ primary source override (2026-04-29 source-MAE audit).
# Default is NBP for d-1+ markets. When a city is mapped to "hrrr" here,
# HRRR's daily-min forecast is used as μ instead (with NBP σ if available).
# Evidence: source_audit.py 2026-04-29 across n=336 settled candidates:
#   CHI:  HRRR MAE 0.43°F vs NBP 2.33°F (5x better; n=18, mechanism: HRRR
#         3km grid captures Chicago's lake-effect / urban-heat dynamics
#         better than NBP's stat ensemble)
#   OKC:  HRRR MAE 2.22°F vs NBP 3.50°F (1.6x better; n=24, NBP runs −3.5°F
#         cool-biased on KOKC similar to LAX pattern)
# Other 18 cities have NBP equal-or-better — keep them on NBP.
# ─── Auto-select primary forecast source (2026-05-06) ─────────────────
# A daily cron (tools/auto_select_per_series_primary.py) writes
# data/auto_primary_selection.json with weighted-MAE-best source per
# (station, days_out). Bot reads at startup + on file mtime change.
#
# Lookup priority (highest wins):
#   1. MANUAL_PRIMARY_OVERRIDES_D{0,1} (operator pin in code)
#   2. auto_primary_selection.json (daily cron)
#   3. PER_SERIES_D{0,1}_PRIMARY hardcoded dicts below
#   4. 'hrrr' (d-0) or 'nbp' (d-1) baseline default
#
# JSON staleness: if file age > 2 days OR computed_at > 36h, fall through
# to (3). This protects against silent cron failures.
MANUAL_PRIMARY_OVERRIDES_D0: dict[str, str] = {}  # e.g. {"KXLOWTSEA": "hrrr"}
MANUAL_PRIMARY_OVERRIDES_D1: dict[str, str] = {}

AUTO_PRIMARY_SELECTION_PATH = "auto_primary_selection.json"  # resolved relative to DATA_DIR
AUTO_PRIMARY_MAX_AGE_SEC = 36 * 3600  # 36h since computed_at — beyond that, treat as stale

_auto_primary_cache: dict = {
    "loaded_mtime": 0.0,
    "computed_ts": 0.0,
    "d0": {},   # icao -> source
    "d1": {},
    "valid": False,
}
_auto_primary_lock = threading.Lock()

# Series -> ICAO mapping (used for auto-select JSON which is keyed by ICAO)
_SERIES_TO_ICAO: dict[str, str] = {
    "KXLOWTNYC": "KNYC", "KXLOWTSFO": "KSFO", "KXLOWTSEA": "KSEA",
    "KXLOWTLAX": "KLAX", "KXLOWTMIA": "KMIA", "KXLOWTHOU": "KHOU",
    "KXLOWTPHX": "KPHX", "KXLOWTPHIL": "KPHL", "KXLOWTOKC": "KOKC",
    "KXLOWTBOS": "KBOS", "KXLOWTCHI": "KMDW", "KXLOWTMIN": "KMSP",
    "KXLOWTDEN": "KDEN", "KXLOWTLV":  "KLAS", "KXLOWTATL": "KATL",
    "KXLOWTDC": "KDCA", "KXLOWTDAL": "KDFW", "KXLOWTAUS": "KAUS",
    "KXLOWTSATX": "KSAT", "KXLOWTNOLA": "KMSY",
}


def _maybe_reload_auto_primary() -> None:
    """Reload auto_primary_selection.json if mtime has changed since last load.
    Cheap when file unchanged. Thread-safe."""
    path = os.path.join(DATA_DIR, AUTO_PRIMARY_SELECTION_PATH) if not os.path.isabs(AUTO_PRIMARY_SELECTION_PATH) else AUTO_PRIMARY_SELECTION_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        # No file — invalidate cache so we fall through to hardcoded
        with _auto_primary_lock:
            if _auto_primary_cache["valid"]:
                _auto_primary_cache["valid"] = False
                _auto_primary_cache["d0"] = {}
                _auto_primary_cache["d1"] = {}
        return
    with _auto_primary_lock:
        if mtime <= _auto_primary_cache["loaded_mtime"]:
            return  # unchanged
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        # Bump loaded_mtime to skip retries on the same corrupt file (avoid retry storm)
        with _auto_primary_lock:
            _auto_primary_cache["loaded_mtime"] = mtime
        return
    # Validate computed_at age
    try:
        computed_ts = datetime.fromisoformat(data.get("computed_at", "")).timestamp()
    except (ValueError, TypeError):
        with _auto_primary_lock:
            _auto_primary_cache["loaded_mtime"] = mtime
        return
    if (time.time() - computed_ts) > AUTO_PRIMARY_MAX_AGE_SEC:
        # Stale — invalidate
        with _auto_primary_lock:
            _auto_primary_cache["valid"] = False
            _auto_primary_cache["d0"] = {}
            _auto_primary_cache["d1"] = {}
            _auto_primary_cache["loaded_mtime"] = mtime
            _auto_primary_cache["computed_ts"] = computed_ts
        return
    # Build the lookup dicts (icao -> source). Falsy values are skipped.
    d0 = {}
    d1 = {}
    for icao, sel in (data.get("selections") or {}).items():
        if isinstance(sel, dict):
            v0 = sel.get("d0_primary")
            if v0:
                d0[icao] = v0
            v1 = sel.get("d1_primary")
            if v1:
                d1[icao] = v1
    with _auto_primary_lock:
        _auto_primary_cache["loaded_mtime"] = mtime
        _auto_primary_cache["computed_ts"] = computed_ts
        _auto_primary_cache["d0"] = d0
        _auto_primary_cache["d1"] = d1
        _auto_primary_cache["valid"] = True


def get_d0_primary(series: str) -> Optional[str]:
    """Return the d-0 primary source for `series` (e.g. 'KXLOWTSEA'), or
    None if no override applies (caller falls back to default 'hrrr').

    Resolution order: manual override → auto-select → hardcoded → None.
    """
    # 1) Manual override (operator pin)
    v = MANUAL_PRIMARY_OVERRIDES_D0.get(series)
    if v:
        return v
    # 2) Auto-select (daily cron)
    _maybe_reload_auto_primary()
    icao = _SERIES_TO_ICAO.get(series)
    with _auto_primary_lock:
        if _auto_primary_cache["valid"] and icao:
            v = _auto_primary_cache["d0"].get(icao)
            if v:
                return v
    # 3) Hardcoded fallback
    return PER_SERIES_D0_PRIMARY.get(series)


def get_d1_primary(series: str) -> Optional[str]:
    """Same as get_d0_primary but for d-1+. None means caller uses default 'nbp'."""
    v = MANUAL_PRIMARY_OVERRIDES_D1.get(series)
    if v:
        return v
    _maybe_reload_auto_primary()
    icao = _SERIES_TO_ICAO.get(series)
    with _auto_primary_lock:
        if _auto_primary_cache["valid"] and icao:
            v = _auto_primary_cache["d1"].get(icao)
            if v:
                return v
    return PER_SERIES_D1_PRIMARY.get(series)


# ─── Hardcoded fallback dicts (still used when auto-select unavailable) ───

# d-0 still uses HRRR universally (HRRR beats NBP overall on d-0 by 1.22°F
# MAE) so this only affects d-1+ markets at these specific cities.
PER_SERIES_D1_PRIMARY: dict[str, str] = {
    "KXLOWTCHI":  "hrrr",
    "KXLOWTOKC":  "hrrr",
    # 2026-05-04 audit: gulf-coast cities have NBP forecast warm-biased
    # 4-5°F at d-1 vs actual rm. HRRR is 2-7× more accurate on these
    # cells (n=4 each over 14d, MAE gaps far outside noise).
    #   | City | NBP d-1 MAE | HRRR d-1 MAE | gap     |
    #   |------|-------------|--------------|---------|
    #   | HOU  | 4.35°F      | 1.58°F       | +2.77°F |
    #   | NOLA | 4.05°F      | 0.55°F       | +3.50°F |
    #   | SATX | 5.10°F      | 1.85°F       | +3.25°F |
    # See `/tmp/inv2_audit_v2.py` on VPS for re-running.
    "KXLOWTHOU":  "hrrr",
    "KXLOWTNOLA": "hrrr",
    "KXLOWTSATX": "hrrr",
    # 2026-05-05 broader audit — KMIA flipped from "hrrr" to "nbm" + KDEN/KMIN
    # added. KMIA is a same-day re-audit refinement: morning's HRRR-PRIMARY was
    # right call given NBP's catastrophic warm bias, but the broader 14d audit
    # showed NBM is even better (MAE 1.15 vs HRRR 1.70 vs NBP 1.74 at d-1, n=4):
    #   KMIA d-1 errs (NBP, HRRR, NBM):
    #     4-30: 0.0,  -3.0, -0.8 → NBM and NBP roughly equal
    #     5-01: +0.4, -3.7, -3.6 → NBP best
    #     5-02: +1.8, -3.0, -3.0 → NBP best
    #     5-03: +5.4, +0.7, +0.7 → HRRR/NBM tied
    #     5-04: +3.0, +0.1, +0.1 → HRRR/NBM tied
    #   NBM matches HRRR's accuracy on warm days + NBP's cold-direction
    #   resilience on cool days.
    "KXLOWTMIA":  "nbm",  # was "hrrr" (deployed earlier 2026-05-05 commit
                          # 6014618); flipped to "nbm" same day after
                          # broader audit. NBM is the structurally-better
                          # source on KMIA d-1 in both directions.
    # 2026-05-05 audit additions (n=4-5 per cell):
    "KXLOWTDEN":  "nbm",  # NBP MAE=2.80 vs HRRR=1.40 vs NBM=1.05 (n=4).
                          # NBP runs −2.4°F cold at d-1 — biggest cell-MAE
                          # gap in the audit (+1.75°F NBM vs current NBP).
    "KXLOWTMIN":  "hrrr", # NBP MAE=4.24 vs HRRR=3.68 vs NBM=4.50 (n=5).
                          # All sources struggle (3.7-4.5°F MAE) but HRRR
                          # is best. Marginal flip; KMIN d-1 trades are
                          # high-noise regardless. Re-audit when NAM-MOS
                          # gets ported.
}

USE_D1_CONSENSUS = True  # 2026-05-29 (Chris): d-1 mu = median-consensus of HRRR/NBM/NBP
# instead of a single per-city source. Source-accuracy test n=248 station-days, both halves:
# NBM warm-biased +1.64F, HRRR best (-0.41F), median-consensus MAE 1.77 < every single source
# (1.87-2.08); replaces the fragile per-city auto-select that wrongly assigned CHI->NBM (warm,
# busted 05-29). Audited HRRR-primary cities (CHI/OKC/HOU/NOLA/SAT, hardcoded HRRR in
# PER_SERIES_D1_PRIMARY) KEEP HRRR (NBP/NBM catastrophically warm there; consensus would re-warm).
# Forecast-quality upgrade; PnL expected marginal -> forward-monitor. Rollback: USE_D1_CONSENSUS=False.


def _d1_consensus_mu(series, hrrr, nbm, nbp):
    """d-1 mu: median-consensus of available sources, EXCEPT hardcoded HRRR-primary
    cities keep HRRR. Returns (mu, sigma, source) or None (caller falls back to single source)."""
    import statistics as _st
    sig = nbp["sigma"] if nbp else 2.5
    if PER_SERIES_D1_PRIMARY.get(series) == "hrrr" and hrrr is not None:
        return float(hrrr), sig, "hrrr_d1_override"
    vals = []
    if hrrr is not None:
        vals.append(float(hrrr))
    if nbm is not None:
        vals.append(float(nbm))
    if nbp:
        vals.append(float(nbp["mu"]))
    if len(vals) >= 2:
        return _st.median(vals), sig, "consensus_d1"
    return None


# Per-city d-0 source override. d-0 default is HRRR (freshest nowcast), but
# 30-day candidate-log audit (2026-04-29) shows three stations where NBP is
# materially more accurate AND less biased than HRRR on d-0. Switching d-0
# source to NBP for these cities cuts MAE by 30-65% and removes a structural
# HRRR-cool bias that was driving B-bracket BUY_NO losses (e.g. today's
# NYC-B51.5 settled loss: HRRR μ=49.2 vs cli=52, 2.8°F gap).
#
# Source-MAE evidence (n=46-58k cycle samples per city × source):
#   | City | NBP d-0 | HRRR d-0 | gap   | bias    | bot.mu MAE |
#   |------|---------|----------|-------|---------|------------|
#   | NYC  | 1.54°F  | 3.30°F   | -1.76 | -3.30°F | 3.27°F     |
#   | DC   | 1.83°F  | 2.61°F   | -0.78 | -1.65°F | 2.60°F     |
#   | BOS  | 1.17°F  | 2.57°F   | -1.40 | -2.52°F | 2.54°F     |
#
# Other cities NOT included (NBP not better enough on d-0):
#   PHIL: NBP 2.05 vs HRRR 2.35 (-0.30°F, marginal)
#   ATL:  NBP 1.61 vs HRRR 1.48 (HRRR slightly better — leave on HRRR)
#
# σ from NBP is subject to staleness inflation (already wired for "nbp" and
# "hrrr_d1_override"; "nbp_d0_override" added to that tuple below). NBP
# cycles every 6h so d-0 NBP can be 4-6h stale on late-evening entries.
PER_SERIES_D0_PRIMARY: dict[str, str] = {
    # 2026-05-04 re-audit (14d, n=5-6 per cell, joined min_bot candidate
    # log with obs.sqlite running_min — see `/tmp/inv2_audit_v2.py`):
    # NBP-vs-HRRR balance has shifted from the 2026-04-29/05-01 deploys.
    # Removed 4 cells where HRRR is now materially better at d-0:
    #   KXLOWTNYC: NBP MAE=1.97 vs HRRR=0.88 (gap +1.09°F)
    #   KXLOWTDC:  NBP MAE=2.03 vs HRRR=1.66 (gap +0.37°F, marginal)
    #   KXLOWTMIA: NBP MAE=1.92 vs HRRR=0.53 (gap +1.39°F — biggest flip)
    #   KXLOWTPHX: NBP MAE=2.40 vs HRRR=1.52 (gap +0.88°F)
    # Whatever drove the original NBP advantage on these cities at the
    # April-29 audit (n~31k cycle samples) is no longer present in the
    # 14d settled-actuals window. These cells default to HRRR (the d-0
    # default) until a future re-audit shows otherwise.
    #
    # 2026-04-29 cohort (still STILL nbp-better at d-0 per 14d audit):
    "KXLOWTBOS":  "nbp",  # NBP MAE=0.87 vs HRRR=1.86 (gap −0.99°F)
    # 2026-05-01 cohort (still NBP-better at d-0):
    "KXLOWTLV":   "nbp",  # NBP MAE=0.88 vs HRRR=1.46 (gap −0.58°F) [Vegas]
    "KXLOWTLAX":  "nbp",  # NBP MAE=1.37 vs HRRR=1.90 (gap −0.53°F)
    "KXLOWTPHIL": "nbp",  # NBP MAE=0.97 vs HRRR=1.76 (gap −0.79°F)
    "KXLOWTSEA":  "nbp",  # NBP MAE=1.23 vs HRRR=1.57 (gap −0.34°F, marginal)
    "KXLOWTSFO":  "nbp",  # NBP MAE=0.93 vs HRRR=1.54 (gap −0.61°F)
    # 2026-05-05 audit additions (n=6 per cell, broader 14d audit catching
    # cells that the May-04 narrow audit missed):
    "KXLOWTATL":  "nbp",  # NBP MAE=1.13 vs HRRR=2.03 (gap +0.90°F, n=6).
                          # HRRR runs −1.70°F cold-biased on KATL d-0; NBP
                          # +0.67°F warm but tighter overall. Trigger:
                          # KATL-26MAY05-B54.5 BUY_NO entry today on HRRR
                          # mu=51.4 vs actual rm=55.94 (4°F off).
    "KXLOWTDAL":  "nbp",  # NBP MAE=0.53 vs HRRR=1.20 (gap +0.67°F, n=6).
                          # NBP excellent on KDAL d-0 (-0.27°F bias).
    # Re-audit ~2026-05-18 (≥21d cumulative window).
}

# ─── Hard ceilings that gate execute_opportunity before placing the order
MAX_NEW_POSITIONS_PER_CYCLE = 3     # cycle scope (60s scan)
DAILY_EXPOSURE_CAP_USD = 10000.00   # day scope (UTC midnight); $4 → $15 → $30 → $60 (2026-04-27)
                                    # → $120 (2026-04-28 night) → effectively unlimited (2026-04-29
                                    # evening, per Chris). With ~$300 bankroll (BANKROLL_REF_USD), MAX_BET_USD=$30,
                                    # MAX_NEW_POSITIONS_PER_CYCLE=3, and BANKROLL_FLOOR_USD=$5, the
                                    # bankroll itself becomes the binding constraint (bot refuses to
                                    # place orders once balance < $5). The $10,000 value is a sentinel
                                    # that will never be hit at current scale; if bankroll grows past
                                    # $5k, revisit.

# ─── Kelly anchor (V2 port): bankroll, not MAX_BET_USD ──────────────────
# Pre-fix: bet_usd = kelly * MAX_BET_USD (anchored to the cap, sized as if
# bankroll = $5). Result: every trade hit the $1 floor, $5 cap unused.
# Fix: bet_usd = kelly * bankroll, capped at MAX_BET_USD. With $21 bankroll,
# 25% Kelly fraction, 25% edge, 50c price → bet_usd $2.62 vs old $0.625.
BANKROLL_REFRESH_SEC = 60          # cache TTL — refresh ~once per scan

# ─── _obs_confirmed_alive (V2 port: rm has decisively settled the bracket) ─
# When running_min has unambiguously crossed into "our side is decided"
# territory, bypass forecast-based gates and boost Kelly. Mirror of V2's
# _obs_confirmed_dead for max-bot. Fires only on directionally-correct setups
# (BUY_NO when rm went well below bracket; BUY_YES when rm hit YES territory
# post-sunrise or with adequate buffer).
OBS_ALIVE_BUFFER_F = 3.0            # rm must be this many °F outside bracket to fire
OBS_ALIVE_MIN_EDGE = 0.05           # bypass-mode edge floor (vs MIN_EDGE for normal entries)
SIGNAL_KELLY_MULT = 1.5             # Kelly boost when obs confirms (matches V2's recent retune)

# ─── F2A asymmetry gate (V2 port, BUY_NO only) ────────────────────────────
# Three sub-checks on BUY_NO entries. Bypassed when _obs_confirmed_alive.
# V2 backtest (original): tightening these bands swung era P&L +$30 → +$74.
#
# 2026-05-10: F2A_PROB_LO 0.05 → 0.0 (sub-rule A effectively off). Stack-aware
# audit + sub-rule split on 12d showed sub-rule A blocked 16 unique trades
# (other gates passed) with W:L = 15:1 (94% wr), net +$3.36/c (~+$168/12d
# at $45 bet), LOO-1 robust +$2.22/c, helps:hurts 7:0 every day. n=16 is
# below the playbook n≥20 bar, but binomial p<0.001 and there's a direct
# V2 precedent: V2 max bot ran the IDENTICAL sub-rule audit on 2026-04-30
# (commit 84c7878, 13d pool n=248, narrowed F2A to sigma-only), found
# "prob (mp not in [0.05, 0.30)): 9 sole, 9W:0L, +$42.53 freed" and
# removed all prob branches. V2 has been running narrowed F2A for 11+
# days with no revert. Min_bot was carrying un-narrowed code by accident
# of the port-back never happening. Mechanism: bot's mp<5% on BUY_NO is
# the deep-conviction sweet spot (wide μ-vs-bracket gap on cold tails);
# the "price-asymmetry trap" framing assumes mp is well-calibrated at
# extremes — empirically it's conservative, so true WR >> mp. Sub-rule
# B (mp≥0.30) is dead code on min_bot — DIRECTIONAL_BUY_NO at mp>0.30
# fires first. Sub-rule C (σ<1.5°F) kept: only 6 unique kills, 4W:2L,
# LOO-1 −$0.22, marginal — same call V2 made on the same evidence.
F2A_PROB_LO = 0.0                   # rule A disabled 2026-05-10; mp<0 never true
F2A_PROB_HI = 0.30                  # mp ≥ this is calibration cliff (dead code: DIRECTIONAL_BUY_NO catches mp>0.30 first)
F2A_SIGMA_MIN = 1.5                 # sigma < this is over-confident model (kept; LOO-1 borderline, V2 also retained)

# ─── σ-aware Kelly sizing (2026-04-30) ────────────────────────────────────
# Quadratic shrink as σ grows above SIGMA_REF_F: at σ=2.5 (typical NBP), no
# shrink. At σ=4.0, bet is 39% of base. At σ=6.8, bet is 13.5%. Recognizes
# that same-edge-with-wider-σ is a fundamentally weaker signal that should
# size down. Triggered by AUS-26MAY01-T56 (σ=5.7) where bot bet $29.70 on a
# d-1 BUY_YES tail with effective coin-flip confidence.
#
# 2026-04-30 PM: SIGMA_MAX_F entry cap REMOVED. Earlier ship was a reactive
# fix on n=1 (AUS-T56) that turned out to mostly forfeit edge rather than
# prevent harm. The disaster path is now fully closed by:
#   1. σ-aware sizing — wide-σ bets shrink to ~$5
#   2. Hard-stop skip on rm-None — d-1 / early d-0 protected
#   3. Hard-stop skip on BUY_YES T-high — held to settlement
#   4. Partial-fill exit fix — no orphan contracts
# Without these, the cap was load-bearing. With them, the cap was blocking
# ~1 candidate/day (PHX σ=6.8) for no demonstrated harm-prevention. n=1
# winner (DEN σ=6.0) was being forfeited. Removed to gather data; can
# re-add if running data shows σ ≥ 6 zone is genuinely -EV.
SIGMA_REF_F = 2.5                   # reference σ for full-Kelly sizing
# F2A_DIST_MIN: V2 uses 0.5°F from NBM. NOT ported — min-bot audit (n=15) found
# `mu at bracket edge` is the BUY_NO winner pattern (cli flips OUTSIDE the bracket
# from there 60-100% of the time). MIN_ABS_DISTANCE_F (mu vs bracket MID, 0.5°F)
# already catches the dangerous "mu near bracket center / strictly inside" cases.

# ─── MSG multi-source consensus (V2 port, BUY_NO only) ────────────────────
# Count how many of {NBP, HRRR, NBM} forecasts predict YES wins. Block if
# consensus is too strong against us. Per-city tiers: WORST cities require
# unanimity (no source predicting YES). Bypassed when _obs_confirmed_alive.
# 2026-05-10: DISABLED. 14d audit (n=9 settled): 4 helps : 5 hurts (44%
# accuracy, below random). Net +$26 raw / +$9 adj — in noise band. All
# fires concentrated in WORST tier; default tier (max_consensus=2) never
# fired in 14 days. Per-city detail: NYC 2:0 (+$59), PHIL 1:0 (+$30),
# DEN 1:1 (+$17) carried by NYC; NOLA 0:3 (-$46) and SEA 0:1 (-$33)
# completely dragged the filter. Top would-win blocked: NOLA-MAY07-B73.5
# mp=10% edge=29% ($19 missed), SEA-MAY09-B49.5 mp=14% edge=39% ($33
# missed). These are exactly the high-conviction BUY_NO setups MSG was
# supposed to validate; cross-source consensus was anti-predictive on
# them. Predicate preserved for fresh re-enable. Re-evaluate ~2026-05-31
# at n>=20 firings.
MSG_ENABLED = False
MSG_MAX_CONSENSUS_DEFAULT = 2       # block if > this many sources predict YES
MSG_MAX_CONSENSUS_WORST = 0         # WORST cities: any source predicting YES blocks
MSG_WORST_CITIES = {                # cities with historical poor MIN calibration (mirror V2's WORST_7)
    "KXLOWTNYC", "KXLOWTSEA", "KXLOWTPHIL",
    "KXLOWTLV", "KXLOWTNOLA", "KXLOWTDEN",
}
MSG_MARGIN_F = 3.0                  # outlier source > this many °F into YES territory blocks

# ─── SKIP_MODEL_MARKET_DISAGREE (V2 port 2026-05-04, both sides) ──────────
# Block when model and market disagree strongly. V2 BUY_NO original (+$223
# lift / 15L:5W on n=253): when model says YES is moderately likely
# (mp ≥ MP_FLOOR) AND market is paying a lot more for NO than the model
# justifies (yes_bid_frac − mp ≥ GAP_MIN), the market has information the
# model doesn't — the apparent edge is illusory. Mirrored for BUY_YES on
# lows: when mp_no ≥ MP_FLOOR AND no_bid_frac − mp_no ≥ GAP_MIN, market
# overpays NO vs model → BUY_YES likely loses. min_bot LIVE pool
# (n=24, 4/29-5/02): catches DC-T46 BUY_YES (-$24, biggest live loss),
# 1:0 helps:hurts. Bypassed by caller when _obs_confirmed_alive.
#
# 2026-05-11 eve REVERT: GAP_MIN 0.20 → 0.25. Today's earlier tighten
# (commit 847d18e) targeted the same SEA-class entry that DIRECTIONAL
# 0.30→0.25 revert handles cleaner. Comparison on today's n=4 at-risk
# entries: DIRECTIONAL revert h:h 3:0 lift +$52.76 vs MMD tighten h:h
# 3:1 lift +$47.72 (MMD kills MIN-B37.5 winner mp=0.227 that DIRECTIONAL
# skips). Both reverts together restore the pre-2026-05-10 chain on this
# band.
SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR = 0.22
SKIP_MODEL_MARKET_DISAGREE_GAP_MIN = 0.25

# ─── SKIP_MU_NEAR_FLOOR + SKIP_MU_NEAR_BELOW_BRACKET (V1 mirror) ──────────
# 2026-05-06 night port from V1 SKIP_MU_NEAR_BELOW_BRACKET (commit 39670d1)
# + companion tail_high mirror designed for min-temp markets.
#
# Two filters under one constant block + helper:
#
# (A) MU_NEAR_FLOOR_TAIL_HIGH — block BUY_YES on tail_high (T-floor)
#     when blended μ sits 0..2.0F above floor. tail_high BUY_YES bets
#     "min temp ≥ floor" (warm low day). When μ is just above floor,
#     the cold tail of the Gaussian has meaningful probability of
#     dipping actual below floor → BUY_YES loses.
#
#     Audit (5/4-5/6, n=44 with Kalshi truth, BUY_YES action only):
#       - tail_high BUY_YES with μ in (0, 2]F above floor:  6L : 0W
#       - 100% loser-only catch on this window
#       - mu values: +0.5, +1.1, +1.3, +1.4, +1.5, +1.5
#       - Tickers: TDC-T46, TOKC-T47, TATL-T53, TAUS-T56, TOKC-T51, TMIN-T38
#       - 0 tail_high BUY_YES winners exist in the 7d pool — symmetric to
#         V1's "no winners with mu just below mid" finding for B-bracket.
#
# (B) MU_NEAR_BELOW_BRACKET — same as V1's filter but applied to min_bot
#     B-bracket BUY_NO entries. Block when 0 < (mid - μ) ≤ 2.0F.
#
#     Audit (5/4-5/6, n=44 with Kalshi truth, B-bracket BUY_NO):
#       - μ in (0, 2]F below mid: 6L : 3W (2:1)
#       - threshold sweep 1.0/1.5/2.0/2.5/3.0 — 2.0 is optimal
#         (1.5 misses 1 loser at gap=1.9; 2.5 dilutes by adding 2L:2W)
#
# Both filters use threshold 2.0F to match V1's deployed
# MU_NEAR_BELOW_BRACKET_MAX. Combined estimated lift: $90-230/wk against
# current min_bot bleed of $748/wk.
MU_NEAR_FLOOR_TAIL_HIGH_MAX_F = 2.0  # gap = mu - floor; skip BUY_YES if 0 < gap <= this
MU_NEAR_BELOW_BRACKET_MAX_F = 2.0    # gap = mid - mu; skip BUY_NO if 0 < gap <= this

# ─── Hard stop on existing positions (V2 port, mid-cycle exit) ────────────
# 2026-05-04: DISABLED via sentinel values. Pattern mirrors V1's
# SESSION_DRAWDOWN_LIMIT = -9999 disable from 2026-05-03.
#
# Trigger: KXLOWTMIA-26MAY04-B71.5 BUY_NO hard-stopped at $0.04 with rm=71.6
# (exit_reason=hard_stop, pnl=-$8.91). Per Chris's call, the MTM-based hard
# stop has been firing on min-temp BUY_NO positions where rm enters the
# bracket from above and market price collapses, but the trade can still win
# if rm continues to drop BELOW floor (only path for low-temp bracket BUY_NO
# to win once obs touches the bracket). The hard stop locks in losses on
# trades that the obs evolution can still rescue.
#
# Behavior with sentinels: the comparison `loss_pct >= 999.0` is impossible
# (loss_pct is bounded by entry_price → 1.0 max), so the hard-stop branch
# at line ~4404 is dead code without removing it. _check_obs_confirmed_loser
# and _check_position_obs_winning still fire — those use rm vs floor/cap, not
# MTM. Trailing TP / settlement / time-based exits unchanged.
#
# Re-enable: revert both to their prior values (0.80 / 0.70). Don't try to
# tune mid-range — full disable is the simplest test of the hypothesis that
# settlement-rides outperform MTM-cut over a 14-day forward audit.
HARD_STOP_BRACKET_LOSS_PCT = 999.0  # disabled (was 0.80) — sentinel
HARD_STOP_TAIL_LOSS_PCT = 999.0     # disabled (was 0.70) — sentinel

# 2026-05-04: very-loose value-dead market stop. Hard-stop is disabled, but
# a position trading at ≤1c is essentially worthless (market is ~99% certain
# the other side wins). This is the final safety net so deep-out-of-money
# positions don't tie up capital indefinitely. Obs-winner override fires
# FIRST and blocks this exit on positions the obs still confirms — so genuine
# recovery cases (e.g. min-temp BUY_NO with rm < floor − buffer) hold even
# when bid is 1c.
MARKET_STOP_BID_CEIL_C = 1          # exit if current bid ≤ 1¢ AND no obs-winner override

# ─── PACED 10am liquidation + keep confirmed winners (2026-05-26) ──────────
# TIME_EXIT_10AM exists to dodge the overnight collapse: a min-temp BUY_NO can
# be winning at 10am (low still above the bracket) yet get re-tested lower by an
# evening front, fall into the bracket, and settle 0 -- so we sell AT-RISK
# positions in the morning. Two bugs in HOW it sold:
#  (1) Dumped the FULL position at the bid each cycle, chasing the bid down as
#      its own selling drained the thin KXLOWT book. 5/25 KBOS (low 53.6,
#      bracket [52,53], NO winning but at-risk) walked 87->33c, bulk at 33c,
#      -$6.38 -- though the bid HELD 86c all afternoon. KSEA same (60->25c,
#      -$18.93). ~85-96% self-inflicted impact, not real re-pricing.
#  (2) Also dumped CONFIRMED winners (low already below floor -> can't lose,
#      settles 100c) at the ~99c bid, giving up the last cent + sell risk.
# Fix: (A) keep confirmed winners (skip 10am sell when obs-winner check passes;
# low only falls so it can't collapse) -- hold to settlement. (B) Everything
# else: PACE the sell -- offer PACED_EXIT_CLIP_C contracts/cycle at the bid so
# we don't outrun the book; clears in ~10-15 min at the fair ~86c, not the 33c
# bottom. Est. 5/25: KBOS +$21, KSEA +$13; clean 99c winners unchanged.
# Rollback: ENABLE_PACED_EXIT = False (byte-identical full dump).
ENABLE_PACED_EXIT  = True
PACED_EXIT_CLIP_C  = 100000  # 2026-05-27: dribble DISABLED -> full-size dump. The paced clip self-crushed KOKC 62->29c live (5/27); pacing doesn't beat the thin-book exit crush (structural). Hold-confirmed-winner (A) retained via ENABLE_PACED_EXIT=True. Effectively-unlimited clip => _execute_exit sells full size like the long-standing baseline. Pending structural exit redesign.

# ─── ANTI-CHASE floor on TIME_EXIT_10AM (2026-06-01) ────────────────────
# 2026-06-01: structural fix to the thin-book exit walk-down. Even with
# PACED_EXIT_CLIP_C=100000 (full-size dump), the bot re-reads the (lower)
# bid each cycle and chases it down on thin books. NOLA-26MAY28 walked
# 89→33 in 13 cycles after 50 min of stable 89-90c bid (no new info =
# self-crush). Same OKC-5/27 pattern, just slower.
#
# Fix: snapshot the uncrushed arrival bid as pos['_exit_ref_bid_c'] on
# first fire; refuse to sell when current bid < ref - ANTICHASE_EXIT_TOL_C.
# Lets self-crushed bids recover; genuine declines still caught by
# market_stop (≤1c) and shadow_obs_loser (≤25c + obs-confirmed).
#
# Different from 5/26 ship (ee203b0, reverted): that combined floor with
# PACED dribble — the dribble was the OKC self-crush cause. This adds the
# floor on top of the (kept) full-size dump. No re-introduction of dribble.
# Rollback: ENABLE_ANTICHASE_TIME_EXIT = False.
ENABLE_ANTICHASE_TIME_EXIT = True
ANTICHASE_EXIT_TOL_C = 8  # cents below ref bid where we stop chasing

# ─── SHADOW exit activation gate (2026-05-13 ship) ────────────────────────
# Graduates _check_position_obs_confirmed_loser_for_exit from logging-only
# (Stage 1 since 2026-05-04) to actual exit, scoped to BUY_NO B-bracket
# only (the case the audit covers — all 11 historical fires were BUY_NO
# B-brackets) and conditional on bid concurrence.
#
# Mechanism: SHADOW alone fires on rm in [floor+1, cap] for B-brackets, but
# at 1°F-wide brackets the rm=floor+1=cap edge is fundamentally ambiguous
# (CLI rounding can land actual low above or in bracket). SEA-26MAY11
# (rm=51.0, lost) and DC-26MAY11 (rm=51.08, won +$14.52) had ~same rm and
# opposite outcomes. Adding `bid <= SHADOW_EXIT_MAX_BID_C` requires the
# market to also concur the position is doomed — concurrence of obs-loser
# AND market-loser is a much stronger joint signal than either alone.
#
# Audit: n=11 SHADOW fires over 8d (2026-05-04 → 2026-05-12), all BUY_NO
# B-bracket. At bid<=25c:
#   - 6 activations, all true positives
#   - $37.55 saved (TP exits at 21-25c bid vs eventual 1c market_stop)
#   - 0 false positives (all 4 historical FPs had bid 36-59c, gated out
#     including the DC-26MAY11 +$27.28 swing)
# Threshold sweep: clean plateau bid in {25,30,35}c (no fires in 26-35c
# gap); FPs slip in at 40c+. n is sub-bar (playbook n>=20) but mechanism is
# articulable and gate is strictly tighter than current logging-only
# behavior — same precedent override as COLD_SOURCE_OUTLIER (n=1).
#
# Other action/bracket combos (BUY_YES, T-cap, T-floor) stay Stage 1
# log-only — no backtest evidence to ship Stage 2 there yet.
#
# Reversible: set to 0 to disable activation (returns to log-only Stage 1).
# Audit script: /tmp/shadow_gate_design.py
SHADOW_EXIT_MAX_BID_C = 25

# ─── NARROW_MARGIN_TP — take-profit on edge-of-bracket positions ───────────
# Companion to SHADOW exit Stage 2. SHADOW catches losers when rm enters
# the bracket; NMTP catches the EARLIER risk window when rm is just above
# the cap (still nominally a winner, but late-day radiative cooling can
# push rm into the bracket and crash the bid before SHADOW can fire).
#
# Mechanism: when rm is within MARGIN_F of cap (at-risk zone for late-day
# cooling on radiative-cooling-prone stations) AND we're in profit (bid
# in [MIN_BID_C, MAX_BID_C], MTM ≥ MIN_MTM), take profit at current bid.
#
# The station whitelist is the key discriminator. Cross-validation:
# the 4 whitelist stations have the worst BUY_NO B-bracket WR over 14d
# (KSEA 20%, KLAX 0%, KMDW 50%, KMIA 60%), meaning they're empirically
# losing positions in this regime. Climate-physical: KMIA/KLAX/KSEA
# Pacific/Mediterranean coastal with calm-clear-night radiative cooling;
# KMDW has lake-effect inversion potential. Continental humid stations
# (KATL 50%, KHOU 100%, KMSP 100%, KDFW 80%) excluded — NMTP would net-
# hurt there by selling their reliable BUY_NO B-bracket winners.
#
# Audit (14d, 2026-04-29 → 2026-05-12, n=10 fires under combo gate):
#   Helps: 7  Hurts: 3  Ratio: 7:3 = 2.33:1  (PASSES playbook 2:1 bar)
#   Net lift: $+176.34 / 14d ≈ $88/wk  (PASSES $30/wk bar)
#   LOO-1 (drop 2026-05-12): $+123.86  (PASSES $15 bar)
#   LOO-2 (drop 2 best days): $+71.80  (still positive)
#   LOO-station (drop any 1 station): $+94.20 to $+177.44  (all positive)
# Catches today's MIA bust (+$53.58 swing vs SHADOW Stage 2's $3.96 save)
# and SEA-MAY11 bust (+$52.06 swing).
#
# n=10 sub-bar (playbook ≥20) but mechanism articulable + same precedent
# override as COLD_SOURCE_OUTLIER (n=1) and HCDT (n=3) shipped earlier today.
#
# Reversible: NARROW_MARGIN_TP_ENABLED = False to disable.
# Audit script: /tmp/nmtp_robustness.py
NARROW_MARGIN_TP_ENABLED      = True
NARROW_MARGIN_TP_MARGIN_F     = 1.5      # rm within this many °F of cap
NARROW_MARGIN_TP_MIN_BID_C    = 65       # bid floor (real profit to capture)
NARROW_MARGIN_TP_MAX_BID_C    = 80       # bid ceiling (avoid extreme-MTM hurters)
NARROW_MARGIN_TP_MIN_MTM      = 0.20     # MTM floor
NARROW_MARGIN_TP_STATIONS     = frozenset(['KLAX', 'KMDW', 'KMIA', 'KSEA'])

# ─── TAKE_PROFIT_15 — late-morning take-profit on BUY_NO positions ────────
# 2026-05-20: Lock in BUY_NO gains when MTM ≥ 15% of cost AND local time
# ≥ 10:30 LST. Mechanism: by 10:30am LST most morning lows have set;
# positions with MTM ≥ 15% past that = market has converged toward our side.
# Lock it in before evening volatility (canonical archetype is SATX-
# 26MAY19-T71: peak +$11.60 MTM at 9 PM CDT, then late-evening cooling
# pushed temp into bracket → settled -$24.94 full loss).
#
# Backtest (n=77 settled BUY_NO ≥5qty pool, +2 injected 5/19 settling-losers):
#   n_fire=43  helps=6  hurts=37
#   helps_$ +$272.06  hurts_$ -$63.47
#   RESCUE +$208.59  /  robust +$69.94 (LOO-1 drop 2026-05-12)
# Helps: DC-MAY12 +$70 / DC-MAY14 +$69 / AUS-MAY12 +$70 /
#        SATX-MAY19 +$30 / SEA-MAY04 +$10 / SEA-MAY05 +$23
# Hurts: 26 of 37 caps are < $1 each (selling at NB=99c when settle is 100c).
# Worst caps: DEN-MAY05 -$16.34 / SFO-MAY07 -$6.86 / SATX-MAY11 -$5.70.
# Playbook bars: n>=20 PASS, lift>=+$30 PASS, robust>=+$15 PASS,
# $-weighted h:hu strong (+$272 : -$63), mechanism articulable.
#
# Placed BEFORE obs_confirmed_winner in the exit loop: the late-evening-
# cool-flip pattern (SATX/HOU 5/19) has rm clearly above cap at TP time
# but the bracket can still flip via evening cooling — obs_winner would
# skip the exit on those, missing the saves. Cost of capping confirmed
# winners is small (most at NB=99c → <$1 cap each).
# Reversible: TAKE_PROFIT_15_ENABLED = False to disable.
# Audit script: /tmp/tp_verify_1030.py
TAKE_PROFIT_15_ENABLED        = True
TAKE_PROFIT_15_MIN_MTM_PCT    = 0.15     # MTM/cost floor
TAKE_PROFIT_15_MIN_LOCAL_HOUR = 10       # min local hour (10:30 = past 10:30am LST)
TAKE_PROFIT_15_MIN_LOCAL_MIN  = 30

# ─── PRICE_ZONE block REMOVED 2026-04-29 ─────────────────────────────────
# V2 port that blocked BUY_NO when yes_bid ∈ [30c, 40c] (market "uncertain").
# Removed after all-gate audit on min_bot historical candidates: 2/2
# PRICE_ZONE-blocked cases were winners (NYC-26APR25-B42.5 +$0.34/c,
# ATL-26APR27-B59.5 +$0.33/c). Pattern that V2 saw on max-temp markets
# (50% WR / −$99 / n=50) does not appear to hold for min-temp — possibly
# because min-temp markets in the 30-40c band are pricing different signals
# (overnight cooling vs daytime warming). Sample is small (n=2) but 100%
# winners; the cost of keeping the gate exceeds the cost of the occasional
# miss it might be saving us from. If a future audit flips, re-enable from
# git history (commit before this removal).

# ─── H_2.0 disagreement skip (V2-inspired, d-1+ BUY_NO only) ─────────────
# V2's H_2.0 skips d-1 BUY_NO when NWS-HRRR diverge >2°F. min_bot has no NWS
# integration; we use the existing `disagreement` field (max pairwise diff
# among NBP/HRRR/NBM) as a proxy. Scoped to d-1+ where forecast uncertainty
# is the only error signal (d-0 has running_min). Bypassed when
# _obs_confirmed_alive.
#
# 2026-05-10: 2.0 → 4.5. Stack-aware audit (12d, n=109 historical blocks
# → 81 resolved unique kills after netting overlap with MU_NEAR_BELOW_BRACKET
# / BUY_NO_EXTREME_SIGMA / SPREAD / KLAX_HIGH_SIGMA) showed the gate at 2.0°F
# was blocking 52W:29L (64% winners) with net +$0.43/c (≈ +$30 over 12d at
# $45 bet) BUT LOO-1 robust was −$1.60/c — one bad week wiped the lift.
# Threshold sweep stratified by disag bin:
#   [2.0, 2.5):  6W:1L  +$1.52/c  ← clearly over-blocking
#   [3.0, 3.5): 10W:3L  +$1.74/c  ← clearly over-blocking
#   [3.5, 4.0):  4W:0L  +$1.60/c  ← clearly over-blocking
#   [4.0, 5.0): 11W:7L  −$0.40/c  ← gate breaking even
#   [5.0, 6.0):  7W:6L  −$1.48/c  ← gate working
#   [6.0,  ∞):  10W:8L  −$1.90/c  ← gate working
# T=4.5 passes all 5 playbook bars (n=39, net +$3.73/c ≈ +$280, robust
# +$2.23/c ≈ +$167, helps:hurts 7:3, mechanism = "only block at very high
# disagreement where models genuinely diverge"). 2-4°F is noise; only 5°F+
# is signal. The asymmetric loss profile of d-1+ BUY_NO B-brackets (avg win
# +$0.35, avg loss -$0.65) means even 57% wr at the high-disag tail is net
# negative, so the gate still earns its keep there.
H_2_0_DISAGREE_F = 4.5              # d-1+ BUY_NO disagreement ceiling

# 2026-05-13: BUY_YES_DISAGREE_MAX_F — BUY_YES disagreement ceiling. Backtest
# of 123 settled trades (apr 24 - may 11), BUY_YES cohort only (n_yes ≈ 32):
# disagreement >= 2.0 blocks 11 trades, lift +$50 vs baseline, robust +$30
# (drop biggest-day delta), helps:hurts 4:2 across dates. Walk-forward both
# halves positive. n_blk=11 is below the playbook's n>=20 bar but mechanism
# is conservative (only blocks the model-disagree tail on the smaller cohort
# where every loss matters) and the user wants forward-watch data, so ship.
BUY_YES_DISAGREE_MAX_F = 2.0

# 2026-05-12: COLD_SOURCE_OUTLIER — d-0 + d-1+ BUY_NO gate. Block when the
# bot's picked mu source predicts COLDER than the median of {NBP, NBM-OM,
# HRRR} by > COLD_SOURCE_OUTLIER_F. Closes the d-0 hole in H_2_0 (which is
# d-1+ only) for the specific pattern that hurt KXLOWTNYC-26MAY12-B46.5
# (-$72.74 MTM): bot picked HRRR=41.1, NBP=48, NBM-OM=47 → picked is 5.9°F
# cold-outlier from median=47, mp=0.055 → max-Kelly position into a bracket
# the warmer sources said was in-play. Mechanism: when bot picks the cold
# model and others say warmer, the cold-side conviction rests on a single
# dissenting source — don't trust it.
# Backtest (V1 min lifecycle pool n=95 settled + 10 open MTM since 2026-04-28):
# T=4°F: n=1 block (NYC today, +$72.74 lift, 1:0 helps:hurts). At T=3°F: n=4,
# 1:3 h:h (kills 3 winners — too aggressive). Asymmetric variant chosen over
# symmetric |μ-med|>T (which at T=4 also kills DEN-26MAY12 warm-outlier
# winner +$6.17). Sub-bar by playbook n>=20 but mechanism structurally clean
# and asymmetric scope bounds false-positive risk. Audit script:
# /tmp/v1_min_h20_outlier_backtest.py.
COLD_SOURCE_OUTLIER_ENABLED = True
COLD_SOURCE_OUTLIER_F = 4.0

# 2026-05-12 eve: HIGH_CONVICTION_DISAG_TRAP — d-0 + d-1+ BUY_NO gate. Block
# when bot has high conviction (mp < MP_MAX) AND NWP sources actively disagree
# (>= DISAG_F). Mechanism: low mp triggers max-Kelly sizing; active source
# disagreement means the model's σ may not capture true uncertainty. Together:
# "high conviction without justification" — catastrophic when wrong.
# Backtest (V1 min lifecycle pool n=83 settled+live, 2026-04-28 to 2026-05-12):
#   MP_MAX=0.075, DISAG_F=2.0°F: n=3 blocks, W:L 0:3 (ALL LOSERS, ROI -100%).
#   Lift +$164.13, robust(-1) +$89.56, robust(-2) +$29.93.
#   Catches: NYC-MAY12 (-$74.57, cold-pick outlier), AUS-MAY12 (-$59.63,
#   ensemble warm-bust), ATL-MAY07 (-$29.93, warm-pick outlier).
#   Natural data gaps at thresholds: disag 1.6 (Miami winner) → 2.1 (AUS loser);
#   mp 0.074 (AUS loser) → 0.078 (OKC-MAY08 winner +$11.14).
# Stack-aware vs COLD_SOURCE_OUTLIER: 2 of 3 catches are UNIQUE.
#   - NYC has gap=-5.9 (already caught by COLD)
#   - AUS has gap=-0.1 (UNIQUE — cold-pick gate doesn't fire)
#   - ATL has gap=+2.79 (UNIQUE — warm-pick, cold-pick gate doesn't fire)
# Unique-catch value: +$89.56 over 14d (AUS + ATL).
# Sub-bar by playbook n>=20 (n=3) but 4/5 bars pass strongly: lift 5x bar,
# robust 6x bar, h:h 3:0 PERFECT, mechanism articulable. COLD_SOURCE_OUTLIER
# precedent (commit 2410f33) shipped at n=1 with same sub-bar override.
HIGH_CONVICTION_DISAG_TRAP_ENABLED = True
HIGH_CONVICTION_DISAG_TRAP_MP_MAX  = 0.075
HIGH_CONVICTION_DISAG_TRAP_DISAG_F = 2.0

# 2026-05-27: ENTRY_TIME_WINDOW — BUY_NO entry-window cap. Block BUY_NO entries
# that would fire more than ENTRY_TIME_WINDOW_HOURS_MAX hours before the
# climate-day midnight start (city local time). Catches the "d-1 forecast
# entered too early" failure mode where MIN_EDGE clears on a confident d-1
# forecast but the actual error bars are larger than the model's σ reflects.
#
# Trigger: 2026-05-26 Texas cluster (AUS-T65, SATX-T64, DAL-B66.5 at +9.0/
# +8.4/+7.4h pre-climate, all $79.65 cap, all using hrrr_d1_override, all
# settled LOSS for ~-$238 net). Cluster was 6 of 9 BUY_NO entries in Texas
# region — single forecast bet, six times.
#
# Audit (14d, n=56 BUY_NO resolved entries from 5/13 → 5/26):
#   T=5h: 28 blocks → 19L:9W = 2.1:1 helps:hurts
#           gate_lift +$495.15, LOO-1 robust +$416.95
#           binomial p<0.01 (19/28 = 68% loss-catch vs 50% null)
#   T=4h: 31 blocks → 20L:11W (1.8:1, +$491 / robust +$413)
#   T=6h: 20 blocks → 12L:8W  (1.5:1, +$291 / robust +$213)
# All playbook bars pass at T=5h: n≥20 ✓, h:h≥2:1 ✓, lift positive ✓,
# robust positive ✓. Outcome-by-hpc-bucket shows win-rate dip 25-31% in
# the +2-8h range (vs ~45% in [-2h, +2h] window) — model is uniformly
# worse at d-1 lead than the bot's σ admits.
#
# obs_alive cases never have hpc > 0 (running_min implies entry post climate-
# day start), so this gate doesn't touch d-0/obs-aware entries.
#
# Audit script: /tmp/v1_entry_time_audit.py.
ENTRY_TIME_WINDOW_ENABLED = True
ENTRY_TIME_WINDOW_HOURS_MAX = 5.0  # block BUY_NO when hours pre-climate-day > this

# 2026-05-15: HRRR_DISSENT — BUY_NO B-bracket gate. Block when HRRR predicts
# low at-or-below cap (HRRR is the near-term short-horizon model, ~0-12h
# lead) AND both slow consensus models (NBP, NBM-OM) confidently above cap
# by HRRR_DISSENT_NBP_NBM_BUFFER. Catches the "primary-NBP overconfidence
# with HRRR nowcast dissent" pattern.
#
# Trigger: KXLOWTNYC-26MAY15-B49.5 entered 2026-05-14 14:10 UTC at $0.68 NO
# (88 contracts, $59.84 cost), now MTM −$59.84 / settled-losing (market
# YES at 99c). At entry: NBP=53.0, NBM-OM=52.3, HRRR=50.0 (HRRR exactly at
# cap 50). Bot picked NBP as primary (mu_source="nbp"), ignored HRRR's
# warning. Existing H_2_0_DISAGREE_F (4.5°F) didn't fire because disag
# was 3.0°F (53-50 = 3); COLD_SOURCE_OUTLIER didn't fire because bot
# picked the WARMEST source (53) not coldest.
#
# Backtest (paper_min_bot/tools/backtest_filters.py --stack live,
# since 2026-04-15, pool n=140):
#   n_blocked_inc=1, lift_inc=+$30.00, h:hu=1:0 PERFECT (caught
#   KXLOWTDC-26MAY12-B45.5 −$60.35 loss with same NBP-vs-HRRR pattern:
#   HRRR=45.4 ≤ cap=45.5, NBP=48.0 / NBM-OM=49.1 ≥ cap+1.5=47.0).
#   Robust drop-day: $0 (single fire — drop day removes it).
#   13+ other variants tested (min(picks)<=cap, disag>=4, hrrr<=cap+1,
#   etc.) all bled historical winners; this surgical 1-arm is the only
#   positive-lift configuration. See backtest_filters --custom output.
#
# Mechanism: HRRR is the only short-horizon nowcast in the stack and
# updates hourly with current boundary-layer conditions. NBP is consensus,
# slower-updating; can lag regime shifts. When HRRR ≤ cap but NBP/NBM-OM
# are both confidently above (≥+1.5°F gap), HRRR is typically reading
# nowcast info NBP missed; bot's primary-NBP confidence is misplaced.
HRRR_DISSENT_ENABLED = True
HRRR_DISSENT_NBP_NBM_BUFFER = 1.5   # min(NBP, NBM-OM) >= cap + this triggers
                                    # set ENABLED=False to disable

# 2026-05-17 PM: BUY_NO_NBM_IN_BRACKET — d-1+ BUY_NO B-bracket gate. Block when
# NBM-OM (the bias-corrected slow-model consensus) lies INSIDE the upper 1°F of
# the YES bracket AND HRRR is below cap. Catches the "climatological avg
# predicts cli right at upper bracket edge" pattern: small forecast errors flip
# cli into bracket = BUY_NO loses.
#
# Backtest (stack-aware, full pool n=63 BUY_NO d-1+ since 2026-04-25; nets out
# HRRR_DISSENT / COLD_SOURCE_OUTLIER / HCDT / BUY_NO_LOW_BRACKET_TRAP overlap):
#   n=3 unique catches: MIA-MAY12-B77.5 −$43.04 (NBM=77.9 cap=78), MIN-MAY16-
#   B57.5 −$15.36 (NBM=57.8 cap=58), MIN-MAY13-B48.5 +$11.90 win (NBM=48.1
#   cap=49). h:hu = 2:1 PERFECT by count, lift +$46.50, LOO-1 +$3.46.
#
# Sub-bar on n (n=3 vs playbook n>=20) and LOO-1 ($3 vs $15) but passes the
# count h:hu bar cleanly. Mechanism: NBM ∈ [cap-1, cap] = slow-model consensus
# inside YES bracket. HRRR<cap confirms cold direction. Bot's BUY_NO bet is on
# the wrong side of the bracket geometry. n=1 precedent ships exist (HRRR_DISSENT,
# COLD_SOURCE_OUTLIER both shipped at n=1 with mechanism override).
#
# Driver: 5 of 6 May 11-17 fully-stopped BUY_NO catastrophes (-$197 cohort)
# uncaught by current stack. This gate catches the 2 with NBM-inside-bracket
# pattern (MIA-MAY12 + MIN-MAY16 = -$58.40 of the $197) and prevents PHIL-MAY18
# (open today, NBM=65.6 cap=66 → in [65, 66]) from following the same path.
BUY_NO_NBM_IN_BRACKET_ENABLED = True
BUY_NO_NBM_IN_BRACKET_MARGIN_F = 1.0  # NBM in [cap - margin, cap] triggers
                                      # set ENABLED=False to disable

# 2026-05-17 PM: BUY_NO_HIGH_MP_TRAP — BUY_NO gate (all brackets). Block when
# bot's model_prob > MP_MAX. Calibration audit shows bot's mp is UNDERESTIMATED
# at the high end: mp 25-30% bucket actually loses 67% (n=3), mp 20-25% loses
# 30% (n=10), mp 15-20% loses only 12% (n=17 — the calibration sweet spot).
#
# Backtest (stack-aware, full pool):
#   mp > 0.24: n=5 unique, W=2 L=3, lift +$101, LOO-1 +$49, h:hu 3:2 PERFECT.
#   Catches SEA-MAY11 (-$52 STOP), MIA-MAY12 (-$43 STOP), DC-MAY17 (-$24 STOP)
#   = 3 of the recent fully-stopped catastrophes. Blocks BOS-MAY16 +$4.50 and
#   SFO-MAY12 +$13.27 wins.
#
# Mechanism: When bot mp > 24% on a BUY_NO, the bot is essentially taking a
# coin-flip-ish bet on a position with asymmetric reward (NO at $0.55-0.65 →
# win pays 35-45c, lose costs 55-65c). At true mp=67% (per calibration), the
# EV is decisively negative. The bot's "edge" reading at high mp is the
# classic obs-rule trap: market is right, our model is wrong — when bot
# disagrees with market by 25+pp on a high-conviction position, market knows
# something. See feedback_market_right_obs_wrong / RULE #2.
#
# Sub-bar on n (n=5 vs playbook n>=20) — small sample but internally consistent
# (LOO-1 robust + calibration table provides independent mechanistic evidence).
# h:hu 3:2 dollar-weighted is even stronger (6.7:1 by $).
BUY_NO_HIGH_MP_TRAP_ENABLED = True
BUY_NO_HIGH_MP_TRAP_MP_MAX  = 0.24   # block BUY_NO when model_prob > this
                                     # set ENABLED=False to disable

# 2026-05-18 PM: BUY_NO_TAIL_RISK — BUY_NO B-bracket gate. Block when chosen mu
# is barely above cap (0 < mu - cap < MU_GAP_F): forecast sits right at the
# upper bracket boundary so any small error puts cli inside [floor, cap] and
# BUY_NO loses. Coin-flip-ish bracket geometry.
#
# Backtest (May 4-18 BUY_NO settled, full pool n=74; stack-aware net of all
# prior gates incl. NBM_IN_BRACKET / HIGH_MP_TRAP / HRRR_DISSENT / HCDT /
# COLD_OUTLIER):
#   n=12 unique blocks. helps:hurts = 7:5 (1.40x positive).
#   lift +$87.76, LOO-1 +$35.86.
#   Historical losses blocked: SEA-MAY11 -$51.90 (mu=52 cap=51), DAL-MAY14
#     -$31.64 (mu=69 cap=68), LAX-MAY08 -$29.27 (mu=60.4 cap=59), MIN-MAY16
#     -$15.36 (mu=59 cap=58), LV-MAY18 -$8.82 (mu=59.9 cap=59), PHX-MAY18
#     -$6.12 (mu=68.4 cap=68), DEN-MAY08 -$1.34 (mu=43.6 cap=42) — 7 total.
#   Historical wins blocked: NYC-MAY13 +$33.88, NYC-MAY16 +$15.04, MIN-MAY11
#     +$5.16, OKC-MAY08 +$1.75, SATX-MAY09 +$0.86 — 5 total.
#
# Today's open MTM losers caught (2026-05-18 22:10 UTC):
#   PHIL-MAY18 -$22.32 (mu=67 cap=66, gap=1.0; ALSO caught by NBM_IN_BRACKET)
#   LV-MAY18 -$8.68 (mu=59.9 cap=59, gap=0.9)
#   PHX-MAY18 -$3.40 (mu=68.4 cap=68, gap=0.4)
#   MIN-MAY18 -$2.58 (mu=56.5 cap=55, gap=1.5)
# Misses CHI-MAY18 (gap=2.9, just outside), SFO-MAY19 (HRRR-src),
# SATX-MAY19 (T-bracket, no clean signal).
#
# Sub-bar on n (n=12 vs playbook n>=20) but passes count h:hu (1.40x),
# lift +$88 (≥$30), LOO-1 +$36 (≥$15). Same sub-n precedent as NBM_IN_BRACKET
# (n=3 ship). Mechanism: B-bracket BUY_NO with mu barely above cap is essentially
# a coin-flip — Gaussian sigma in production is typically 2-5°F so a 0.5-1.5°F
# error window of mu being above cap is well within the noise floor. Bypassed
# by caller when _obs_confirmed_alive.
BUY_NO_TAIL_RISK_ENABLED   = True
BUY_NO_TAIL_RISK_MU_GAP_F  = 2.0   # block when 0 < mu - cap < this (B only)
                                   # set ENABLED=False to disable

# 2026-05-19 PM: BUY_NO_HRRR_IN_BRACKET_WARM — BUY_NO B-bracket gate. Block
# when bot bets warm-side (mu > cap) BUT HRRR (the short-horizon nowcast)
# predicts cli will land INSIDE the YES bracket [floor, cap]. Direct two-source
# opposition on the exact direction of risk — the bot picked a warmer model
# for primary mu while HRRR points at the bracket.
#
# Found via cli-augmented deep-dive (2026-05-19): pulled `cli_reports.low_f`
# from obs.sqlite for all BUY_NO B-bracket trades including STOP_LOSS exits
# (which don't write settlement records). With ground-truth cli on every
# BUY_NO B trade May 4-19, n=72.
#
# Backtest (May 4-19 BUY_NO settled+stopped, stack-aware net of all prior
# gates incl. TAIL_RISK + NBM_IN_BRACKET + HIGH_MP_TRAP):
#   n=5 unique blocks. helps:hurts = 5:0 (∞ — perfect).
#   lift +$174.21, LOO-1 +$113.86.
#   Detail (all 5 LOSSES):
#     DC-MAY12 -$60.35 (mu=48 nbp, floor=45 cap=46, HRRR=45.4, cli=46)
#     NYC-MAY15 -$59.84 (mu=53 nbp, floor=49 cap=50, HRRR=50.0, cli=50)
#     DAL-MAY14 -$31.64 (mu=69 nbp, floor=67 cap=68, HRRR=67.8, cli=68)
#     MIA-MAY04 -$21.96 (mu=74 nbp, floor=71 cap=72, HRRR=72.0, cli=71)
#     DAL-MAY11 -$0.42 (mu=61.7 nbm_d0_override, floor=58 cap=59, HRRR=58.3, cli=59)
#
# Overlap with shipped TAIL_RISK (mu-cap<2): 1 ticker (DAL-MAY14, mu-cap=1
# AND HRRR-in-bracket — both filters catch it, no harm). Net 4 NEW catches
# beyond TAIL_RISK = +$142.57 disjoint lift.
#
# Sub-bar on n (n=5 vs playbook n>=20) but PASSES all other bars including
# the strict h:hu>=2:1 by infinite margin. Same sub-n precedent as
# NBM_IN_BRACKET (n=3) + HRRR_DISSENT (n=1) ships.
#
# Bypassed by caller when _obs_confirmed_alive (running_min established).
BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = True

# 2026-05-20 PM: BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP. BUY_NO gate. Block
# when market priced YES >> bot mp (yes_ask_c >= K * mp_pct, K=8). Catches
# SATX-style mp~0 outliers where bot truncated-Gaussian collapses P(YES) to
# ~0 but market reads forecast trajectory / cooling regime / dealer flow
# correctly. Market BBO encodes signal the static mu/sigma model misses.
#
# Backtest n=71 settled BUY_NO with mp+yes_ask (2026-04-25 to 05-20):
#   K=8:  skip 2 (NYC-26MAY12-B46.5 -74.57 USD + SATX-26MAY19-T71 -24.94 USD),
#         2L:0W, 0 winners killed, lift +99.51 USD
#   K=5:  skip 6 (4L:2W; 6.50 USD winners killed), lift +182.57 USD
#   K=10: skip 1 (SATX only), lift +24.94 USD
# Picked K=8 as no-false-positive floor; both catches were the bots worst
# recent losses. Mechanism cleanly captures SATX-class (mp=0.7%, yes_ask=46c,
# ratio=65x) and NYC-MAY12 (mp=5.5%, yes_ask=45c, ratio=8.2x).
#
# Stack overlap with MODEL_MARKET_DISAGREE: zero. That filter requires
# mp >= 0.22; this filter requires mp <= yes_ask/(K*100), so K=8 implicitly
# bounds mp <= 0.125 even at yes_ask=100c. Complementary, no double-block.
#
# Bypassed by caller when _obs_confirmed_alive (running_min established).
BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = True
BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO   = 8.0   # yes_ask_c / (mp*100)


ORDER_FILL_TIMEOUT_SEC = 5.0        # wait this long for fill, then cancel
# 2026-05-03: laddered ask-chase on first-entry partial fills. After the
# initial maker order fills < intended count, walk the ask up by 1c each
# retry until edge < MIN_EDGE or remainder filled. Caps execution time +
# slippage compounding. See _should_enter ladder block below.
# 2026-05-04: bumped 3 → 5 (PHX/AUS/SEA orderbooks too thin for 3 walks
# to clear meaningful intended counts) AND extended to BUY_YES (was BUY_NO
# only). BUY_YES ladder uses yes_ask + MAX_BET_BUY_YES_USD ($5) cap.
LADDER_MAX_RETRIES = 5
BANKROLL_FLOOR_USD = 5.00           # refuse new orders if portfolio cash < this
# 2026-05-02: BANKROLL_REF_USD now drives live Kelly sizing (was: documentation-
# only). Chris bumped to $500 because actual Kalshi cash had drifted to ~$2-50
# under heavy open exposure, leaving Kelly bet_usd = kelly × bankroll pinned at
# the 1-contract floor (e.g. HOU-B52.5 sized at $0.43 even with edge=38.8%).
# Pairs with `_get_bankroll_cached()` change below: once cold-start gate clears
# (one successful Kalshi balance fetch confirming live connectivity), sizing
# anchors on this constant, not the live `available` cash. Real-balance
# insufficient_balance bounces are still self-correcting via the existing
# retry path. Backtest-standardization memory should reflect this same number.
BANKROLL_REF_USD = 500.00

# Auto-cleanup of position records whose climate day is more than this many
# days in the past. Defends against positions that never settle (data error)
# from accumulating indefinitely.
POSITION_TTL_DAYS = 3

# Data paths
DATA_DIR = Path("/home/ubuntu/paper_min_bot/data")
LOG_DIR = Path("/home/ubuntu/paper_min_bot/logs")
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    # Non-VPS environments (tests, dev boxes) may not have /home/ubuntu.
    # Tests override DATA_DIR/LOG_DIR before calling anything that writes.
    pass  # noqa: defensive — startup mkdir on dev/test boxes (no /home/ubuntu)

POSITIONS_FILE = DATA_DIR / "positions.json"
# Legacy single-file path (writes pre-2026-04-29). Kept for historical reads
# only — new writes route through `_trades_file_today()` which date-rotates.
TRADES_FILE = DATA_DIR / "trades.jsonl"
SETTLEMENTS_FILE = DATA_DIR / "settlements.jsonl"
STATS_FILE = DATA_DIR / "stats.json"
NBP_CACHE_FILE = DATA_DIR / "nbp_cache.json"
LAST_RM_SEEN_FILE = DATA_DIR / "last_rm_seen.json"
ENV_FILE = Path("/home/ubuntu/.env")

# Kalshi
KALSHI_BASE = "https://api.elections.kalshi.com"
KALSHI_TIMEOUT = 15.0

# WebSocket BBO live overlay (mirrors V2). When True, kalshi_ws subscribes to
# every discovered market_ticker and live BBO replaces the REST snapshot
# returned by /markets — typical freshness drops from ~30s to <100ms.
USE_KALSHI_WS = True

# ═══════════════════════════════════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════════════════════════════════

_LOG_LOCK = threading.Lock()
_LOG_FILE_PATH = LOG_DIR / f"min_bot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"

def log(msg: str, level: str = "info") -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    with _LOG_LOCK:
        print(line, flush=True)
        try:
            # Rotate log by date
            global _LOG_FILE_PATH
            today_path = LOG_DIR / f"min_bot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
            _LOG_FILE_PATH = today_path
            with open(today_path, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass  # noqa: defensive — log file write fails are benign; stdout still has the line


# Skip-log debouncing. The skip path inside execute_opportunity logs a line
# every time a gate fires; the same (ticker, gate, values) typically repeats
# every cycle until the ticker settles or quotes change. Pre-fix the log
# was 83% repeated skip lines (82k of 99k lines/24h, top offender 2108 firings
# of OBS_CONFIRMED_LOSER on one ticker). Cap at one log per (ticker, msg) until
# the message changes, with a re-log every 30 min so dashboards / log-tail
# eyeballing still see "still blocked."
_skip_log_state: dict[str, tuple[str, float]] = {}
_skip_log_lock = threading.Lock()
SKIP_LOG_RELOG_SEC = 1800.0


def _log_skip(ticker: str, msg: str) -> None:
    """log(msg) but suppress repeats for the same (ticker, msg) within
    SKIP_LOG_RELOG_SEC. msg should be the full pre-formatted skip line
    (e.g. `"  skip KXLOWTNYC-26APR28-B45.5: edge 25% > MAX_EDGE 42%"`).
    Different formatted values (e.g. edge 25% → 26%) count as different
    msgs and re-log normally."""
    now = time.time()
    with _skip_log_lock:
        prev = _skip_log_state.get(ticker)
        if prev is not None and prev[0] == msg and (now - prev[1]) < SKIP_LOG_RELOG_SEC:
            return
        _skip_log_state[ticker] = (msg, now)
    log(msg)


_discord_skip_state: dict[str, float] = {}
_discord_skip_lock = threading.Lock()
DISCORD_SKIP_RELOG_SEC = 21600.0  # 6h dedup; ticker lifetime is ~1 climate day


def _discord_skip_send(ticker: str, msg: str) -> None:
    """Discord version of _log_skip — dedup so the same (ticker) doesn't
    spam the channel every 30s eval cycle. First fire goes through; repeats
    within DISCORD_SKIP_RELOG_SEC are suppressed."""
    now = time.time()
    with _discord_skip_lock:
        last = _discord_skip_state.get(ticker, 0.0)
        if (now - last) < DISCORD_SKIP_RELOG_SEC:
            return
        _discord_skip_state[ticker] = now
    discord_send(msg)


def _audit_skip(opp: dict, decision: str, msg: str) -> None:
    """Audit-aware gate-skip helper (2026-05-04 night Phase B extension).

    Wraps _log_skip + decision_log.record(). Use at every execute_opportunity
    gate-skip-and-return-False site so each gate decision lands in
    bot_decisions.sqlite alongside V1+V2's eval rows. The `decision` tag
    becomes the queryable column for cross-bot backtests.

    Spread is captured in fraction form (cents/100) for cross-bot consistency.
    Failures inside record() are swallowed — never let logging crash trading.
    """
    ticker = opp.get("market_ticker", "") if isinstance(opp, dict) else ""
    _log_skip(ticker, msg)
    if _dlog is None or not isinstance(opp, dict):
        return
    try:
        action = opp.get("action")
        if action == "BUY_YES":
            ya, yb = opp.get("yes_ask"), opp.get("yes_bid")
            sp = (ya - yb) if (ya is not None and yb is not None) else None
        else:
            na, nb = opp.get("no_ask"), opp.get("no_bid")
            sp = (na - nb) if (na is not None and nb is not None) else None
        _dlog.record(
            bot='min_bot', stage='gate', decision=decision,
            ticker=ticker,
            series=opp.get('series'),
            settlement_date=opp.get('date_str'),
            station=opp.get('station'),
            floor=opp.get('floor'), cap=opp.get('cap'),
            days_out=_days_out_int(opp),
            next_action=action,
            yes_bid=(opp.get('yes_bid') / 100.0) if opp.get('yes_bid') is not None else None,
            yes_ask=(opp.get('yes_ask') / 100.0) if opp.get('yes_ask') is not None else None,
            no_bid=(opp.get('no_bid') / 100.0) if opp.get('no_bid') is not None else None,
            no_ask=(opp.get('no_ask') / 100.0) if opp.get('no_ask') is not None else None,
            spread=(sp / 100.0) if sp is not None else None,
            model_prob=opp.get('model_prob'),
            edge=opp.get('edge'),
            mu_blended=opp.get('mu'),
            sigma_final=opp.get('sigma'),
            nbp_mu=opp.get('mu_nbp'), nbp_sigma=opp.get('sigma_nbp'),
            hrrr_high=opp.get('mu_hrrr'),
            nbm_high=opp.get('mu_nbm_om'),
            running_min=opp.get('running_min'),
            disagree_ratio=opp.get('disagreement'),
            blocker_reason=msg.strip(),
        )
    except Exception:
        pass


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, default=str)
    os.replace(tmp, path)


# Date-rotated trades log. Writes go to `data/trades_YYYY-MM-DD.jsonl` per
# UTC date (matches V2's `weather_candidates_YYYY-MM-DD.jsonl` convention).
# Pre-rotation history lives in the legacy `trades.jsonl` file. Readers that
# only care about today's entries (_compute_today_exposure,
# _reconcile_from_trades_log) read today's file directly — fast even on a
# 1GB+ historical archive — while the gate-audit / backtest tooling globs
# every dated file plus the legacy file when it needs full history.
def _trades_file_today() -> Path:
    return DATA_DIR / f"trades_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"


# ═══════════════════════════════════════════════════════════════════════
# DISCORD NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════
# Pattern ported from V1 (kalshi_weather_bot.py): one bounded queue + one
# worker thread, never block the caller, drop on overflow. Auth is a Discord
# bot token; the same bot must already be a member of DISCORD_CHANNEL.

DISCORD_TOKEN = ""                  # filled from ~/.env at startup
DISCORD_CHANNEL = "1497464077608550570"   # min_bot updates channel

import queue as _queue
_discord_queue: "_queue.Queue[str]" = _queue.Queue(maxsize=50)
_discord_dropped = 0


def _discord_worker_loop() -> None:
    url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL}/messages"
    headers = {"Authorization": f"Bot {DISCORD_TOKEN}", "Content-Type": "application/json"}
    while True:
        try:
            msg = _discord_queue.get()
        except Exception:
            time.sleep(1.0)
            continue
        try:
            text = str(msg)
            chunks = [text[i:i + 1990] for i in range(0, len(text), 1990)] if len(text) > 1990 else [text]
            for chunk in chunks:
                try:
                    resp = httpx.post(url, headers=headers, json={"content": chunk}, timeout=5.0)
                    if resp.status_code == 429:
                        try:
                            retry_after = float(resp.json().get("retry_after", 1.0))
                        except Exception:
                            retry_after = 1.0
                        time.sleep(min(max(retry_after, 0.5), 10.0))
                        break
                    if resp.status_code >= 400:
                        if not hasattr(discord_send, "_last_err") or time.time() - discord_send._last_err > 300:
                            discord_send._last_err = time.time()
                            print(f"[discord] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
                        break
                except Exception as exc:
                    if not hasattr(discord_send, "_last_err") or time.time() - discord_send._last_err > 300:
                        discord_send._last_err = time.time()
                        print(f"[discord] send failed: {exc}", flush=True)
                    break
        except Exception as _outer_exc:
            # Unexpected error in the worker outer loop. Don't recurse via
            # discord_send (we ARE the worker); print to stdout, throttled
            # via the same `_last_err` tag so a stuck worker doesn't spam.
            if not hasattr(discord_send, "_last_err") or time.time() - discord_send._last_err > 300:
                discord_send._last_err = time.time()
                print(f"[discord] worker outer exception: {type(_outer_exc).__name__}: {_outer_exc}", flush=True)
        finally:
            try:
                _discord_queue.task_done()
            except Exception:
                pass  # noqa: defensive — task_done() can raise ValueError if queue state is odd; we don't care here


def discord_send(msg: str) -> None:
    """Enqueue a Discord message. Non-blocking; drops if queue is full."""
    if not DISCORD_TOKEN:
        return
    global _discord_dropped
    try:
        _discord_queue.put_nowait(str(msg))
    except _queue.Full:
        _discord_dropped += 1
        if _discord_dropped == 1 or _discord_dropped % 100 == 0:
            print(f"[discord] queue full — dropped {_discord_dropped} messages total", flush=True)


def _start_discord_worker() -> None:
    """Load token from ~/.env and start the single worker thread. Idempotent."""
    global DISCORD_TOKEN
    if DISCORD_TOKEN:
        return
    try:
        env_txt = ENV_FILE.read_text()
        m = re.search(r"DISCORD_BOT_TOKEN=(\S+)", env_txt)
        if m:
            DISCORD_TOKEN = m.group(1)
    except Exception as _env_exc:
        # noqa: should_log — env file missing/unreadable. The follow-up
        # `if not DISCORD_TOKEN` log will fire too, but log the actual
        # underlying error so missing-file vs permissions vs malformed
        # are distinguishable in startup logs.
        log(f"  Discord token load: env read failed: {type(_env_exc).__name__}: {_env_exc}", "warn")
    if not DISCORD_TOKEN:
        log("  Discord disabled — DISCORD_BOT_TOKEN missing from .env", "warn")
        return
    threading.Thread(target=_discord_worker_loop, name="discord-worker", daemon=True).start()
    log(f"  Discord worker started → channel {DISCORD_CHANNEL}")


def notify_discord_entry(record: dict, opp: dict) -> None:
    """Send a one-line ENTRY notification to Discord."""
    discord_send(
        f"**ENTRY** {record['action']} {record['count']}x @ {int(round(record['entry_price']*100))}c "
        f"on `{record['market_ticker']}` ({record['label']})\n"
        f"edge {record['edge']:.0%}  mp {record['model_prob']:.0%}  "
        f"μ {record['mu']:.1f}°F  σ {record['sigma']:.1f}°F  "
        f"rm {record['running_min']}  cost ${record['cost']:.2f}  "
        f"day ${_today_exposure_usd:.2f}/${DAILY_EXPOSURE_CAP_USD:.2f}"
    )


def notify_discord_settlement(ticker: str, action: str, cli_low: int,
                              in_bracket: bool, won: bool, pnl: float) -> None:
    emoji = "🟢" if won else "🔴"
    discord_send(
        f"{emoji} **SETTLED** `{ticker}` — {action}, CLI {cli_low}°F, "
        f"in_bracket={in_bracket}, P&L **${pnl:+.2f}**"
    )


# ═══════════════════════════════════════════════════════════════════════
# KALSHI CLIENT + AUTH
# ═══════════════════════════════════════════════════════════════════════

_KEY_ID: str = ""
_PRIVATE_KEY = None
_KALSHI_WS_STARTED = False

# Hardcoded V2 wallet key id — same constant `obs-pipeline-bot/kalshi_weather_bot_v2.py`
# uses. Account 2; rate-limit bucket is independent of the V1 account.
_KALSHI_KEY_ID_V2 = "7224fdb1-f5c9-4dc5-a1ce-b85013ad34d1"


def _load_kalshi_auth() -> None:
    """Load Kalshi auth based on WALLET.
        v1 → ~/.env KALSHI_KEY_ID + ~/kalshi_key.pem
        v2 → _KALSHI_KEY_ID_V2 const + obs-pipeline-bot/kalshi_key_v2_account2.pem
    Auth is needed for market discovery (signed)."""
    global _KEY_ID, _PRIVATE_KEY
    if WALLET == "v2":
        _KEY_ID = _KALSHI_KEY_ID_V2
        pem_path = Path("/home/ubuntu/obs-pipeline-bot/kalshi_key_v2_account2.pem")
    else:
        try:
            env_txt = ENV_FILE.read_text()
            m = re.search(r"KALSHI_KEY_ID=(\S+)", env_txt)
            if not m:
                raise RuntimeError("KALSHI_KEY_ID missing from .env")
            _KEY_ID = m.group(1)
        except Exception as e:
            raise RuntimeError(f"Failed loading .env: {e}") from e
        pem_path = Path("/home/ubuntu/kalshi_key.pem")
    if not pem_path.exists():
        raise RuntimeError(f"Kalshi PEM missing at {pem_path} (wallet={WALLET})")
    _PRIVATE_KEY = serialization.load_pem_private_key(pem_path.read_bytes(), password=None)


def _sign(method: str, path: str) -> dict[str, str]:
    if _PRIVATE_KEY is None:
        _load_kalshi_auth()
    ts_ms = str(int(time.time() * 1000))
    msg = (ts_ms + method + path).encode()
    sig = _PRIVATE_KEY.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": _KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        "KALSHI-ACCESS-TIMESTAMP": ts_ms,
        "Content-Type": "application/json",
    }


def kalshi_get(path: str, params: dict | None = None) -> dict:
    r = httpx.get(KALSHI_BASE + path, params=params, headers=_sign("GET", path),
                  timeout=KALSHI_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _ensure_kalshi_ws() -> None:
    """Start the WebSocket BBO client once auth is loaded. Idempotent."""
    global _KALSHI_WS_STARTED
    if _KALSHI_WS_STARTED or not USE_KALSHI_WS:
        return
    if _PRIVATE_KEY is None:
        _load_kalshi_auth()
    try:
        kalshi_ws.start(_sign, log_fn=lambda s: log(s))
        _KALSHI_WS_STARTED = True
    except Exception as e:
        log(f"kalshi_ws.start failed: {e}", "warn")


def kalshi_post(path: str, body: dict) -> dict:
    r = httpx.post(KALSHI_BASE + path, json=body, headers=_sign("POST", path),
                   timeout=KALSHI_TIMEOUT)
    if r.status_code >= 400:
        # Surface the response body so callers can pattern-match on errors.
        raise httpx.HTTPStatusError(f"{r.status_code}: {r.text[:200]}",
                                    request=r.request, response=r)
    return r.json()


def kalshi_delete(path: str) -> dict:
    r = httpx.delete(KALSHI_BASE + path, headers=_sign("DELETE", path),
                     timeout=KALSHI_TIMEOUT)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}


def get_kalshi_balance() -> Optional[float]:
    """Available portfolio balance in USD. Returns None on failure.
    Kalshi /portfolio/balance returns balance in cents."""
    try:
        d = kalshi_get("/trade-api/v2/portfolio/balance")
        bc = d.get("balance")
        if bc is None:
            return None
        return float(bc) / 100.0
    except Exception as e:
        log(f"  balance fetch failed: {e}", "warn")
        return None


def place_kalshi_order(ticker: str, side: str, count: int,
                       price_cents: int) -> Optional[str]:
    """Place a limit BUY at price_cents. Returns order_id or None on failure.

    2026-04-30: removed 409 trading_is_paused and 400 insufficient_balance
    cooldowns. Per `feedback_no_unnecessary_cooldowns.md`: speed > politeness;
    cost of retry is one HTTP RTT and a log line, no fee, no fill. Faster
    retry = faster fill the moment Kalshi unpauses or a balance refresh
    (every 60s) repopulates the cache."""
    body = {
        "ticker": ticker, "action": "buy", "side": side,
        "type": "limit", "count": count,
    }
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    try:
        r = kalshi_post("/trade-api/v2/portfolio/orders", body)
        order = r.get("order", {})
        oid = order.get("order_id")
        st = order.get("status", "?")
        log(f"  ORDER buy {count}x {side} @ {price_cents}c on {ticker} -> {oid} ({st})")
        return oid
    except Exception as e:
        log(f"  ORDER FAILED {ticker} {side} {count}@{price_cents}c: {e}", "error")
        return None


def place_kalshi_sell_order(ticker: str, side: str, count: int,
                             price_cents: int) -> Optional[str]:
    """Place a limit SELL at price_cents. Used by hard-stop exits.
    `side` is the side we currently HOLD (e.g. 'no' if we hold BUY_NO).
    Returns order_id or None on failure."""
    body = {
        "ticker": ticker, "action": "sell", "side": side,
        "type": "limit", "count": count,
    }
    if side == "yes":
        body["yes_price"] = price_cents
    else:
        body["no_price"] = price_cents
    try:
        r = kalshi_post("/trade-api/v2/portfolio/orders", body)
        order = r.get("order", {})
        oid = order.get("order_id")
        st = order.get("status", "?")
        log(f"  ORDER sell {count}x {side} @ {price_cents}c on {ticker} -> {oid} ({st})")
        return oid
    except Exception as e:
        log(f"  SELL FAILED {ticker} {side} {count}@{price_cents}c: {e}", "error")
        return None


def wait_for_fill(order_id: str, expected_count: int,
                  timeout_sec: float = 5.0) -> tuple[str, int]:
    """Wait up to timeout_sec for `expected_count` fills. Returns
    (status, filled_count). Polls kalshi_ws fill cache, falls back to REST."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if USE_KALSHI_WS:
            try:
                f = kalshi_ws.get_fill(order_id)
                if f and f.get("total_count", 0) >= expected_count:
                    return ("filled", int(f["total_count"]))
            except Exception:
                pass  # noqa: defensive — WS fill-cache poll is best-effort; REST fallback is authoritative
        time.sleep(0.1)
    # REST fallback (authoritative)
    try:
        d = kalshi_get(f"/trade-api/v2/portfolio/orders/{order_id}")
        o = d.get("order", {})
        rc = o.get("remaining_count_fp")
        remaining = int(float(rc)) if rc else o.get("remaining_count", expected_count)
        filled = max(0, expected_count - remaining)
        st = o.get("status", "unknown")
        if st == "executed" or remaining == 0:
            st = "filled"
        return (st, filled)
    except Exception as e:
        log(f"  fill check failed for {order_id}: {e}", "warn")
        return ("unknown", 0)


# ═══════════════════════════════════════════════════════════════════════
# OBS READER — running_min + snapshot from obs-pipeline
# ═══════════════════════════════════════════════════════════════════════

def _climate_date_nws(tz_name: str, now_utc: datetime | None = None) -> str:
    """NWS LST climate day (no DST). Mirrors obs-pipeline's function."""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    jan = datetime(now_utc.year, 1, 15, 12, 0, tzinfo=ZoneInfo(tz_name))
    std_offset = jan.utcoffset() or timedelta(0)
    lst = now_utc + std_offset
    return lst.strftime("%Y-%m-%d")


def get_running_min(station: str, climate_date: str) -> Optional[float]:
    """Read running_min from obs-pipeline sqlite for (station, climate_date).
    Returns None if no obs recorded yet."""
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT min_f FROM running_min WHERE station=? AND climate_date=?",
            (station, climate_date),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else None
    except sqlite3.Error as e:
        log(f"  obs-pipeline DB read error for {station} {climate_date}: {e}", "warn")
        return None


# 2026-05-05: V1-port `_cli_aligned_rm` analog for min-bot. Returns the
# integer-°F-rounded running_min that matches NWS CLI's reporting precision.
# Used by precision_shadow_audit.py and (after validation) by entry/exit
# gate comparisons against bracket floor/cap to align bot's view with how
# Kalshi will settle.
#
# Why: bot's running_min can be 0-1°F above CLI integer settlement because
#   (a) integer-°C sources (madis_fsl2, nws_obs) round DOWN at the 0.5°C
#       boundary (e.g., true 13.3°C displayed as 13°C → 55.4°F when CLI's
#       2-min ASOS avg rounds 55.94°F → 56°F), and
#   (b) CLI uses 2-min ASOS averages with integer °F rounding, structurally
#       different from our 5-min METAR snapshot.
# When bot's raw rm=55.4°F is compared to bracket cap=55, raw rm is "in
# bracket" (BUY_NO would lose). cli_aligned_rmin(55.4)=55 still in bracket
# at this value, but cli_aligned_rmin(55.94)=56 (CLI's actual rounding) is
# out of bracket — matching how Kalshi settles.
#
# USE_CLI_ALIGNED_RMIN flag: OFF by default (no behavior change). Flip on
# only after the precision_shadow_audit.py 7-day report (2026-05-12) shows
# net P&L lift from the cli-aligned variant. Gate-by-gate wiring is
# deferred until then; this helper is a pure utility today.
USE_CLI_ALIGNED_RMIN = True  # 2026-05-06: V1/V2-port. Aligns rm at model_prob calc + obs_confirmed_* checks via METAR-precision lookup, not just int-rounding.


def _lst_climate_window_utc(station: str, climate_date: str) -> Optional[tuple[int, int]]:
    """Return (start_utc_ts, end_utc_ts) for a station's LST climate day.

    Kalshi settles weather brackets on LST climate days (no DST). Each station's
    LST window has a different UTC offset (Pacific=UTC-8, Mountain=UTC-7,
    Central=UTC-6, Eastern=UTC-5; KPHX is also Mountain no-DST). Using a
    universal pad pulls the prior climate day's tail into next-day queries —
    documented failure mode in `feedback_per_station_lst_climate_window.md`.

    Returns None if station tz unknown or date_str unparseable; caller treats
    as "skip the precision lookup, fall back to raw running_min."
    """
    tz_name = _STATION_TZ.get(station)
    if not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
        # Jan-15 trick: standard time only (no DST) → matches LST.
        y = int(climate_date.split("-")[0])
        jan15 = datetime(y, 1, 15, tzinfo=tz)
        lst_offset_h = jan15.utcoffset().total_seconds() / 3600.0
        d = datetime.strptime(climate_date, "%Y-%m-%d")
        start_utc = d.replace(tzinfo=timezone.utc) + timedelta(hours=-lst_offset_h)
        end_utc = start_utc + timedelta(hours=24)
        return int(start_utc.timestamp()), int(end_utc.timestamp())
    except Exception:
        return None


def _get_metar_running_min(station: str, climate_date: str) -> Optional[float]:
    """METAR-text-only running_min from awc/ldm sources for (station, date).

    Returns the minimum temp_f from observations table where source IN
    ('awc', 'ldm') for the given climate_date. These are NWS METAR T-group
    decimal sources (0.1°C precision) — what NWS CLI actually rolls up to
    integer °F. Mirror of V1/V2 `_get_metar_running_max`.

    Returns None when no awc/ldm observations are present for the date,
    or on DB error. Caller falls back to the unaligned running_min.

    2026-05-08 BUG FIX: previously used a ±12h pad on UTC-midnight bounds,
    which created a 48-hour window that pulled in the PRIOR climate day's
    morning low (cooling cycle). For KHOU on 2026-05-08 the window pulled
    in 5/7 morning's 64.94°F low → cli_aligned_rmin returned 65 → false
    OBS_CONFIRMED_ALIVE on B70.5 (true today rm was 73.4). Now uses proper
    per-station LST climate window. See feedback_per_station_lst_climate_window.md.
    """
    win = _lst_climate_window_utc(station, climate_date)
    if win is None:
        return None
    start_ts, end_ts = win
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            """
            SELECT MIN(temp_f) FROM observations
            WHERE station = ?
              AND source IN ('awc','ldm')
              AND obs_time >= ?
              AND obs_time <  ?
            """,
            (station, start_ts, end_ts),
        ).fetchone()
        conn.close()
        if row and row[0] is not None:
            return float(row[0])
        return None
    except sqlite3.Error as e:
        log(f"  metar rmin DB read error for {station} {climate_date}: {e}", "warn")
        return None


def _get_current_temp_f(station: Optional[str], window_sec: int = 1800) -> Optional[float]:
    """Latest temp_f observation at `station` from any source within `window_sec`.

    Used by OBS_CONFIRMED_LOSER (BUY_NO branch) as a sanity check: if the live
    temperature is well below the bot's `running_min`, rm is stale or sourced
    differently (cli-aligned RM occasionally lags raw METAR briefly during
    cooling) and the LOSER signal is unreliable. Returns None when no fresh
    obs are available — caller treats as "skip the sanity check"."""
    if not station:
        return None
    cutoff = int(time.time() - window_sec)
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT temp_f FROM observations "
            "WHERE station = ? AND temp_f IS NOT NULL AND obs_time >= ? "
            "ORDER BY obs_time DESC LIMIT 1",
            (station, cutoff),
        ).fetchone()
        conn.close()
        return float(row[0]) if row else None
    except sqlite3.Error as e:
        log(f"  obs current-temp read error for {station}: {e}", "warn")
        return None


def _cli_aligned_rmin(running_min: Optional[float],
                     station: Optional[str] = None,
                     climate_date: Optional[str] = None) -> Optional[float]:
    """CLI-aligned integer °F running_min for bracket-edge comparisons.

    2026-05-06 (V1/V2-port): when station+climate_date are provided AND the
    flag is on, prefer METAR-precision running_min (from awc/ldm 0.1°C T-group
    obs) over the polluted running_min table (which can include integer-°C
    sources madis_fsl2/nws_obs). Fall back to provided `running_min` when
    METAR data is missing.

    Without station/climate_date OR with flag off, behaves as the original
    half-up int-round helper (or no-op).

    Half-up rounding matches NWS CLI convention. Returns None if input None.

    Defensive: if METAR rmin > running_min + 0.5°F (METAR colder than aggregate
    by less than 0.5°F is fine; warmer is suspicious — running_min is
    lowest-wins so aggregate <= METAR is unexpected), prefer running_min.
    """
    if running_min is None:
        return None
    if not USE_CLI_ALIGNED_RMIN:
        return float(running_min)

    # Try METAR-precision lookup first when station+date provided
    if station and climate_date:
        rm_metar = _get_metar_running_min(station, climate_date)
        if rm_metar is not None:
            # Defensive: METAR shouldn't be materially colder than aggregate.
            # Aggregate min uses lowest-wins across sources, so it should be
            # <= METAR min. If METAR > aggregate + 0.5, something's off — use
            # aggregate.
            if rm_metar > float(running_min) + 0.5:
                return float(int(float(running_min) + 0.5))
            # Half-up round METAR to integer °F (CLI convention)
            return float(int(rm_metar + 0.5))

    # Fallback: simple int-round of provided running_min
    return float(int(float(running_min) + 0.5))


# NEW LOW Discord alerts (2026-04-30) — mirror of V1/V2 max-bot's "NEW HIGH"
# pattern. Min-bot doesn't write running_min itself (obs-pipeline does), so we
# poll here once per scan and emit on a downward step. State is per-station,
# (cd, last_rm_seen). A new climate day resets the baseline (no alert on the
# very first obs of a new cd, since rm there is "highest seen so far" in the
# new day, not actually a low). Threshold 0.1°F filters float noise; the
# integer-rounded `iem_currents` source rounds to whole °F so an actual new
# low always crosses 0.1°F.
#
# Persistence (2026-05-03): state previously was in-memory only, so a restart
# wiped the baseline and the first cycle silently absorbed the current rm
# (which could be a corrupt value from an upstream bug). Now persisted to
# `last_rm_seen.json` so a restart re-reads the prior baseline and a real
# regression still triggers an alert.
NEW_LOW_THRESHOLD_F = 0.1


def _load_last_rm_seen() -> dict[str, tuple[str, float]]:
    """Load persisted baseline from `last_rm_seen.json`. Returns {} on first
    run, missing file, or any parse/IO error — file is a soft cache. Per-entry
    failures are silently skipped so one malformed row can't drop the rest."""
    try:
        if not LAST_RM_SEEN_FILE.exists():
            return {}
        with open(LAST_RM_SEEN_FILE) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, tuple[str, float]] = {}
    for st, value in raw.items():
        if not isinstance(st, str):
            continue
        try:
            cd, rm = value
        except (TypeError, ValueError):
            continue
        if not isinstance(cd, str):
            continue
        try:
            out[st] = (cd, float(rm))
        except (TypeError, ValueError):
            continue
    return out


def _save_last_rm_seen() -> None:
    """Persist `_last_rm_seen` atomically. Tuples → 2-element lists in JSON."""
    try:
        tmp = LAST_RM_SEEN_FILE.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            json.dump(
                {st: [cd, rm] for st, (cd, rm) in _last_rm_seen.items()},
                f, indent=2, sort_keys=True,
            )
        os.replace(tmp, LAST_RM_SEEN_FILE)
    except OSError as e:
        # Soft cache — log but don't crash the scan loop.
        try:
            log(f"  last_rm_seen save failed: {e}", "warn")
        except Exception:
            pass


_last_rm_seen: dict[str, tuple[str, float]] = _load_last_rm_seen()

# 6-hour rolling summary of running_min across all 20 stations. Fires once
# at startup (so a fresh boot has immediate visibility) and then every 6h.
SUMMARY_INTERVAL_SEC = 6 * 3600
_last_summary_ts: float = 0.0


def _send_running_low_summary() -> None:
    """Post a 'running low' summary for all 20 stations to the Discord
    channel. Each line: <city> (<icao>): X°F  age=Nm  src=...
    Stations with no rm yet are listed as '—'."""
    now_utc = datetime.now(timezone.utc)
    lines = [f"🌡️ **6h Running-Min Summary** ({now_utc.strftime('%Y-%m-%d %H:%M UTC')})"]
    lines.append("```")
    for series, info in CITIES.items():
        station = info["station"]
        label = info["label"]
        tz = info["tz"]
        cd = _climate_date_nws(tz)
        rm = get_running_min(station, cd)
        if rm is None:
            lines.append(f"{station:6s} {label:14s}     —    cd {cd}  (no obs yet)")
            continue
        # Pull the min_obs_time for context
        try:
            conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
            row = conn.execute(
                "SELECT min_obs_time, source FROM running_min WHERE station=? AND climate_date=?",
                (station, cd),
            ).fetchone()
            conn.close()
        except sqlite3.Error:
            row = None
        mot = row[0] if row else None
        src = row[1] if row else "?"
        age_min = ((now_utc.timestamp() - mot) / 60.0) if mot else None
        age_str = f"{age_min:>4.0f}m" if age_min is not None else "  ?"
        cd_short = cd[5:]  # MM-DD
        lines.append(f"{station:6s} {label:14s} {rm:>5.1f}°F cd {cd_short} obs_age={age_str} src={src}")
    lines.append("```")
    discord_send("\n".join(lines))


def _maybe_send_low_summary() -> None:
    """Time-gated wrapper for _send_running_low_summary. Called from scan_cycle.

    2026-05-01: lazy anchor. `_last_summary_ts: float = 0.0` at module init
    used to mean "never sent" — but `now - 0.0 >> SUMMARY_INTERVAL_SEC` so
    every bot restart would trigger an immediate summary on the first scan.
    With ~5 restarts in a single day that's ~5 summaries instead of the
    intended 1 per 6h. Fix: on the first call after a fresh process start
    (when _last_summary_ts == 0.0), record `now` and return WITHOUT sending,
    anchoring the 6h window to bot startup time."""
    global _last_summary_ts
    now = time.time()
    if _last_summary_ts == 0.0:
        # Fresh process — anchor the throttle window, don't send yet.
        _last_summary_ts = now
        return
    if now - _last_summary_ts < SUMMARY_INTERVAL_SEC:
        return
    try:
        _send_running_low_summary()
        _last_summary_ts = now
    except Exception as e:
        log(f"  running-low summary failed: {e}", "warn")


def _check_new_low_alerts() -> None:
    """Poll running_min for all 20 stations; Discord-alert on new daily lows.
    Called from scan_cycle. State persists in module-global `_last_rm_seen`,
    which is also written to `last_rm_seen.json` so the baseline survives
    restarts (otherwise a bug-injected corrupt rm would be silently accepted
    on the first cycle after a restart)."""
    dirty = False
    for series, info in CITIES.items():
        station = info["station"]
        label = info["label"]
        tz = info["tz"]
        cd = _climate_date_nws(tz)
        rm = get_running_min(station, cd)
        if rm is None:
            continue
        prev = _last_rm_seen.get(station)
        if prev is None or prev[0] != cd:
            # First sighting OR new climate day — establish baseline, no alert.
            # On cd rollover, the first obs of the new day is "the only obs so
            # far" and isn't really a "new low" against history.
            _last_rm_seen[station] = (cd, rm)
            dirty = True
            continue
        prev_cd, prev_rm = prev
        if rm < prev_rm - NEW_LOW_THRESHOLD_F:
            drop = prev_rm - rm
            discord_send(
                f"❄️ **NEW LOW** {label} ({station}): "
                f"{prev_rm:.1f}°F → {rm:.1f}°F (Δ −{drop:.1f}°F)"
            )
            _last_rm_seen[station] = (cd, rm)
            dirty = True
        elif rm != prev_rm:
            # rm shouldn't increase given lowest-wins semantics, but absorb
            # source-disagreement edge cases without alerting.
            _last_rm_seen[station] = (cd, rm)
            dirty = True
    if dirty:
        _save_last_rm_seen()


def get_latest_obs(station: str) -> Optional[dict]:
    """Latest observation (temp_f, obs_time) from obs-pipeline — used for
    current-temperature snapshot and model sigma collapse decisions."""
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT temp_f, obs_time, source FROM observations "
            "WHERE station=? AND temp_f IS NOT NULL "
            "ORDER BY obs_time DESC LIMIT 1",
            (station,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        return {"temp_f": float(row[0]), "obs_time": int(row[1]), "source": row[2]}
    except sqlite3.Error as e:
        log(f"  obs-pipeline latest-obs read error: {e}", "warn")
        return None


def get_cli_low(station: str, climate_date: str) -> Optional[int]:
    """Read CLI settlement low (if published) from obs-pipeline cli_reports."""
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT low_f FROM cli_reports WHERE station=? AND climate_date=? "
            "ORDER BY issued_time DESC LIMIT 1",
            (station, climate_date),
        ).fetchone()
        conn.close()
        return int(row[0]) if row and row[0] is not None else None
    except sqlite3.Error as e:
        log(f"  cli_low read error: {e}", "warn")
        return None


# Buffer (hours) after climate-day end before we trust an obs-pipeline CLI as
# final. NWS publishes the morning-after climate summary at ~01:00-03:00 LST,
# covering the full midnight-to-midnight climate day; intermediate (noon, 4 PM,
# 5 PM, 10 PM) reports cover the day SO FAR and can be wildly off for min-temp
# markets when late-evening cooling drives a new daily min. The 4 PM partial is
# issued 8+ hours BEFORE climate-day end (negative offset from threshold), so a
# 1 h buffer keeps phantoms blocked while admitting the 01-03 AM morning-after.
# Was 6 h pre-2026-05-13 which blocked every morning-after CLI and routed 100%
# of settlements through the kalshi fallback (losing cli_low metadata).
CLI_FINAL_BUFFER_H = 1


def _cli_is_final(station: str, climate_date: str, tz_str: Optional[str]) -> bool:
    """Return True only if the latest CLI for (station, climate_date) was issued
    AFTER climate_date_end_LST + CLI_FINAL_BUFFER_H. That guarantees the report
    is the morning-after summary, not a partial intra-day reading.

    Why (2026-04-29 phantom-settlement bug):
      Bot fired SETTLED WIN +$5.22 on KXLOWTSATX-26APR29-T73 from a 4 PM CDT
      partial CLI showing low=77 ("VALID AS OF 0400 PM LOCAL TIME"). The
      climate day ran until midnight CDT; market priced 89% NO; final CLI
      issued the next morning will reflect overnight cooling. Trusting partials
      caused the phantom WIN.
    """
    if not tz_str:
        return False
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        row = conn.execute(
            "SELECT issued_time FROM cli_reports WHERE station=? AND climate_date=? "
            "AND low_f IS NOT NULL ORDER BY issued_time DESC LIMIT 1",
            (station, climate_date),
        ).fetchone()
        conn.close()
        if not row:
            return False
        issued_epoch = int(row[0])
        tz = ZoneInfo(tz_str)
        # climate_date end = next-day 00:00 LST
        cd = datetime.strptime(climate_date, "%Y-%m-%d").replace(tzinfo=tz)
        cd_end_utc = (cd + timedelta(days=1)).astimezone(timezone.utc)
        threshold_epoch = int(cd_end_utc.timestamp()) + CLI_FINAL_BUFFER_H * 3600
        return issued_epoch >= threshold_epoch
    except (sqlite3.Error, ValueError, KeyError) as e:
        log(f"  _cli_is_final read error: {e}", "warn")
        return False


# ─── Recent-CLI history (used by MAX_EDGE NBP-consistency bypass) ─────────
RECENT_CLI_DAYS = 7
RECENT_CLI_MIN_SAMPLES = 3
NBP_CONSISTENCY_BUFFER_F = 2.0

# 2026-05-02: stations where the mp-range bypass is DISABLED. Backtest of
# every historical bypass-fired entry (n=23, 19 settled) showed the bypass
# is net loser overall (-$51.42, 10W:9L) and the loss concentrates entirely
# on coastal / marine-layer stations:
#
#   COASTAL: 3W : 6L, net -$91.87 (incl. 4 of the bot's biggest hard_stops:
#            KLAX-26MAY01 -$29, KHOU-26APR30 -$29, KSFO-26MAY01 -$24,
#            KLAX-26MAY02 -$24)
#   INLAND:  7W : 3L, net +$40.45
#
# Mechanism: the bypass condition "NBP μ within ±2°F of last-7d CLI range"
# is meaningless on high-variance stations whose CLI range spans 8-12°F.
# The buffer expands the "consistency window" to 12-16°F — virtually any
# forecast lands inside it, turning the gate into a no-op rubber stamp.
# Inland stations with tight 4-6°F ranges have a meaningful test.
#
# Removing the bypass for these 9 stations preserves +$40 inland net while
# eliminating the -$92 coastal drain. The list maps to the same 9 stations
# called out in min_bot σ-mult / NBP-d0-override discussions of marine-layer
# microclimates. See project_min_bot_mp_range_bypass_coastal_skip_20260502.md.
COASTAL_NO_MPBYPASS_STATIONS = frozenset({
    "KLAX",   # downtown LA marine layer
    "KSFO",   # San Francisco Bay
    "KSEA",   # Seattle Sound
    "KMIA",   # Miami coastal
    "KHOU",   # Houston Gulf
    "KMSY",   # New Orleans Gulf
    "KNYC",   # NYC coastal
    "KPHL",   # Philadelphia coastal-ish
    "KBOS",   # Boston coastal
    "KMDW",   # 2026-05-03: added — Chicago Midway. Lake Michigan microclimate
              # behaves like coastal: variable lake-breeze effects on overnight
              # lows that NBP CLI-range buffer doesn't capture. Triggered by
              # CHI-26MAY03-B44.5 hard-stop -$24.60 (entered post-COASTAL fix
              # because CHI wasn't on the list). 8-trade audit since 2026-04-25:
              # CHI net -$17.07 / -52% ROI with 2 hard-stops, similar magnitude
              # to other coastal bleeders.
})

# 2026-05-03: floor-mu cushion gate for known marine-cold-bias cities.
# Audit n=9 BUY_NO B-bracket d-0+d-1 trades since 2026-04-25 on these 4 stations:
# every single one had floor-mu gap ≤ 2°F; 6 of 9 lost (-$117 net). Lift
# from filter at threshold 2.0°F: +$117.16 / robust +$39.68 / helps:hurts 3:0.
# Mechanism-validated by control: same filter on non-coastal HURTS by -$82,
# confirming this is coastal-specific (NBP under-predicts overnight lows
# 1-2°F when marine layer / Gulf moisture moderates the airmass).
# Narrower than COASTAL_NO_MPBYPASS_STATIONS (which is 10 stations) — only
# the 4 here showed consistent coastal cold-bias on d-0/d-1 BUY_NO.
_COASTAL_TIGHT_FLOOR_ENABLED = False  # 2026-05-06: disabled. 5/4-5/6 audit found
                                       # 6 winners blocked / 1 loser — gate is regime-flipped.
                                       # Original 5/3 backtest n=9 didn't generalize (cool-front
                                       # regime broke the marine-cold-bias premise). Predicate
                                       # preserved for fresh sliding-window re-enable. Re-evaluate
                                       # ~2026-05-13.
COASTAL_TIGHT_FLOOR_STATIONS = frozenset({"KLAX", "KSFO", "KMIA", "KHOU"})
COASTAL_TIGHT_FLOOR_MIN_GAP_F = 2.1  # 2026-05-03: bumped 2.0 → 2.1 to include
                                       # gap=2.0 borderline cases. Re-audit on
                                       # forward sample showed 2.1 lifts +$10
                                       # vs 2.0 by catching the 2 open MAY03
                                       # trades (LAX/SFO both projected losers
                                       # via CLI integer rounding) at the cost
                                       # of 1 settled winner (SFO-MAY02 +$15.30).


def get_recent_cli_range(station: str, days: int = RECENT_CLI_DAYS,
                          before_date: Optional[str] = None) -> Optional[tuple[float, float]]:
    """Return (min_low, max_low) for the station's CLI lows over the last `days`
    climate days, optionally only looking *before* `before_date` (for back-tests
    or to avoid leaking the position's own settlement back into the consistency
    window). Returns None if fewer than `RECENT_CLI_MIN_SAMPLES` distinct
    climate days are available — conservative: when we lack data, MAX_EDGE
    still bites."""
    try:
        conn = sqlite3.connect(f"file:{OBS_DB_PATH}?mode=ro", uri=True, timeout=2.0)
        if before_date is not None:
            rows = conn.execute(
                "SELECT climate_date, low_f FROM cli_reports "
                "WHERE station=? AND low_f IS NOT NULL AND climate_date < ? "
                "ORDER BY climate_date DESC",
                (station, before_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT climate_date, low_f FROM cli_reports "
                "WHERE station=? AND low_f IS NOT NULL "
                "ORDER BY climate_date DESC",
                (station,),
            ).fetchall()
        conn.close()
    except sqlite3.Error:
        return None
    seen: set = set()
    lows: list[float] = []
    for cd, low in rows:
        if cd in seen:
            continue
        seen.add(cd)
        lows.append(float(low))
        if len(lows) >= days:
            break
    if len(lows) < RECENT_CLI_MIN_SAMPLES:
        return None
    return (min(lows), max(lows))


def _nbp_consistent_with_recent_cli(opp: dict,
                                     buffer_f: float = NBP_CONSISTENCY_BUFFER_F) -> bool:
    """True if `opp.mu` lies within (recent_min − buffer, recent_max + buffer)
    for the station's last-7d CLI lows. Used as an mp-range gate bypass: when
    the forecast aligns with what's actually been happening at the station,
    extreme model_prob values (e.g. mp < 0.15 on cheap BUY_NO bets where μ is
    clearly outside the bracket) are trustworthy. Backtest 2026-04-29: 3/3
    such cases won. (Originally also bypassed MAX_EDGE; that bypass was rolled
    back after backtest showed 5/5 BUY_NO MAX_EDGE-bypass losses where μ was
    at-or-near the bracket boundary.)

    Excludes the position's own climate_date from the lookback so back-test
    scenarios don't leak. Returns False on insufficient history (< 3 days),
    keeping MAX_EDGE active in that case.

    2026-05-02: returns False (bypass disabled) for stations in
    COASTAL_NO_MPBYPASS_STATIONS — see constant docstring for the 19-trade
    backtest showing the bypass was net-negative on coastal/marine-layer
    stations whose recent-CLI range spans 8-12°F (range × ±2°F buffer makes
    the consistency test trivially true). Inland stations with tight 4-6°F
    ranges still get the bypass."""
    mu = opp.get("mu")
    station = opp.get("station")
    if mu is None or not station:
        return False
    if station in COASTAL_NO_MPBYPASS_STATIONS:
        return False
    rng = get_recent_cli_range(station, before_date=opp.get("date_str"))
    if rng is None:
        return False
    cli_min, cli_max = rng
    return (cli_min - buffer_f) <= float(mu) <= (cli_max + buffer_f)


# ═══════════════════════════════════════════════════════════════════════
# NBP FETCHER — NBM Probabilistic bulletins for TNNMN (min mu) + TNNSD (min sigma)
# ═══════════════════════════════════════════════════════════════════════
#
# NBM Probabilistic text bulletins are published every 6h on AWS S3.
# Format: /noaa-nbm-grib2-pds/blend.YYYYMMDD/HH/text/blend_nbstx.tHHz
# Parsed sections include TNNMN (min-temperature mean, °F) and TNNSD
# (standard deviation, °F). Mirrors V2's NBP fetcher for max (TXNMN/TXNSD)
# but with opposite variables.
#
# Cache: 6h. Disk-persisted so restart doesn't lose recent NBP data.

_nbp_cache: dict[str, dict] = {}      # {station: {'date_str': {'mu': f, 'sigma': f, 'fetched': ts}}}
_nbp_cache_lock = threading.Lock()
_nbp_cache_ts: float = 0.0
# Nominal cycle time of the bulletin currently in cache (UTC). Used by the
# HEAD-poll trigger to compute the next-expected cycle URL.
_nbp_cache_cycle_dt: Optional[datetime] = None

NBP_CYCLE_HOURS = (1, 7, 13, 19)        # NBP Probabilistic full cycles (TXNMN populated)
NBP_PUBLISH_LATENCY_MIN = 70            # don't probe S3 before cycle+70min — model still running
NBP_HARD_STALE_SEC = 8 * 3600           # safety net: refresh unconditionally if cache > 8h old

# 2026-05-07: V2-port. NCEP publishes blend bulletins ~90s ahead of the AWS S3
# mirror, and during S3 outages (like the 5/7 01Z gap) NCEP can have data S3
# doesn't yet. We HEAD both endpoints in parallel and use whichever returns
# 200 first.
_NBP_S3_BASE = "https://noaa-nbm-grib2-pds.s3.amazonaws.com"
_NBP_NCEP_BASE = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/blend/prod"

# Last-Modified header per URL — avoids re-downloading the same 33MB bulletin
# when the cycle hasn't actually advanced (typical fall-through when the
# next-expected cycle is overdue from upstream).
_nbp_last_modified: dict[str, str] = {}

# NCEP nomads serves blend bulletins behind a 302 redirect. httpx's
# module-level functions and `httpx.request(...)` do NOT honor
# follow_redirects=True (only `httpx.Client` does), so we need a dedicated
# client for the NBP path. Used for both HEAD probes and bulletin GETs.
_NBP_HTTP = httpx.Client(timeout=30.0, follow_redirects=True)

def _load_nbp_cache_from_disk() -> None:
    global _nbp_cache, _nbp_cache_ts, _nbp_cache_cycle_dt
    try:
        if NBP_CACHE_FILE.exists():
            with open(NBP_CACHE_FILE) as f:
                data = json.load(f)
            cycle_iso = data.get("cycle_dt")
            cycle_dt = None
            if cycle_iso:
                try:
                    cycle_dt = datetime.fromisoformat(cycle_iso)
                    if cycle_dt.tzinfo is None:
                        cycle_dt = cycle_dt.replace(tzinfo=timezone.utc)
                except Exception:
                    cycle_dt = None
            with _nbp_cache_lock:
                _nbp_cache = data.get("cache", {})
                _nbp_cache_ts = float(data.get("ts", 0.0))
                _nbp_cache_cycle_dt = cycle_dt
            log(f"  NBP cache loaded: {sum(len(v) for v in _nbp_cache.values())} entries")
    except Exception as e:
        log(f"  NBP cache load failed (non-critical): {e}", "warn")


def _save_nbp_cache_to_disk() -> None:
    try:
        with _nbp_cache_lock:
            cycle_iso = _nbp_cache_cycle_dt.isoformat() if _nbp_cache_cycle_dt else None
            snap = {"cache": dict(_nbp_cache), "ts": _nbp_cache_ts, "cycle_dt": cycle_iso}
        _atomic_write_json(NBP_CACHE_FILE, snap)
    except Exception as e:
        log(f"  NBP cache save failed: {e}", "warn")


def _nbp_parse_bulletin(text: str, cycle_hour: int, bulletin_date: str) -> dict[str, dict]:
    """Parse NBP text bulletin — extract TXNMN + TXNSD rows per station, indexed
    at 12z UTC columns (which carry the MIN-temp values — 00z = max, 12z = min).

    NBM NBP bulletin format (learned 2026-04-24 by inspecting live bulletin):
      - Station block begins with ` {STATION}    NBM V4.3 NBP GUIDANCE ...`
      - Header row: SAT 25 | SUN 26 | ... (day-of-week + date)
      - UTC row: `12| 00  12| 00  12|...` — 12z=MinT, 00z=MaxT
      - FHR row: forecast hours from bulletin init
      - TXNMN row: temperature mean °F (MIN at UTC==12, MAX at UTC==00)
      - TXNSD row: standard deviation °F (same column semantics)

    Date mapping: the 12z forecast for "SAT 25" (day name + date header) is
    the MIN for SAT 25. We derive the target date from the bulletin init +
    FHR (adjusted back 1h since the 12z forecast hour lands at the START of
    morning, which is still the same calendar day as the overnight low).

    Returns: {station: {date_str: {'mu': f, 'sigma': f}}}.
    """
    result: dict[str, dict] = {}
    bull_dt = datetime.strptime(bulletin_date, "%Y%m%d")
    stations_to_find = set(NBP_STATION_MAP.values())

    for station in stations_to_find:
        idx = text.find(f" {station} ")
        if idx < 0:
            continue
        start = max(0, text.rfind("\n", 0, idx) + 1)
        block_lines = text[start:start + 2000].splitlines()

        all_vals: dict[str, list[Optional[int]]] = {}
        for line in block_lines:
            parts = line.split("|")
            if len(parts) < 2:
                continue
            field = line[:7].strip()
            if not field or field in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"):
                continue
            vals: list[Optional[int]] = []
            pre = parts[0][7:].strip()
            if pre:
                for tok in pre.split():
                    try:
                        vals.append(int(tok))
                    except ValueError:
                        vals.append(None)
            for p in parts[1:]:
                for tok in p.strip().split():
                    try:
                        vals.append(int(tok))
                    except ValueError:
                        vals.append(None)
            all_vals[field] = vals

        utc_vals = all_vals.get("UTC", [])
        fhr_vals = all_vals.get("FHR", [])
        # TXNMN + TXNSD are used for BOTH max and min — disambiguated by UTC col
        txnmn = all_vals.get("TXNMN", [])
        txnsd = all_vals.get("TXNSD", [])

        # 12z columns carry the MinT values we want.
        mint_indices = [i for i, v in enumerate(utc_vals) if v == 12]

        station_data: dict[str, dict] = {}
        for i in mint_indices:
            if i >= len(fhr_vals) or fhr_vals[i] is None:
                continue
            fhr = fhr_vals[i]
            # Target date for the 12z forecast: bulletin init + (cycle_hour +
            # fhr) hours. The 12z forecast represents the overnight low whose
            # calendar date aligns with the DAY-OF-WEEK header in the block.
            # Simplest correct mapping: UTC 12z for MinT → the calendar date
            # of the target hour in LST (i.e. the day you'd wake up on).
            target_dt = bull_dt + timedelta(hours=cycle_hour + fhr)
            # The 12z forecast time is 7am CDT / 6am CST / 5am MST / 4am PST —
            # well inside the climate day it belongs to. No offset needed.
            target_date = target_dt.strftime("%Y-%m-%d")

            mu = txnmn[i] if i < len(txnmn) else None
            sigma = txnsd[i] if i < len(txnsd) else None
            if mu is None or sigma is None:
                continue
            station_data[target_date] = {
                "mu": float(mu),
                "sigma": float(sigma) if sigma > 0 else 2.5,
            }
        if station_data:
            result[station] = station_data
    return result


def _nbp_parallel_head(ncep_url: str, s3_url: str) -> tuple[Optional[str], Optional[str]]:
    """HEAD NCEP + S3 in parallel; first 200 wins. Returns (winner_url, last_modified)
    or (None, None) if both fail. NCEP typically publishes ~90s before S3 mirror,
    and during S3 outages NCEP can have data S3 doesn't yet.

    Uses the module-level `_NBP_HTTP` client with follow_redirects=True. NCEP
    nomads returns 302 for blend URLs and httpx's bare `httpx.head()` /
    `httpx.request()` calls do NOT actually follow redirects — only
    `httpx.Client` honors follow_redirects=True. Using bare functions here
    silently 302s and falls back to S3-only."""
    def _head(url):
        try:
            r = _NBP_HTTP.head(url, timeout=3.0)
            if r.status_code in (200, 206):
                return url, r.headers.get("last-modified", "")
        except Exception:
            pass
        return None
    tp = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="nbp-head")
    try:
        futs = {tp.submit(_head, u): u for u in (ncep_url, s3_url)}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=4):
                res = fut.result()
                if res:
                    return res
        except concurrent.futures.TimeoutError:
            pass
        return None, None
    finally:
        tp.shutdown(wait=False)


def _nbp_fetch_latest_bulletin() -> Optional[tuple[str, int, str]]:
    """Fetch the most recent NBP bulletin text. Tries NCEP first, falls back to S3.
    Returns (text, cycle_hour, bulletin_date_YYYYMMDD) on new download, None
    otherwise. Uses Last-Modified caching so a fall-through to an older cycle
    that's already in cache doesn't redundantly re-download 33MB.

    Uses blend_nbptx (longer-range bulletin with TXNMN/TXNSD). NBM Probabilistic
    cycles run 01/07/13/19 UTC; 06z/18z are short-range and omit TXNMN.

    2026-05-07: V2-port — added NCEP-primary fallback + Last-Modified caching.
    Pre-port the function was S3-only and S3 mirror lag (e.g. 5/7 01Z late by
    >4h) would force the bot to serve a stale fallback cycle even though NCEP
    had the new data.
    """
    global _nbp_cache_ts
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    cycle_order = [
        (today, "19"), (today, "13"), (today, "07"), (today, "01"),
        (yesterday, "19"), (yesterday, "13"), (yesterday, "07"),
    ]
    for d, h in cycle_order:
        ncep_url = f"{_NBP_NCEP_BASE}/blend.{d}/{h}/text/blend_nbptx.t{h}z"
        s3_url = f"{_NBP_S3_BASE}/blend.{d}/{h}/text/blend_nbptx.t{h}z"
        winner_url, winner_lm = _nbp_parallel_head(ncep_url, s3_url)
        if not winner_url:
            continue

        # Skip-if-unchanged: same Last-Modified means we already have this
        # bulletin's data in cache. Bump cache_ts as proof-of-life so the
        # stale-cache watcher doesn't false-alarm during the inter-cycle gap.
        if _nbp_last_modified.get(winner_url) == winner_lm and winner_lm:
            with _nbp_cache_lock:
                _nbp_cache_ts = time.time()
            return None

        try:
            r = _NBP_HTTP.get(winner_url, timeout=30.0)
            if r.status_code != 200:
                continue
            data = r.text
        except Exception:
            continue
        # Sanity check: TXNMN present (present on 01/07/13/19 cycles).
        if "TXNMN" not in data[:500000]:
            continue
        if len(data) < 1000000:
            continue
        _nbp_last_modified[winner_url] = winner_lm
        src = "NCEP" if "nomads" in winner_url else "S3"
        log(f"  NBP: fetched blend.{d}/{h} ({len(data)//1024}KB) via {src}")
        return data, int(h), d
    return None


# Non-reentrant lock around the full fetch+parse+commit. Held only by the
# thread doing real work; concurrent callers (poller daemon vs scan loop)
# bail out instead of double-fetching the 33MB bulletin.
_nbp_refresh_lock = threading.Lock()


def refresh_nbp_forecasts() -> None:
    """Fetch NBP bulletin + parse + update cache. Idempotent; call periodically.

    Non-blocking guard: if another thread is already inside this function,
    return immediately. The 33MB GET takes 1–3s and we don't want the
    poller-daemon and the scan-loop to race-fetch the same cycle.
    """
    if not _nbp_refresh_lock.acquire(blocking=False):
        return
    try:
        global _nbp_cache, _nbp_cache_ts, _nbp_cache_cycle_dt
        fetched = _nbp_fetch_latest_bulletin()
        if not fetched:
            log("  NBP: fetch failed — keeping stale cache", "warn")
            return
        text, cycle_hour, bulletin_date = fetched
        parsed = _nbp_parse_bulletin(text, cycle_hour, bulletin_date)
        cycle_dt = datetime(
            int(bulletin_date[:4]), int(bulletin_date[4:6]), int(bulletin_date[6:8]),
            cycle_hour, tzinfo=timezone.utc,
        )
        with _nbp_cache_lock:
            for st, dates in parsed.items():
                _nbp_cache.setdefault(st, {}).update(dates)
            _nbp_cache_ts = time.time()
            _nbp_cache_cycle_dt = cycle_dt
        _save_nbp_cache_to_disk()
        log(f"  NBP: parsed {len(parsed)} stations (cycle {bulletin_date}/{cycle_hour:02d}z)")
    finally:
        _nbp_refresh_lock.release()


def _nbp_next_cycle_available() -> bool:
    """HEAD-poll trigger for refresh_nbp_forecasts().

    Returns True iff the bot should attempt a refresh this scan. Logic:
      - Cold start (no cycle in cache): True (let _nbp_fetch_latest_bulletin walk back).
      - Cache is hard-stale (>8h): True (safety net for stuck pointer / S3 outage).
      - Less than 70 min since next-expected cycle nominal time: False (model still running).
      - Otherwise: HEAD-probe the next-expected cycle URL on S3.
        Return True iff S3 returns 200.

    NBP cycles run 01/07/13/19 UTC. Typical S3 publish latency 75–90 min.
    HEAD probes are ~50 bytes; ~30 probes per cycle in steady state.
    """
    with _nbp_cache_lock:
        last_cycle = _nbp_cache_cycle_dt
        last_ts = _nbp_cache_ts

    # Cold start — fall through to existing latest-cycle walkback in the fetcher.
    if last_cycle is None or last_ts <= 0:
        return True

    # Hard-stale safety net — covers stuck pointer or extended S3 outage.
    if (time.time() - last_ts) > NBP_HARD_STALE_SEC:
        return True

    next_cycle = last_cycle + timedelta(hours=6)
    elapsed_min = (datetime.now(timezone.utc) - next_cycle).total_seconds() / 60.0

    # Future cycle (clock skew or wrong pointer) — don't probe.
    if elapsed_min < 0:
        return False

    # Model still running — typical NBM publish is 75–90 min after cycle nominal.
    if elapsed_min < NBP_PUBLISH_LATENCY_MIN:
        return False

    d = next_cycle.strftime("%Y%m%d")
    h = next_cycle.strftime("%H")
    ncep_url = f"{_NBP_NCEP_BASE}/blend.{d}/{h}/text/blend_nbptx.t{h}z"
    s3_url = f"{_NBP_S3_BASE}/blend.{d}/{h}/text/blend_nbptx.t{h}z"
    winner_url, _ = _nbp_parallel_head(ncep_url, s3_url)
    return winner_url is not None


# Tick intervals for the background NBP poller daemon. 5s during the
# active publish window collapses worst-case detection latency from ~60s
# (scan-loop interval) to ~5s. 60s outside the window keeps the loop
# responsive to clock changes and pointer updates without burning resources.
NBP_POLL_TICK_ACTIVE_SEC = 5.0
NBP_POLL_TICK_IDLE_SEC = 60.0
# Active window opens at cycle+60min and closes at cycle+240min. Outside
# the window the daemon idles at IDLE tick. The scan loop's own
# `_nbp_next_cycle_available()` call still covers cycles that publish
# beyond +240min (NCEP outage).
NBP_POLL_WINDOW_START_MIN = 60
NBP_POLL_WINDOW_END_MIN = 240


def _nbp_poll_interval_sec() -> float:
    """Return the tick interval the poller daemon should sleep for.
    Active-window pace (5s) once we're past cycle+60min, idle pace (60s)
    otherwise. Cold start (no cycle pointer yet) ticks at active pace so
    a fresh boot doesn't sit idle for 60s before its first probe."""
    with _nbp_cache_lock:
        last_cycle = _nbp_cache_cycle_dt
    if last_cycle is None:
        return NBP_POLL_TICK_ACTIVE_SEC
    next_cycle = last_cycle + timedelta(hours=6)
    elapsed_min = (datetime.now(timezone.utc) - next_cycle).total_seconds() / 60.0
    if NBP_POLL_WINDOW_START_MIN <= elapsed_min <= NBP_POLL_WINDOW_END_MIN:
        return NBP_POLL_TICK_ACTIVE_SEC
    return NBP_POLL_TICK_IDLE_SEC


def _nbp_poller_loop() -> None:
    """Background thread: HEAD-poll S3 for new NBP cycles independently of
    the scan loop. Detection latency drops from ~scan_interval (60s) to
    ~tick (5s) because we don't have to wait for the next scan to even
    look at S3."""
    log("  NBP poller: thread started")
    while not _shutdown.is_set():
        try:
            if _nbp_next_cycle_available():
                refresh_nbp_forecasts()
        except Exception as e:
            log(f"  NBP poller error: {e}", "warn")
        _shutdown.wait(_nbp_poll_interval_sec())
    log("  NBP poller: thread exiting")


def _start_nbp_poller() -> None:
    """Start the background NBP poller daemon. Idempotent — guarded by a
    module-level flag so repeated calls during deploy / test setup are safe."""
    global _nbp_poller_started
    if _nbp_poller_started:
        return
    _nbp_poller_started = True
    threading.Thread(target=_nbp_poller_loop, name="nbp-poller", daemon=True).start()


_nbp_poller_started: bool = False


def get_nbp_forecast(series: str, date_str: str) -> Optional[dict]:
    """Get cached NBP (mu, sigma) for a series' station on date_str.
    Returns None if not in cache."""
    nbp_station = NBP_STATION_MAP.get(series)
    if not nbp_station:
        return None
    with _nbp_cache_lock:
        return _nbp_cache.get(nbp_station, {}).get(date_str)


# ═══════════════════════════════════════════════════════════════════════
# NBM OPEN-METEO FETCHER — hourly min via Open-Meteo's NBM endpoint
# ═══════════════════════════════════════════════════════════════════════

_nbm_om_cache: dict[str, dict] = {}
_nbm_om_cache_lock = threading.Lock()

# 2026-05-03: NBM-OM cache TTL realigned to V2's dynamic pattern.
# Previous 3600s static TTL had two issues:
#   1. Detection latency 0-60min for new NBM cycles (V2 detects in <30s)
#   2. False-positive stale-cache Discord alerts every hour at TTL boundary
# V2 has been running 30s/5s dynamic TTL since 2026-04-29 without rate-limit
# issues on Open-Meteo's free endpoint. Same pattern matches HRRR's
# _hrrr_dynamic_ttl logic 20 lines below.
NBM_OM_TTL_SEC = 30                   # refresh cadence outside the publish window
NBM_OM_TTL_SEC_NEW_RUN_WINDOW = 5     # tighter TTL during HH:35-50 UTC pub window
NBM_OM_STALE_ALERT_SEC = 1800         # 30 min — alert (real refresh failure, not normal latency)
NBM_OM_BLOCK_SEC = 7200               # 2 h — refuse to serve cache beyond this


def _nbm_om_dynamic_ttl() -> int:
    """Current NBM-OM cache TTL based on UTC clock. NBM-OM new cycles land
    in Open-Meteo HH:35-50 UTC each hour; tighter TTL inside that window
    catches new runs within ~5s. Outside the window, 30s catches upstream
    refresh promptly without unnecessary load."""
    minute = datetime.now(timezone.utc).minute
    if 35 <= minute <= 50:
        return NBM_OM_TTL_SEC_NEW_RUN_WINDOW
    return NBM_OM_TTL_SEC

_hrrr_cache: dict[str, dict] = {}
_hrrr_cache_lock = threading.Lock()
# 2026-04-30 (Plan C): tightened from 600s → 60s general with 5s during HH:43-55
# UTC pub window. HRRR new runs land at HH:43-55 each hour from NCEP via Open-Meteo,
# so polling fast inside the window catches the new run within 5s. Outside the
# window the upstream data isn't changing — 60s is plenty. Combined with batched
# fetches (20 cities → 1 request) this matches V2's HRRR detection latency at
# ~5,500 calls/day vs the previous 2,880/day per-city (~2× cost for ~120× speed).
HRRR_TTL_SEC = 60          # general TTL outside HH:43-55
HRRR_TTL_SEC_NEW_RUN_WINDOW = 5  # tighter TTL during HH:43-55 UTC


def _hrrr_dynamic_ttl() -> int:
    """Current HRRR cache TTL based on UTC clock. During HH:43-55 (when a new
    HRRR run is being published from NCEP via Open-Meteo) use the tighter TTL
    to detect new runs ~5s after they're available; otherwise the baseline."""
    minute = datetime.now(timezone.utc).minute
    if 43 <= minute <= 55:
        return HRRR_TTL_SEC_NEW_RUN_WINDOW
    return HRRR_TTL_SEC


def _fetch_open_meteo_batched(url: str, model: str, daily_var: str,
                               cache: dict, cache_lock: threading.Lock,
                               label: str, apikey: Optional[str] = None) -> None:
    """Multi-location batched Open-Meteo fetch. One HTTP request returns data
    for all 20 cities (vs the previous per-city loop = 20× fewer requests).
    Open-Meteo accepts comma-separated lat/lon and returns a list of result
    objects in input order; with timezone=auto each result carries its own
    local timezone."""
    lats = ",".join(str(m["lat"]) for m in CITIES.values())
    lons = ",".join(str(m["lon"]) for m in CITIES.values())
    params = {
        "latitude": lats, "longitude": lons,
        "models": model,
        "daily": daily_var,
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_days": 3,
    }
    if apikey:
        params["apikey"] = apikey
    try:
        r = httpx.get(url, params=params, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"  {label} batched fetch failed: {e}", "warn")
        return
    if not isinstance(data, list):
        data = [data]
    fetched = 0
    now_ts = time.time()
    for series, city_data in zip(CITIES.keys(), data):
        daily = city_data.get("daily", {}) if isinstance(city_data, dict) else {}
        dates = daily.get("time", []) or []
        mins = daily.get(daily_var, []) or []
        by_date = {}
        for d, m in zip(dates, mins):
            if m is not None:
                by_date[d] = {"min_f": float(m), "fetched": now_ts}
        if by_date:
            with cache_lock:
                cache[series] = by_date
            fetched += 1
    if fetched:
        log(f"  {label}: {fetched}/{len(CITIES)} cities (1 batched req)")


def refresh_nbm_om_forecasts() -> None:
    """Fetch real NBM (ncep_nbm_conus) daily min via Open-Meteo customer-api.

    2026-05-07: model identifier corrected from `best_match` to
    `ncep_nbm_conus`. Open-Meteo's `best_match` is its auto-picker — for
    US points short-range it returns HRRR, longer-range GFS/ICON.
    `refresh_nbm_om_forecasts` had been calling `best_match` since
    inception, so for d-0 it returned HRRR-equivalent data, making
    `mu_nbm_om == mu_hrrr` 100% of the time once the 5/5 customer-api
    fix removed the free-tier throttle that was masking it via cache lag.
    V1/V2 use `ncep_nbm_conus` correctly — this aligns min_bot with them.

    2026-05-05: switched URL from `api.open-meteo.com` (free, 10k/day cap)
    to `customer-api.open-meteo.com` + apikey (paid commercial tier).
    """
    _load_open_meteo_key()
    if not _OPEN_METEO_API_KEY:
        log("  NBM-OM disabled: OPEN_METEO_API_KEY not in .env", "warn")
        return
    _fetch_open_meteo_batched(
        "https://customer-api.open-meteo.com/v1/forecast",
        model="ncep_nbm_conus",
        daily_var="temperature_2m_min",
        cache=_nbm_om_cache,
        cache_lock=_nbm_om_cache_lock,
        label="NBM-OM",
        apikey=_OPEN_METEO_API_KEY,
    )


# Open-Meteo paid endpoint — required for HRRR access. Same key as V1/V2.
_OPEN_METEO_API_KEY: Optional[str] = None
_HRRR_DISABLED = False  # set True after first 400 to stop retrying

def _load_open_meteo_key() -> None:
    global _OPEN_METEO_API_KEY
    if _OPEN_METEO_API_KEY is not None:
        return
    try:
        env_txt = ENV_FILE.read_text()
        m = re.search(r"OPEN_METEO_API_KEY=(\S+)", env_txt)
        if m:
            _OPEN_METEO_API_KEY = m.group(1)
            log(f"  Open-Meteo paid key loaded (HRRR enabled)")
    except Exception as e:
        log(f"  Open-Meteo key load failed: {e}", "warn")


def refresh_hrrr_forecasts() -> None:
    """Fetch HRRR hourly temperatures via Open-Meteo's PAID endpoint
    (customer-api.open-meteo.com) using `models=ncep_hrrr_conus`. Computes
    daily min from hourly trajectory. Free endpoint does NOT support HRRR.

    HRRR is high-res (3km), updates hourly, ~48h horizon — best available
    nowcast for upcoming overnight lows.

    2026-04-30 (Plan C): batched 20 cities into 1 request and dynamic TTL
    (60s general / 5s during HH:43-55 UTC pub window). 20× fewer requests
    per refresh, ~5s detection latency for new HRRR runs (vs prior ~10min).
    """
    global _HRRR_DISABLED
    if _HRRR_DISABLED:
        return
    _load_open_meteo_key()
    if not _OPEN_METEO_API_KEY:
        _HRRR_DISABLED = True
        log("  HRRR disabled: OPEN_METEO_API_KEY not in .env", "warn")
        return

    lats = ",".join(str(m["lat"]) for m in CITIES.values())
    lons = ",".join(str(m["lon"]) for m in CITIES.values())
    try:
        r = httpx.get(
            "https://customer-api.open-meteo.com/v1/forecast",
            params={
                "latitude": lats, "longitude": lons,
                "models": "ncep_hrrr_conus", "hourly": "temperature_2m",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "forecast_days": 3,
                "apikey": _OPEN_METEO_API_KEY,
            },
            timeout=15.0,
        )
        if r.status_code in (401, 403):
            _HRRR_DISABLED = True
            log(f"  HRRR disabled: paid endpoint auth failed ({r.status_code})", "warn")
            return
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log(f"  HRRR batched fetch failed: {e}", "warn")
        return
    if not isinstance(data, list):
        data = [data]
    now_ts = time.time()
    fetched_count = 0
    for series, city_data in zip(CITIES.keys(), data):
        if not isinstance(city_data, dict):
            continue
        hourly = city_data.get("hourly", {})
        times = hourly.get("time", []) or []
        temps = hourly.get("temperature_2m", []) or []
        by_date: dict[str, float] = {}
        for t, temp in zip(times, temps):
            if temp is None:
                continue
            date_str = t[:10]
            if date_str not in by_date or temp < by_date[date_str]:
                by_date[date_str] = float(temp)
        if by_date:
            with _hrrr_cache_lock:
                _hrrr_cache[series] = {
                    d: {"min_f": v, "fetched": now_ts}
                    for d, v in by_date.items()
                }
            fetched_count += 1
    if fetched_count:
        log(f"  HRRR: {fetched_count}/{len(CITIES)} cities (1 batched req)")


def get_nbm_om_min(series: str, date_str: str) -> Optional[float]:
    with _nbm_om_cache_lock:
        entry = _nbm_om_cache.get(series, {}).get(date_str)
    if not entry:
        return None
    age_s = time.time() - entry.get("fetched", 0)
    if age_s > NBM_OM_BLOCK_SEC:
        return None  # too stale — refuse to serve
    # 2026-05-03: alert threshold separated from cache TTL. With dynamic TTL
    # of 5-30s, the prior "alert at age > TTL" pattern fired every refresh
    # cycle. STALE_ALERT_SEC (30 min) catches genuine refresh failures
    # without flagging normal sub-minute refresh latency.
    if age_s > NBM_OM_STALE_ALERT_SEC:
        _stale_cache_alert("NBM-OM", series, age_s, NBM_OM_STALE_ALERT_SEC,
                            NBM_OM_BLOCK_SEC)
    return float(entry["min_f"])


def get_hrrr_min(series: str, date_str: str) -> Optional[float]:
    with _hrrr_cache_lock:
        entry = _hrrr_cache.get(series, {}).get(date_str)
    if not entry:
        return None
    age_s = time.time() - entry.get("fetched", 0)
    # 2026-04-30: HRRR has ~hourly publication cadence regardless of cache TTL.
    # Block-served threshold scales off the 60s general TTL but keeps a
    # generous 30-minute window to tolerate a missed pub or a brief
    # Open-Meteo outage. Stale-served alert fires when we cross 5 minutes
    # (the prior "standard" served-stale threshold), so degraded refresh
    # surfaces visibly without flooding Discord during the normal hourly
    # gap between pub windows.
    _block_threshold_s = 1800        # 30 min (was HRRR_TTL_SEC * 3 = 30 min before retune)
    _stale_threshold_s = 300         # 5 min (was HRRR_TTL_SEC = 10 min before retune)
    if age_s > _block_threshold_s:
        return None
    if age_s > _stale_threshold_s:
        _stale_cache_alert("HRRR", series, age_s, _stale_threshold_s,
                            _block_threshold_s)
    return float(entry["min_f"])


# ─── Forecast-cache age helpers (2026-05-02) ─────────────────────────────
# Used purely to enrich entry trade records — captures forecast freshness
# at the moment of trade execution so future bias-correction can attribute
# errors to "stale forecast" patterns (e.g. HRRR stuck on an old run when
# NBP/NBM/obs were already showing a different regime). No behavior change.


def _cache_entry_age_min(cache, lock, key1, date_str):
    """Minutes since per-(key1, date_str) cached forecast was fetched.
    Returns None when no entry exists or no 'fetched' field present.

    Cache shape: `{key1: {date_str: {..., 'fetched': epoch_seconds}}}`.
    `key1` is `station` for NBP cache, `series` for HRRR / NBM-OM caches.
    """
    if not key1 or not date_str:
        return None
    try:
        with lock:
            per_date = cache.get(key1, {})
            entry = per_date.get(date_str) if isinstance(per_date, dict) else None
        if entry and isinstance(entry, dict) and 'fetched' in entry:
            return round((time.time() - float(entry['fetched'])) / 60.0, 1)
    except Exception:
        pass
    return None


def _days_out_int(opp):
    """Integer days from today's NWS climate-day to opp.date_str (per opp.tz).
    0 = today (d-0), 1 = tomorrow (d-1), 2 = day after (d-2), etc.
    Returns None on parse failure.

    Companion to existing `is_today_at_entry` boolean — preserves the d-1
    vs d-2 vs d-3 distinction that the boolean throws away.
    """
    try:
        tz_name = opp.get('tz', 'America/New_York')
        today_str = _climate_date_nws(tz_name)
        today_d = datetime.strptime(today_str, '%Y-%m-%d').date()
        target_d = datetime.strptime(opp['date_str'], '%Y-%m-%d').date()
        return (target_d - today_d).days
    except Exception:
        return None


# ─── Stale-cache fallback alerting ────────────────────────────────────────
# When a forecast cache TTL has expired but we're still inside the fallback
# window (data is served, not blocked), fire a Discord alert so a degraded
# upstream surfaces visibly. Per-(source, series) throttle: 30 min.
# Pattern matches obs-pipeline iem_currents/nws_obs (commit 2227fd7).
_stale_alert_last_ts: dict[str, float] = {}
_STALE_ALERT_THROTTLE_SEC = 1800.0   # 30 min per source/series


def _stale_cache_alert(source: str, series: str, age_s: float,
                       ttl_s: float, fallback_max_s: float) -> None:
    """Fire a rate-limited Discord alert when we serve stale-cache data."""
    key = f"stale:{source}:{series}"
    now = time.time()
    last = _stale_alert_last_ts.get(key, 0.0)
    if now - last < _STALE_ALERT_THROTTLE_SEC:
        return
    _stale_alert_last_ts[key] = now
    age_min = age_s / 60.0
    ttl_min = ttl_s / 60.0
    fb_min = fallback_max_s / 60.0
    msg = (
        f":warning: **STALE CACHE FALLBACK** `{source}` "
        f"`{series}` — age={age_min:.1f}m "
        f"(TTL={ttl_min:.0f}m, fallback_max={fb_min:.0f}m). "
        f"Serving stale data. Throttled 30m per series."
    )
    log(f"  [stale-cache] {source} {series} age={age_min:.1f}m (>{ttl_min:.0f}m TTL)", "warn")
    discord_send(msg)


def _nbp_alert_overdue(cycle_dt: Optional[datetime], age_h: float) -> bool:
    """Return True when NBP cache is stale beyond the natural cycle gap.

    NBP publishes every 6h (01/07/13/19 UTC) with ~`NBP_PUBLISH_LATENCY_MIN`
    publish latency. Our HEAD-poll detects new cycles within seconds. So
    cache age crossing `cycle_dt + 6h + publish_latency + 30min grace`
    means we missed the next cycle — a real refresh failure worth pinging.

    Pre-2026-05-04 this was a flat `age_h > 3.0` check, which fired
    halfway through every normal 6h inter-cycle gap (false positive every
    ~6h). The cycle-aware threshold sits between ~7h30m and ~7h40m post
    last-cycle, well before the 8h hard-stale block.

    Falls back to `age_h > 6.0` when `cycle_dt` is unknown (cold start
    or schema migration), preserving alerting in the absence of cycle
    metadata."""
    if cycle_dt is None:
        return age_h > 6.0
    expected_next_landed = cycle_dt + timedelta(
        hours=6, minutes=NBP_PUBLISH_LATENCY_MIN + 30
    )
    return datetime.now(timezone.utc) > expected_next_landed


def _nbp_staleness_alert(age_h: float) -> None:
    """Fire a rate-limited Discord alert when NBP cache is meaningfully stale.
    Caller must have already determined the cache is overdue (see
    `_nbp_alert_overdue` — cycle-aware: fires only after the next NBP cycle
    should have been published + ingested, ~7h40m after the cached cycle's
    nominal hour). Single global key — NBP cache is one shared cycle, not
    per-series."""
    key = "stale:NBP:cache"
    now = time.time()
    last = _stale_alert_last_ts.get(key, 0.0)
    if now - last < _STALE_ALERT_THROTTLE_SEC:
        return
    _stale_alert_last_ts[key] = now
    msg = (
        f":warning: **STALE CACHE FALLBACK** `NBP` — "
        f"cache age={age_h:.1f}h "
        f"(next cycle overdue past publish-latency + grace — implies "
        f"S3/parse failure). σ inflated. Throttled 30m."
    )
    log(f"  [stale-cache] NBP cache age={age_h:.1f}h (next cycle overdue — refresh likely failing)", "warn")
    discord_send(msg)


# ═══════════════════════════════════════════════════════════════════════
# MIN-TEMP PROBABILITY MODEL
# ═══════════════════════════════════════════════════════════════════════

def _gauss_cdf(x: float) -> float:
    """Standard normal CDF via erf approximation (no scipy dep)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def calc_bracket_probability_min(
    mu: float, sigma: float,
    floor: Optional[float], cap: Optional[float],
    running_min: Optional[float] = None,
    post_sunrise_lock: bool = False,
    *,
    station: Optional[str] = None,
    date_str: Optional[str] = None,
) -> float:
    """P(daily_min falls in bracket [floor, cap]) under Gaussian(mu, sigma),
    with two physics-based refinements:

    1. **running_min as HARD CEILING**: once we've observed a low of X today,
       the final daily min is ≤ X with certainty. If running_min is below cap,
       truncate the distribution above running_min. If running_min ≤ floor,
       the bracket is already guaranteed LOSER (P=0 unless low must be above
       floor; tails below floor resolve T64 = "63° or below" YES, not bracket).

       Bracket semantics: 'B64.5' means 'low between 64 and 65 inclusive'.
       Tail 'T64' means 'low ≤ 63°F'. Tail 'T71' means 'low ≥ 72°F'.

    2. **post-sunrise lock**: once sunrise has passed + obs is warming, the
       min is essentially fixed. Caller sets post_sunrise_lock=True and we
       collapse sigma to 0.5°F residual ASOS-vs-CLI noise.

    Bracket convention (from Kalshi KXLOWT markets):
      - B-brackets are 2°F wide, centered on integer+0.5 (e.g. B64.5 = [64, 65])
      - T-low is ≤ (T_val - 1), e.g. T64 = low ≤ 63
      - T-high is ≥ (T_val + 1), e.g. T71 = low ≥ 72
    Caller passes floor/cap as the actual bounds (B64.5 → floor=64.0, cap=65.0;
    T64 → floor=None, cap=63.5; T71 → floor=71.5, cap=None).
    """
    if sigma <= 0:
        sigma = 0.5

    # 2026-05-06: V1/V2-port. CLI-align running_min using METAR-precision
    # lookup when caller provides station+date_str. All downstream uses
    # (truncation, post-sunrise lock, ceiling guards) see the integer-°F
    # value matching what NWS CLI will settle on. Avoids false BUY_NO
    # OBS_CONFIRMED_LOSER triggers on madis_fsl2/nws_obs integer-°C
    # round-down pollution. No-op when station/date_str not provided.
    if running_min is not None and station and date_str and USE_CLI_ALIGNED_RMIN:
        _aligned_rm = _cli_aligned_rmin(running_min, station, date_str)
        if _aligned_rm is not None:
            running_min = _aligned_rm

    # 2026-05-02 BRACKET MATH FIX (port from V1/V2 2026-04-22; Brier
    # backtest 0.250 → 0.228 on n=398 settled brackets). Kalshi settles
    # on integer °F CLI lows. A B-bracket [floor, cap] wins YES when
    # CLI ∈ {floor, …, cap} (integer set), corresponding to continuous
    # low ∈ [floor − 0.5, cap + 0.5]. Without this buffer, mp at boundary
    # forecasts is systematically too low (e.g. μ=57 vs bracket [58,59]
    # was 12% pre-fix, true rounded probability is ~23% — that gap is
    # exactly what produced LAX/SFO/ATL/NYC/DEN-APR30 catastrophic losses
    # via bypass at directional 20% gate).
    #
    # parse_market_bracket pre-buffers T-tails (T-low cap=val−0.5,
    # T-high floor=val+0.5), so only widen here when BOTH bounds are
    # present (B-bracket case). T-tails would double-buffer otherwise.
    is_b_bracket = floor is not None and cap is not None
    floor_eff = (floor - 0.5) if is_b_bracket else floor
    cap_eff   = (cap   + 0.5) if is_b_bracket else cap

    # Post-sunrise: collapse forecast to the observed running_min with tight
    # residual noise. Skip the truncation conditioning below — the Gaussian
    # centered on rm already captures the remaining uncertainty (ASOS vs CLI
    # sampling / rounding differences). Sigma 1.0°F (was 0.5) gives us a
    # ±1°F obs-vs-CLI window — a 5-min ASOS reading and the CLI 2-min low can
    # disagree by that much, even post-sunrise.
    if post_sunrise_lock and running_min is not None:
        mu = running_min
        sigma = 1.0
        lo_z = ((floor_eff - mu) / sigma) if floor_eff is not None else None
        hi_z = ((cap_eff   - mu) / sigma) if cap_eff   is not None else None
        p_lo = _gauss_cdf(lo_z) if lo_z is not None else 0.0
        p_hi = _gauss_cdf(hi_z) if hi_z is not None else 1.0
        return max(0.0, min(1.0, p_hi - p_lo))

    # Pre-sunrise with running_min: apply truncation AND renormalize.
    # The posterior P(X in [floor, cap] | X ≤ rm) = P(X in [floor, min(cap,rm)]) / P(X ≤ rm)
    #
    # +1°F buffer (2026-04-25, mirrors V1/V2 max-bot fix): obs (5-min METAR)
    # and Kalshi-settlement CLI (2-min averages, integer-rounded) can disagree
    # by up to ~1°F. Treat running_min as `running_min + 1.0` in the
    # "bracket impossible" guard so a bracket cap just barely above our obs
    # isn't ruled out — its true CLI low could still land inside.
    if running_min is not None:
        rm_buffered = running_min + 1.0
        if floor_eff is not None and floor_eff > rm_buffered:
            return 0.0  # bracket entirely above rm even after +1°F buffer — impossible
        effective_cap = cap_eff if (cap_eff is None or rm_buffered >= cap_eff) else rm_buffered
        norm_z = (rm_buffered - mu) / sigma
        norm_denom = _gauss_cdf(norm_z)
        if norm_denom <= 0:
            return 0.0
        lo_z = ((floor_eff - mu) / sigma) if floor_eff is not None else None
        hi_z = ((effective_cap - mu) / sigma) if effective_cap is not None else norm_z
        p_lo = _gauss_cdf(lo_z) if lo_z is not None else 0.0
        p_hi = _gauss_cdf(hi_z)
        prob = (p_hi - p_lo) / norm_denom
        return max(0.0, min(1.0, prob))

    # No running_min constraint — unconditional Gaussian.
    lo_z = ((floor_eff - mu) / sigma) if floor_eff is not None else None
    hi_z = ((cap_eff   - mu) / sigma) if cap_eff   is not None else None
    p_lo = _gauss_cdf(lo_z) if lo_z is not None else 0.0
    p_hi = _gauss_cdf(hi_z) if hi_z is not None else 1.0
    return max(0.0, min(1.0, p_hi - p_lo))


# ═══════════════════════════════════════════════════════════════════════
# BRACKET / MARKET PARSER
# ═══════════════════════════════════════════════════════════════════════

_TAIL_LO = re.compile(r"-T(\d+)$")     # e.g. -T64
_BRACKET = re.compile(r"-B(\d+\.?\d*)$")  # e.g. -B64.5

def parse_market_bracket(ticker: str) -> Optional[dict]:
    """Parse a KXLOWT market ticker → bracket bounds.

    Bracket convention:
      B<val> → [val-0.5, val+0.5] i.e. 2°F wide centered on val.
      Actually from Kalshi subtitles:
        'B64.5' = '64° to 65°' → floor=64, cap=65
      T low-tail:
        'T64' = '63° or below' → floor=None, cap=63.5
      T high-tail:
        'T71' = '72° or above' → floor=71.5, cap=None

    We determine T-direction by position: if the T value is below the event's
    bracket range (first market) it's the LOW tail; if above (last market) it's
    the HIGH tail. Without access to the full event, we make a best guess from
    magnitude.
    """
    m = _BRACKET.search(ticker)
    if m:
        val = float(m.group(1))
        # 'B64.5' bracket goes from (val - 0.5) to (val + 0.5)
        return {"kind": "bracket", "floor": val - 0.5, "cap": val + 0.5, "value": val}
    m = _TAIL_LO.search(ticker)
    if m:
        val = int(m.group(1))
        # Need event context to know if LOW or HIGH tail. Defer; caller decides.
        return {"kind": "tail", "value": val}
    return None


def resolve_tail_bracket(market: dict, all_markets_in_event: list[dict]) -> dict:
    """Given a tail market + the full event's market list, determine whether
    this is the LOW tail or the HIGH tail and return proper floor/cap."""
    bracket = parse_market_bracket(market.get("ticker", ""))
    if not bracket or bracket["kind"] != "tail":
        return bracket or {}
    val = bracket["value"]
    # Find all B-brackets in the event; their range tells us tail direction.
    b_vals: list[float] = []
    for m in all_markets_in_event:
        b = parse_market_bracket(m.get("ticker", ""))
        if b and b["kind"] == "bracket":
            b_vals.append(b["value"])
    if not b_vals:
        # No brackets found; fall back to subtitle inspection
        sub = (market.get("subtitle") or market.get("yes_sub_title") or "").lower()
        if "below" in sub:
            return {"kind": "tail_low", "floor": None, "cap": float(val) - 0.5, "value": val}
        return {"kind": "tail_high", "floor": float(val) + 0.5, "cap": None, "value": val}
    if val <= min(b_vals):
        return {"kind": "tail_low", "floor": None, "cap": float(val) - 0.5, "value": val}
    return {"kind": "tail_high", "floor": float(val) + 0.5, "cap": None, "value": val}


# ═══════════════════════════════════════════════════════════════════════
# KALSHI MARKET DISCOVERY
# ═══════════════════════════════════════════════════════════════════════

_MON_MAP = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
            "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}


def _event_date_from_ticker(event_ticker: str) -> Optional[str]:
    m = re.match(r".*-(\d{2})([A-Z]{3})(\d{2})$", event_ticker)
    if not m:
        return None
    yy, mon_s, dd = m.groups()
    return f"20{yy}-{_MON_MAP.get(mon_s, '01')}-{dd}"


def _quote_cents(mkt: dict, dollars_field: str, cents_field: str) -> Optional[int]:
    """Return a quote in integer cents from a Kalshi /markets entry.

    Kalshi's /markets endpoint populates *_dollars (fraction) but leaves the
    legacy cents-form fields as None; /events?with_nested_markets does the
    opposite (and ships zeros). Prefer the dollars form, fall back to cents,
    return None when neither is set."""
    d = mkt.get(dollars_field)
    if d is not None:
        try:
            return int(round(float(d) * 100))
        except (TypeError, ValueError):
            pass  # noqa: defensive — decimal field unparseable; fall through to cents_field
    c = mkt.get(cents_field)
    if c is not None:
        try:
            return int(c)
        except (TypeError, ValueError):
            return None
    return None


def _fetch_series_markets(series: str) -> Optional[list[dict]]:
    """One Kalshi call: /trade-api/v2/markets?series_ticker=X&status=open.
    Returns the raw markets list or None on error.

    Mirrors V2's discover path (obs-pipeline-bot/kalshi_weather_bot_v2.py).
    The /events?with_nested_markets path returns markets with all quote
    fields = None and is unsuitable for trading."""
    try:
        data = kalshi_get("/trade-api/v2/markets", {
            "series_ticker": series, "status": "open", "limit": 200,
        })
        return data.get("markets", [])
    except Exception as e:
        log(f"  market discovery failed for {series}: {e}", "warn")
        return None


def _overlay_ws_bbo(markets: list[dict]) -> int:
    """Override yes_bid/yes_ask (cents) with fresh WS BBO when available.
    kalshi_ws stores BBO as fractions (0.0–1.0); we convert to cents to
    match the rest of the bot's downstream math."""
    if not USE_KALSHI_WS:
        return 0
    n = 0
    for m in markets:
        tkr = m.get("market_ticker")
        if not tkr:
            continue
        bbo = kalshi_ws.get_bbo(tkr)
        if bbo is None:
            continue
        m["yes_bid"] = int(round(float(bbo["yes_bid"]) * 100))
        m["yes_ask"] = int(round(float(bbo["yes_ask"]) * 100))
        m["_ws_ts"] = bbo["ts"]
        n += 1
    if n > 0:
        try:
            s = kalshi_ws.get_stats()
            log(f"  WS BBO: {n} tickers overlaid (sub={s['subscribed']} cached={s['cached']} deltas={s['deltas']})")
        except Exception:
            pass  # noqa: defensive — WS stats poll is for log only; failure means we just skip the log line
    return n


def discover_markets() -> list[dict]:
    """Pull open markets for all 20 KXLOWT* series. Returns a flat list:
        [{event_ticker, market_ticker, yes_bid, yes_ask, no_bid, no_ask (cents),
          floor, cap, kind, station, series, date_str, subtitle, tz, label, volume}]

    Strategy (V2 pattern, 2026-04-24):
      1. /trade-api/v2/markets?series_ticker=X&status=open per city, in parallel
         (5-at-a-time ThreadPool, 15s as_completed timeout).
      2. Group results by event_ticker, parse each market via the bracket parser.
      3. Quotes come from yes_*_dollars / no_*_dollars (fraction) → cents.
      4. Subscribe every discovered ticker to kalshi_ws and overlay live BBO.

    Replaces the prior /events?with_nested_markets=true path, which Kalshi
    serves with all yes_bid/yes_ask = None."""
    _ensure_kalshi_ws()

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=5, thread_name_prefix="kdisco")
    results: dict[str, Optional[list[dict]]] = {}
    try:
        future_to_series = {ex.submit(_fetch_series_markets, s): s for s in CITIES}
        try:
            for fut in concurrent.futures.as_completed(future_to_series, timeout=15):
                s = future_to_series[fut]
                try:
                    results[s] = fut.result(timeout=0)
                except Exception as e:
                    log(f"  discover thread for {s} crashed: {e}", "error")
                    results[s] = None
        except concurrent.futures.TimeoutError:
            n_pending = sum(1 for f in future_to_series if not f.done())
            for f in future_to_series:
                if not f.done():
                    f.cancel()
                    results[future_to_series[f]] = None
            log(f"  kdisco: {n_pending} series timed out after 15s", "warn")
    finally:
        ex.shutdown(wait=False)

    out: list[dict] = []
    for series, meta in CITIES.items():
        markets = results.get(series)
        if not markets:
            continue
        # Group by event so resolve_tail_bracket sees siblings.
        by_event: dict[str, list[dict]] = {}
        for mkt in markets:
            et = mkt.get("event_ticker", "")
            if et:
                by_event.setdefault(et, []).append(mkt)
        for ev_tk, mkts in by_event.items():
            date_str = _event_date_from_ticker(ev_tk)
            if not date_str:
                continue
            for mkt in mkts:
                tkr = mkt.get("ticker", "")
                br = parse_market_bracket(tkr)
                if not br:
                    continue
                if br["kind"] == "tail":
                    br = resolve_tail_bracket(mkt, mkts)
                    if not br:
                        continue
                out.append({
                    "event_ticker": ev_tk,
                    "market_ticker": tkr,
                    "yes_bid": _quote_cents(mkt, "yes_bid_dollars", "yes_bid"),
                    "yes_ask": _quote_cents(mkt, "yes_ask_dollars", "yes_ask"),
                    "no_bid":  _quote_cents(mkt, "no_bid_dollars",  "no_bid"),
                    "no_ask":  _quote_cents(mkt, "no_ask_dollars",  "no_ask"),
                    "floor": br.get("floor"),
                    "cap": br.get("cap"),
                    "kind": br.get("kind"),
                    "station": meta["station"],
                    "series": series,
                    "date_str": date_str,
                    "subtitle": mkt.get("subtitle") or mkt.get("yes_sub_title", ""),
                    "tz": meta["tz"],
                    "label": meta["label"],
                    "volume": mkt.get("volume") or mkt.get("volume_24h") or 0,
                })

    if out and USE_KALSHI_WS:
        try:
            kalshi_ws.subscribe([m["market_ticker"] for m in out if m.get("market_ticker")])
        except Exception as e:
            log(f"  kalshi_ws.subscribe failed: {e}", "warn")
        _overlay_ws_bbo(out)
    return out


# ═══════════════════════════════════════════════════════════════════════
# OPPORTUNITY FINDER
# ═══════════════════════════════════════════════════════════════════════

def _hours_to_sunrise(tz_name: str, lat: float, lon: float) -> float:
    """Approximate hours until local sunrise. Negative if sunrise already
    passed today; in that case returns hours until tomorrow's sunrise.

    Rough US-mainland approximation: sunrise is at 6 AM local year-round
    within ±1.5h depending on season/latitude. Good enough for the binary
    "past sunrise" decision + sigma collapse.
    """
    now_local = datetime.now(ZoneInfo(tz_name))
    sunrise_today = now_local.replace(hour=6, minute=30, second=0, microsecond=0)
    if now_local > sunrise_today + timedelta(hours=1):
        # Already past morning; next sunrise is tomorrow.
        sunrise_tomorrow = sunrise_today + timedelta(days=1)
        return (sunrise_tomorrow - now_local).total_seconds() / 3600.0
    return (sunrise_today - now_local).total_seconds() / 3600.0


def _is_post_sunrise(tz_name: str) -> bool:
    """True if we've passed today's sunrise by at least 1 hour — at that
    point, the daily min is almost certainly already observed."""
    now_local = datetime.now(ZoneInfo(tz_name))
    return now_local.hour >= 8  # safe blanket: after 8 AM local, min is set


# ─── Bankroll cache for Kelly anchor ──────────────────────────────────────
_cached_bankroll: float = 0.0
_bankroll_cache_ts: float = 0.0


def _get_bankroll_cached() -> float:
    """Return BANKROLL_REF_USD as the Kelly sizing anchor, gated on at least
    one successful Kalshi balance fetch (the cold-start safety from 2026-04-30).
    Live Kalshi cash is fetched and cached every BANKROLL_REFRESH_SEC purely as
    a connectivity probe; once we've confirmed Kalshi is reachable, Kelly sizes
    against BANKROLL_REF_USD, not the shrinking live cash that would otherwise
    pin every trade at the 1-contract floor under heavy open exposure.

    Returns 0.0 if no real balance has ever been cached (cold start or
    persistent Kalshi-auth failure). Callers must treat 0.0 as 'unverified
    bankroll, refuse to size'.

    2026-05-02: switched from returning live cash to BANKROLL_REF_USD. See
    constant definition above for rationale."""
    global _cached_bankroll, _bankroll_cache_ts
    now = time.time()
    if now - _bankroll_cache_ts > BANKROLL_REFRESH_SEC:
        b = get_kalshi_balance()
        if b is not None:
            _cached_bankroll = b
            _bankroll_cache_ts = now
    if _cached_bankroll <= 0:
        return 0.0  # cold-start gate: no successful balance fetch yet
    return BANKROLL_REF_USD


# ─── Obs-confirmed-alive helpers (V2 port) ────────────────────────────────
def _check_obs_confirmed_alive(opp_or_pos: dict) -> bool:
    """True if running_min has decisively settled the bracket in favor of our
    action. Bypasses forecast-based entry gates and triggers SIGNAL_KELLY_MULT
    boost when used at entry; suppresses hard-stop exit when used on an open
    position. Mirror of V2's `_obs_confirmed_dead` for max-bot.

    Decision rules per (action, bracket-shape):
      BUY_NO + B-bracket: rm < floor − OBS_ALIVE_BUFFER_F
        → low went well below bracket; daily low ≤ rm < floor → NO wins
      BUY_NO + T-high (floor=X−0.5, YES if cli ≥ X): rm < floor − BUFFER
        → low went well below threshold → NO wins
      BUY_NO + T-low (cap=X+0.5, YES if cli ≤ X): defer (would need post-sunrise
        confirmation that low won't drop further into YES territory)
      BUY_YES + T-low (cap=X+0.5, YES if cli ≤ X): rm ≤ cap − 1.0
        → low has hit YES territory with +1°F obs/CLI buffer → YES wins
        (rm is monotonically decreasing → once below threshold, stays there)
      BUY_YES + T-high: NOT bypassed. Removed 2026-04-28 after KOKC-26APR28-T56
        phantom: rm=60.08 at 16:04Z bypassed all gates, NBP next-day forecast
        45°F (21°F cold-front cooling forecast before climate-day end). Unlike
        max-bot's analog, the daily MIN can drop AGAIN later in the climate
        day (evening radiative cooling, late cold-front passage); post-sunrise
        does not lock the low. By the time rm IS final (post-LST-midnight)
        the OBS WINNER LOCK has already pulled the market — no legitimate
        bypass window for min-temp T-high BUY_YES.
      BUY_YES + B-bracket: defer (would need post-sunrise + rm in bracket;
        rare and complex; not a typical sweet-spot anyway)"""
    rm = opp_or_pos.get("running_min")
    if rm is None:
        return False
    floor = opp_or_pos.get("floor")
    cap = opp_or_pos.get("cap")
    action = opp_or_pos.get("action")
    # 2026-05-06: cli-align rm using METAR-precision when station+date present
    _station = opp_or_pos.get("station")
    _date_str = opp_or_pos.get("date_str")
    if USE_CLI_ALIGNED_RMIN and _station and _date_str:
        _aligned = _cli_aligned_rmin(rm, _station, _date_str)
        if _aligned is not None:
            rm = _aligned
    rm_f = float(rm)

    if action == "BUY_NO":
        # B-bracket: low went well below bracket
        if floor is not None and cap is not None:
            if rm_f < float(floor) - OBS_ALIVE_BUFFER_F:
                return True
        # T-high (single-bound floor): low went well below threshold
        elif floor is not None and cap is None:
            if rm_f < float(floor) - OBS_ALIVE_BUFFER_F:
                return True
        # T-low: deferred (needs post-sunrise)
    elif action == "BUY_YES":
        # T-low: rm is monotonically decreasing — once it dips into YES
        # territory it cannot recover. Safe to bypass.
        if cap is not None and floor is None:
            if rm_f <= float(cap) - 1.0:
                return True
        # T-high: NO BYPASS for min-temp markets — rm can still drop later in
        # the climate day. See docstring above.
        # B-bracket: deferred (complex; rare)
    return False


# ─── _obs_confirmed_loser (mirror of _alive, pre-empt losing entries) ─────
def _check_obs_confirmed_loser(opp_or_pos: dict) -> bool:
    """True if running_min has already moved against our action — entering
    this position would be buying a known loser. Mirror of
    `_check_obs_confirmed_alive`. The hard-stop catches these post-entry,
    but we'd rather not enter at all (LAX-T54 lost $3.44 in 18 min on
    2026-04-27 12:44 entry — rm was already 57.2°F vs floor 54.5°F when bot
    bought BUY_NO based on HRRR's stale mu=53.1°F forecast).

    Decision rules per (action, bracket-shape):
      BUY_NO + B-bracket: rm IN [floor, cap] → YES is currently winning
      BUY_NO + T-high (floor=X−0.5, YES if cli ≥ X): rm > floor → low above
        threshold; daily low ≤ rm but already in YES territory now
      BUY_NO + T-low (cap=X+0.5, YES if cli ≤ X): rm ≤ cap−0.5 (i.e. cli ≤ X)
        → low has hit YES threshold; rm only goes down → YES wins, NO loses
      BUY_YES + B-bracket:
        - rm < floor → low went below bracket; YES (low in bracket) lost
        - rm > cap + 1.0 AND past local low-lock → low locked above
          bracket; YES (low in bracket) lost (added 2026-05-02 after
          PHIL-26MAY02-B49.5 slipped through)
      BUY_YES + T-high: rm < floor → low went below threshold; YES lost
      BUY_YES + T-low: rm > cap AND post-sunrise → low never reached
        threshold and won't drop further; YES (low ≤ X) lost"""
    rm = opp_or_pos.get("running_min")
    if rm is None:
        return False
    floor = opp_or_pos.get("floor")
    cap = opp_or_pos.get("cap")
    action = opp_or_pos.get("action")
    # 2026-05-06: cli-align rm using METAR-precision when station+date present
    _station = opp_or_pos.get("station")
    _date_str = opp_or_pos.get("date_str")
    if USE_CLI_ALIGNED_RMIN and _station and _date_str:
        _aligned = _cli_aligned_rmin(rm, _station, _date_str)
        if _aligned is not None:
            rm = _aligned
    rm_f = float(rm)

    if action == "BUY_NO":
        # 2026-05-07: hygiene sanity check. If the latest observed temp is
        # well below the bot's rm, rm is stale or sourced differently from
        # live obs (cli-aligned RM can lag raw METAR briefly during active
        # cooling). The LOSER signal is unreliable in that state — defer
        # the entry block. Backtest 2026-04-23..05-07 (n=20 BUY_NO/B
        # first-fires): 1 false-positive caught (KLAX-26MAY06-B56.5,
        # cobs=55.4 < rm=57.0), 0 correct-blocks lost.
        _cobs = _get_current_temp_f(_station)
        if _cobs is not None and _cobs < rm_f - 0.5:
            return False
        if floor is not None and cap is not None:
            if float(floor) <= rm_f <= float(cap):
                return True
        elif floor is not None and cap is None:
            if rm_f > float(floor):
                return True
        elif cap is not None and floor is None:
            if rm_f <= float(cap) - 0.5:
                return True
    elif action == "BUY_YES":
        if floor is not None and cap is not None:
            if rm_f < float(floor):
                return True
            # 2026-05-02: low locked above bracket → YES can't win. Mirror
            # of the BUY_YES T-low rm>cap+post-sunrise check, but for B-
            # brackets. Triggered by PHIL-26MAY02-B49.5 BUY_YES at 6:45 AM
            # EDT where rm=51.8 > cap=50 + buffer; low locked above bracket
            # but bot didn't block (only rm<floor was checked, no symmetric
            # rm>cap branch).
            #
            # Threshold rm > cap + 1.0: CLI integer rounding requires
            # continuous rm >= cap+0.5 to round to cap+1 (definitively
            # outside bracket). +1.0 adds half a degree for ASOS-vs-METAR
            # obs noise.
            #
            # Post-low-lock check uses hour >= 6 local (tighter than
            # _is_post_sunrise's hour >= 8 blanket). Lows typically lock
            # 30-60 min before sunrise (predawn radiative cooling); 6 AM
            # is past low-lock across all US cities/seasons. False-positive
            # cost: at most $5 per BUY_YES (per MAX_BET_BUY_YES_USD cap).
            tz = opp_or_pos.get("tz", "America/New_York")
            try:
                _now_local = datetime.now(ZoneInfo(tz))
                _past_low_lock = _now_local.hour >= 6
            except Exception:
                _past_low_lock = False
            if rm_f > float(cap) + 1.0 and _past_low_lock:
                return True
        elif floor is not None and cap is None:
            if rm_f < float(floor):
                return True
        elif cap is not None and floor is None:
            tz = opp_or_pos.get("tz", "America/New_York")
            if rm_f > float(cap) and _is_post_sunrise(tz):
                return True
    return False


# ─── F2A asymmetry gate (V2 port, BUY_NO only) ────────────────────────────
def _check_f2a_gate(opp: dict) -> Optional[str]:
    """Returns a block-reason string if F2A blocks, None if it passes (or not
    applicable). BUY_NO only. Bypassed by caller when _obs_confirmed_alive."""
    if opp.get("action") != "BUY_NO":
        return None
    mp = float(opp.get("model_prob", 0.0))
    sigma = float(opp.get("sigma", 0.0))
    mu = opp.get("mu")
    if mu is None:
        return None
    mu_f = float(mu)
    floor = opp.get("floor")
    cap = opp.get("cap")

    if mp < F2A_PROB_LO:
        return f"F2A: mp {mp:.0%} < {F2A_PROB_LO:.0%} (price-asymmetry trap)"
    if mp >= F2A_PROB_HI:
        return f"F2A: mp {mp:.0%} ≥ {F2A_PROB_HI:.0%} (model says YES too likely)"
    if sigma < F2A_SIGMA_MIN:
        return f"F2A: sigma {sigma:.1f}°F < {F2A_SIGMA_MIN:.1f}°F (over-confident model)"
    # F2A distance check NOT applied — see constant comment.
    _ = (mu_f, floor, cap)  # silence unused-vars
    return None


# ─── MSG multi-source consensus (V2 port, BUY_NO only) ────────────────────
def _check_msg_gate(opp: dict) -> Optional[str]:
    """Returns a block-reason string if MSG blocks, None otherwise.
    Counts how many of {NBP, HRRR, NBM} forecasts predict YES (loss for us
    on BUY_NO). Per-city tiers: WORST cities require unanimity; standard
    cities allow up to MSG_MAX_CONSENSUS_DEFAULT sources to predict YES.
    Outlier-margin sub-check: any source > MSG_MARGIN_F into YES territory
    triggers a separate block. Bypassed by caller when _obs_confirmed_alive.

    2026-05-10: gated by MSG_ENABLED. Disabled per audit (44% block
    accuracy, n=9 settled). Predicate preserved for fresh re-enable."""
    if not MSG_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    sources = []
    for k in ("mu_nbp", "mu_hrrr", "mu_nbm_om"):
        v = opp.get(k)
        if v is not None:
            sources.append(float(v))
    if len(sources) < 2:
        return None  # insufficient sources to evaluate consensus

    floor = opp.get("floor")
    cap = opp.get("cap")
    series = opp.get("series", "")
    max_consensus = MSG_MAX_CONSENSUS_WORST if series in MSG_WORST_CITIES else MSG_MAX_CONSENSUS_DEFAULT

    yes_count = 0
    yes_outlier_margin = 0.0
    for s in sources:
        in_yes = False
        margin = 0.0
        if floor is not None and cap is not None:
            # B-bracket: YES region is [floor, cap]. Margin = how deep into bracket.
            if float(floor) <= s <= float(cap):
                in_yes = True
                margin = max(s - float(floor), float(cap) - s)
        elif floor is not None and cap is None:
            # T-high: YES region is mu ≥ floor. Margin = depth above floor.
            if s >= float(floor):
                in_yes = True
                margin = s - float(floor)
        elif cap is not None and floor is None:
            # T-low: YES region is mu ≤ cap. Margin = depth below cap.
            if s <= float(cap):
                in_yes = True
                margin = float(cap) - s
        if in_yes:
            yes_count += 1
            yes_outlier_margin = max(yes_outlier_margin, margin)

    if yes_count > max_consensus:
        return (f"MSG: {yes_count}/{len(sources)} sources predict YES "
                f"(max {max_consensus} for {series})")
    if yes_outlier_margin > MSG_MARGIN_F:
        return (f"MSG: outlier {yes_outlier_margin:.1f}°F into YES "
                f"(>{MSG_MARGIN_F:.1f}°F)")
    return None


# ─── COLD_SOURCE_OUTLIER gate (BUY_NO only, d-0 + d-1+) ───────────────────
def _check_cold_source_outlier(opp: dict) -> Optional[str]:
    """Block BUY_NO when picked μ is colder than median(NBP, NBM-OM, HRRR)
    by > COLD_SOURCE_OUTLIER_F. Closes the d-0 hole in H_2_0_DISAGREE_F (which
    is d-1+ only) for the specific pattern where the bot picks the cold-outlier
    source while warmer sources keep the bracket in-play. Bypassed by caller
    when _obs_confirmed_alive. See COLD_SOURCE_OUTLIER_* constants for
    backtest evidence + 2026-05-12 KNYC trigger case."""
    if not COLD_SOURCE_OUTLIER_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    mu = opp.get("mu")
    if mu is None:
        return None
    sources = []
    for k in ("mu_nbp", "mu_nbm_om", "mu_hrrr"):
        v = opp.get(k)
        if v is not None:
            sources.append(float(v))
    if len(sources) < 2:
        return None  # insufficient sources to compute median
    med = statistics.median(sources)
    gap = float(mu) - med  # negative = picked colder than median
    if gap < -COLD_SOURCE_OUTLIER_F:
        return (f"COLD_SOURCE_OUTLIER: picked μ={float(mu):.1f} vs "
                f"median({len(sources)} sources)={med:.1f} "
                f"gap={gap:+.1f}°F < -{COLD_SOURCE_OUTLIER_F:.1f}°F")
    return None


# ─── HRRR_DISSENT gate (BUY_NO B-bracket only, d-0 + d-1+) ────────────────
def _check_hrrr_dissent(opp: dict) -> Optional[str]:
    """Block BUY_NO B-bracket when HRRR predicts low at-or-below cap AND
    both slow models (NBP, NBM-OM) are confidently above cap by
    HRRR_DISSENT_NBP_NBM_BUFFER. Catches the "primary-NBP overconfidence
    with HRRR nowcast dissent" pattern. See HRRR_DISSENT_* constants for
    backtest evidence (n=1 historical fire, h:hu 1:0 perfect, +$30 lift)
    and the 2026-05-15 KXLOWTNYC trigger case (-$59.84)."""
    if not HRRR_DISSENT_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    floor = opp.get("floor")
    cap = opp.get("cap")
    # B-bracket only (both floor + cap not None)
    if floor is None or cap is None:
        return None
    hrrr_v = opp.get("mu_hrrr")
    nbp_v = opp.get("mu_nbp")
    nbm_v = opp.get("mu_nbm_om")
    if hrrr_v is None or nbp_v is None or nbm_v is None:
        return None  # need all 3 sources to evaluate
    cap_f = float(cap)
    hrrr = float(hrrr_v); nbp = float(nbp_v); nbm = float(nbm_v)
    if hrrr > cap_f:
        return None  # HRRR not at-or-below cap → no dissent signal
    slow_min = min(nbp, nbm)
    threshold = cap_f + float(HRRR_DISSENT_NBP_NBM_BUFFER)
    if slow_min < threshold:
        return None  # slow models not confidently above cap → no dissent gap
    return (f"HRRR_DISSENT: HRRR={hrrr:.1f}<=cap={cap_f:.1f} but "
            f"NBP={nbp:.1f}/NBM-OM={nbm:.1f} both >= cap+"
            f"{HRRR_DISSENT_NBP_NBM_BUFFER:.1f}={threshold:.1f} "
            f"(primary-NBP overconfidence + HRRR near-term dissent)")


# ─── HIGH_CONVICTION_DISAG_TRAP gate (BUY_NO only, d-0 + d-1+) ────────────
def _check_high_conviction_disag_trap(opp: dict) -> Optional[str]:
    """Block BUY_NO when model_prob < HIGH_CONVICTION_DISAG_TRAP_MP_MAX AND
    disagreement >= HIGH_CONVICTION_DISAG_TRAP_DISAG_F. Catches the
    "high-conviction-without-justification" trap: low mp triggers max-Kelly
    sizing while active NWP source disagreement signals the σ may not capture
    true uncertainty. Bypassed by caller when _obs_confirmed_alive. See
    HIGH_CONVICTION_DISAG_TRAP_* constants for backtest evidence + 2026-05-12
    AUS/ATL/NYC trigger cases."""
    if not HIGH_CONVICTION_DISAG_TRAP_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    mp_v = opp.get("model_prob")
    if mp_v is None:
        return None
    mp = float(mp_v)
    if mp >= HIGH_CONVICTION_DISAG_TRAP_MP_MAX:
        return None
    disag = float(opp.get("disagreement") or 0.0)
    if disag < HIGH_CONVICTION_DISAG_TRAP_DISAG_F:
        return None
    return (f"HIGH_CONVICTION_DISAG_TRAP: mp={mp:.3f} < "
            f"{HIGH_CONVICTION_DISAG_TRAP_MP_MAX:.3f} AND "
            f"disag={disag:.1f}°F >= "
            f"{HIGH_CONVICTION_DISAG_TRAP_DISAG_F:.1f}°F")


# ─── ENTRY_TIME_WINDOW gate (BUY_NO only) ─────────────────────────────────
def _check_entry_time_window(opp: dict) -> Optional[str]:
    """Block BUY_NO entries fired more than ENTRY_TIME_WINDOW_HOURS_MAX hours
    before the climate-day local midnight start. Caller bypasses when
    _obs_confirmed_alive (those cases always have hpc <= 0 anyway). See
    ENTRY_TIME_WINDOW_* constants for backtest evidence + 5/26 Texas cluster.
    """
    if not ENTRY_TIME_WINDOW_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    date_str = opp.get("date_str")
    tz_name = opp.get("tz")
    if not date_str or not tz_name:
        return None
    try:
        tz = ZoneInfo(tz_name)
        cd_midnight_local = datetime.strptime(date_str, "%Y-%m-%d").replace(
            tzinfo=tz)
        now_utc = datetime.now(tz=timezone.utc)
        hours_pre = (cd_midnight_local - now_utc).total_seconds() / 3600.0
    except Exception:
        return None
    if hours_pre > ENTRY_TIME_WINDOW_HOURS_MAX:
        return (f"ENTRY_TIME_WINDOW: BUY_NO {hours_pre:.1f}h pre-climate-day "
                f"> {ENTRY_TIME_WINDOW_HOURS_MAX:.0f}h cap "
                f"(d-1 lead forecast error exceeds bot σ)")
    return None


# ─── BUY_NO_NBM_IN_BRACKET gate (d-1+ BUY_NO B-bracket only) ──────────────
def _check_nbm_in_bracket(opp: dict) -> Optional[str]:
    """Block d-1+ BUY_NO B-bracket when NBM-OM lies INSIDE the upper edge of
    the YES bracket (NBM ∈ [cap - margin, cap]) AND HRRR is below cap.
    NBM is the bias-corrected slow-model consensus; when it sits right at
    the upper bracket boundary, a small forecast error pushes cli into the
    bracket = BUY_NO loses. HRRR<cap confirms cold direction. Bypassed by
    caller when _obs_confirmed_alive. See BUY_NO_NBM_IN_BRACKET_* constants
    for backtest evidence + 2026-05-17 ship rationale."""
    if not BUY_NO_NBM_IN_BRACKET_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    floor = opp.get("floor")
    cap = opp.get("cap")
    if floor is None or cap is None:
        return None  # B-bracket only
    if opp.get("is_today", False):
        return None  # d-1+ only (d-0 has running_min)
    nbm_v = opp.get("mu_nbm_om")
    hrrr_v = opp.get("mu_hrrr")
    if nbm_v is None or hrrr_v is None:
        return None
    cap_f = float(cap)
    nbm = float(nbm_v); hrrr = float(hrrr_v)
    lo = cap_f - float(BUY_NO_NBM_IN_BRACKET_MARGIN_F)
    if not (lo <= nbm <= cap_f):
        return None
    if hrrr >= cap_f:
        return None
    return (f"BUY_NO_NBM_IN_BRACKET: NBM-OM={nbm:.1f} in "
            f"[{lo:.1f}, {cap_f:.1f}] (slow-model consensus inside upper "
            f"edge of YES bracket) AND HRRR={hrrr:.1f}<cap={cap_f:.1f}")


# ─── BUY_NO_HIGH_MP_TRAP gate (BUY_NO any bracket, all days_out) ──────────
def _check_high_mp_trap(opp: dict) -> Optional[str]:
    """Block BUY_NO when model_prob > BUY_NO_HIGH_MP_TRAP_MP_MAX. Calibration
    audit shows the bot's mp is underestimated at the high end (mp 25-30%
    bucket actually loses 67% vs bot's expected 25-30%; mp 20-25% loses
    30%; sweet spot is mp 15-20% with 12% loss rate). At high bot-mp, the
    "edge" reading is the classic obs-rule trap: market is right, our
    model is wrong. Bypassed by caller when _obs_confirmed_alive. See
    BUY_NO_HIGH_MP_TRAP_* constants for backtest evidence + 2026-05-17 ship
    rationale."""
    if not BUY_NO_HIGH_MP_TRAP_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    mp_v = opp.get("model_prob")
    if mp_v is None:
        return None
    mp = float(mp_v)
    if mp <= float(BUY_NO_HIGH_MP_TRAP_MP_MAX):
        return None
    return (f"BUY_NO_HIGH_MP_TRAP: mp={mp:.3f} > "
            f"{BUY_NO_HIGH_MP_TRAP_MP_MAX:.3f} (high bot-mp on BUY_NO = "
            f"calibration trap, market likely right per RULE #2)")


# ─── BUY_NO_HRRR_IN_BRACKET_WARM gate (BUY_NO B-bracket, all days_out) ────
def _check_hrrr_in_bracket_warm(opp: dict) -> Optional[str]:
    """Block BUY_NO B-bracket when bot bets warm-side (mu > cap) BUT HRRR
    predicts cli will land INSIDE the YES bracket [floor, cap]. Direct
    two-source opposition: bot picked a warmer model for primary mu while
    HRRR (the short-horizon nowcast) points at the bracket. Bypassed by
    caller when _obs_confirmed_alive. See BUY_NO_HRRR_IN_BRACKET_WARM_*
    constants for backtest evidence + 2026-05-19 ship rationale."""
    if not BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    cap = opp.get("cap")
    floor = opp.get("floor")
    if cap is None or floor is None:
        return None  # B-bracket only
    mu_v = opp.get("mu")
    if mu_v is None:
        return None
    cap_f = float(cap); floor_f = float(floor); mu = float(mu_v)
    if mu <= cap_f:
        return None  # warm-side only (mu > cap)
    hrrr_v = opp.get("mu_hrrr")
    if hrrr_v is None:
        return None
    hrrr = float(hrrr_v)
    if not (floor_f <= hrrr <= cap_f):
        return None  # HRRR must be IN the bracket
    return (f"BUY_NO_HRRR_IN_BRACKET_WARM: mu={mu:.1f}>cap={cap_f:.1f} "
            f"(warm-side bet) BUT HRRR={hrrr:.1f} in YES bracket "
            f"[{floor_f:.1f}, {cap_f:.1f}] (nowcast points at bracket = "
            f"direct two-source opposition on risk direction)")

# --- BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP gate (BUY_NO, all bracket kinds) ---
def _check_extreme_market_disagree_low_mp(opp: dict) -> Optional[str]:
    """Block BUY_NO when yes_ask_c >= K * (mp * 100). Catches SATX-class
    outliers where the truncated Gaussian collapses mp to ~0 but the market
    correctly prices YES at 30-50c via signals (forecast trajectory, frontal
    passage, dealer flow) the static mu/sigma model misses. Bypassed by
    caller when _obs_confirmed_alive. See BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_*
    constants for backtest evidence."""
    if not BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    mp_v = opp.get("model_prob")
    ya_v = opp.get("yes_ask")
    if mp_v is None or ya_v is None:
        return None
    mp = float(mp_v)
    if mp <= 0:
        return None
    ya_c = float(ya_v)
    K = float(BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO)
    threshold_c = K * mp * 100.0
    if ya_c < threshold_c:
        return None
    ratio = ya_c / (mp * 100.0)
    return (f"BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP: yes_ask={ya_c:.0f}c >= "
            f"{K:.1f}x mp ({mp*100:.1f}%) = {threshold_c:.1f}c "
            f"(ratio {ratio:.1f}x - market sees signal bot misses)")



# ─── BUY_NO_TAIL_RISK gate (BUY_NO B-bracket, all days_out) ───────────────
def _check_buy_no_tail_risk(opp: dict) -> Optional[str]:
    """Block BUY_NO B-bracket when chosen mu is barely above cap (0 < mu - cap
    < BUY_NO_TAIL_RISK_MU_GAP_F). Forecast sits right at upper bracket boundary
    so any small error puts cli inside [floor, cap] and BUY_NO loses. Bypassed
    by caller when _obs_confirmed_alive. See BUY_NO_TAIL_RISK_* constants for
    backtest evidence + 2026-05-18 ship rationale."""
    if not BUY_NO_TAIL_RISK_ENABLED:
        return None
    if opp.get("action") != "BUY_NO":
        return None
    cap = opp.get("cap")
    floor = opp.get("floor")
    if cap is None or floor is None:
        return None  # B-bracket only
    mu_v = opp.get("mu")
    if mu_v is None:
        return None
    cap_f = float(cap)
    mu = float(mu_v)
    gap = mu - cap_f
    if not (0 < gap < float(BUY_NO_TAIL_RISK_MU_GAP_F)):
        return None
    return (f"BUY_NO_TAIL_RISK: mu={mu:.1f} barely above cap={cap_f:.1f} "
            f"(gap={gap:.2f}F < {BUY_NO_TAIL_RISK_MU_GAP_F:.1f}F — small "
            f"forecast error flips cli into [{floor:.1f}, {cap_f:.1f}])")


# ─── SKIP_MODEL_MARKET_DISAGREE gate (V2 port, both sides) ────────────────
def _check_model_market_disagree(opp: dict) -> Optional[str]:
    """Block when model and market disagree by GAP_MIN with model assigning at
    least MP_FLOOR probability to the losing side. Applied to BUY_NO (V2
    original) and BUY_YES (mirrored for min_bot lows). Bypassed by caller
    when _obs_confirmed_alive. See SKIP_MODEL_MARKET_DISAGREE_* constants for
    backtest evidence."""
    action = opp.get("action")
    mp_v = opp.get("model_prob")
    if action is None or mp_v is None:
        return None
    mp = float(mp_v)
    if action == "BUY_NO":
        yb = opp.get("yes_bid")
        if yb is None:
            return None
        yes_bid_frac = float(yb) / 100.0
        if (mp >= SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR
                and (yes_bid_frac - mp) >= SKIP_MODEL_MARKET_DISAGREE_GAP_MIN):
            return (f"MODEL_MARKET_DISAGREE: BUY_NO mp={mp:.0%} ≥ "
                    f"{SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR:.0%} AND market "
                    f"overpays YES by {yes_bid_frac - mp:.0%} ≥ "
                    f"{SKIP_MODEL_MARKET_DISAGREE_GAP_MIN:.0%}")
    elif action == "BUY_YES":
        nb = opp.get("no_bid")
        if nb is None:
            return None
        no_bid_frac = float(nb) / 100.0
        mp_no = 1.0 - mp
        if (mp_no >= SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR
                and (no_bid_frac - mp_no) >= SKIP_MODEL_MARKET_DISAGREE_GAP_MIN):
            return (f"MODEL_MARKET_DISAGREE: BUY_YES mp_no={mp_no:.0%} ≥ "
                    f"{SKIP_MODEL_MARKET_DISAGREE_MP_FLOOR:.0%} AND market "
                    f"overpays NO by {no_bid_frac - mp_no:.0%} ≥ "
                    f"{SKIP_MODEL_MARKET_DISAGREE_GAP_MIN:.0%}")
    return None


# ─── SKIP_MU_NEAR_FLOOR / SKIP_MU_NEAR_BELOW_BRACKET gate (V1 mirror) ─────
def _check_mu_position_filter(opp: dict) -> Optional[tuple[str, str]]:
    """Block when blended μ sits in a structurally-bad position relative
    to the bracket the bot is betting against.

    (A) tail_high BUY_YES, mu in (floor, floor + 2.0]F  → cold-tail risk
    (B) B-bracket  BUY_NO,  mu in [mid - 2.0, mid)F     → warm-edge risk

    Returns (tag, reason_str) on block, or None.

    Bracket kind is inferred from floor/cap presence so the gate doesn't
    require opp.bracket_kind to be populated:
      - tail_high: floor set,    cap is None    (e.g. T58, "min ≥ 58.5")
      - tail_low:  floor is None, cap set       (e.g. T46, "min ≤ 45.5")
      - bracket:   both floor and cap set       (e.g. B47.5, "min in [47,48)")
    """
    action = opp.get("action")
    mu = opp.get("mu")
    if action is None or mu is None:
        return None
    mu_f = float(mu)
    floor = opp.get("floor")
    cap = opp.get("cap")

    # (A) tail_high BUY_YES with mu just above floor
    if action == "BUY_YES" and floor is not None and cap is None:
        gap = mu_f - float(floor)
        if 0.0 < gap <= MU_NEAR_FLOOR_TAIL_HIGH_MAX_F:
            return ("MU_NEAR_FLOOR",
                    f"BUY_YES tail_high mu={mu_f:.2f} only +{gap:.2f}F above "
                    f"floor={floor:.1f} (≤{MU_NEAR_FLOOR_TAIL_HIGH_MAX_F:.1f}F) "
                    f"— cold-tail risk, actual likely dips below floor")

    # (B) B-bracket BUY_NO with mu just below midpoint
    if action == "BUY_NO" and floor is not None and cap is not None:
        mid = (float(floor) + float(cap)) / 2.0
        gap = mid - mu_f
        if 0.0 < gap <= MU_NEAR_BELOW_BRACKET_MAX_F:
            return ("MU_NEAR_BELOW_BRACKET",
                    f"BUY_NO bracket mu={mu_f:.2f} {gap:.2f}F below mid={mid:.1f} "
                    f"(≤{MU_NEAR_BELOW_BRACKET_MAX_F:.1f}F) "
                    f"— warm-edge tail catches actual in bracket")
    return None


def find_opportunities(markets: list[dict]) -> list[dict]:
    """For each market, compute model_prob, edge vs yes_ask, and return
    opportunities sorted by absolute edge."""
    opps: list[dict] = []
    for m in markets:
        station = m["station"]
        date_str = m["date_str"]
        tz = m["tz"]
        # OBS WINNER LOCK (2026-04-25): if obs-pipeline already has CLI low
        # for this (station, climate-day), settlement is decided. Kalshi may
        # still show the market open in the post-CLI / pre-Kalshi-close window,
        # but our model has no edge — we'd just be buying losers (or wins
        # already priced in). Skip outright.
        if get_cli_low(station, date_str) is not None:
            continue
        # Determine if this is today's or tomorrow's CD from obs-pipeline's POV
        today_cd = _climate_date_nws(tz)
        is_today = (date_str == today_cd)
        # Past-date guard: if Kalshi still shows yesterday's market open and
        # CLI hasn't published yet, we still shouldn't trade — the answer is
        # known but our obs-pipeline lags. _climate_date_nws is per-TZ, so
        # date_str < today_cd really does mean "yesterday or earlier."
        if date_str < today_cd:
            continue
        # Fetch ALL available forecasts so we can log disagreement + blend.
        nbp = get_nbp_forecast(m["series"], date_str)
        nbm = get_nbm_om_min(m["series"], date_str)
        hrrr = get_hrrr_min(m["series"], date_str)
        mu: Optional[float] = None
        sigma: Optional[float] = None
        mu_source = ""
        # Priority:
        #   - day-0 (today): HRRR first (freshest nowcast for upcoming overnight).
        #     HRRR has no sigma, so pair it with NBP's sigma if we have it,
        #     otherwise use a conservative default.
        #   - day-1+ (future): NBP by default. Per-city overrides via
        #     PER_SERIES_D1_PRIMARY (CHI, OKC → HRRR — backed by source-MAE
        #     audit 2026-04-29, see constant comment).
        #   - else fall back to NBM-OM.
        _d0_primary = get_d0_primary(m["series"])
        if (is_today
                and _d0_primary == "nbp"
                and nbp):
            # Per-city d-0 override: NBP beats HRRR on this station's d-0
            # min forecast (auto-select or hardcoded — see auto_primary_selection.json
            # / PER_SERIES_D0_PRIMARY for evidence).
            mu = nbp["mu"]
            sigma = nbp["sigma"]
            mu_source = "nbp_d0_override"
        elif (is_today
              and _d0_primary == "nbm"
              and nbm is not None):
            # 2026-05-05: per-city d-0 override → NBM_OM. Used for cells
            # where NBM materially beats both NBP and HRRR per audit.
            # NBM has no native sigma; reuse NBP's σ if available, else 2.5°F.
            mu = nbm
            sigma = nbp["sigma"] if nbp else 2.5
            mu_source = "nbm_d0_override"
        elif is_today and hrrr is not None:
            mu = hrrr
            sigma = nbp["sigma"] if nbp else 2.5
            mu_source = "hrrr"
        elif (not is_today and USE_D1_CONSENSUS
              and (_cm := _d1_consensus_mu(m["series"], hrrr, nbm, nbp)) is not None):
            mu, sigma, mu_source = _cm
        elif (not is_today
              and get_d1_primary(m["series"]) == "hrrr"
              and hrrr is not None):
            mu = hrrr
            sigma = nbp["sigma"] if nbp else 2.5
            mu_source = "hrrr_d1_override"
        elif (not is_today
              and get_d1_primary(m["series"]) == "nbm"
              and nbm is not None):
            # 2026-05-05: per-city d-1+ override → NBM_OM. KMIA / KDEN
            # at d-1 have NBM as the lowest-MAE source (audit n=4).
            mu = nbm
            sigma = nbp["sigma"] if nbp else 2.5
            mu_source = "nbm_d1_override"
        elif nbp:
            mu = nbp["mu"]
            sigma = nbp["sigma"]
            mu_source = "nbp"
        elif nbm is not None:
            mu = nbm
            sigma = 2.5
            mu_source = "nbm_om"
        if mu is None:
            continue

        # 2026-05-05: per-station bias correction. Reads from
        # shared_tools/data/per_station_bias.json. get_bias() returns None
        # for non-applicable cells (n too low, unstable, |bias| too small).
        # The base source for the lookup is the underlying forecast source
        # — strip the d{0,1}_override suffix so e.g. "nbp_d0_override" maps
        # to "nbp" in the bias table (which is keyed by raw source name).
        bias_applied_F: Optional[float] = None
        if USE_BIAS_CORRECTION and _bias_correction is not None:
            base_src = mu_source
            for suffix in ("_d0_override", "_d1_override", "_om"):
                if base_src.endswith(suffix):
                    base_src = base_src[:-len(suffix)]
                    break
            try:
                _do = 0 if is_today else 1  # min_bot rarely evaluates >d-1; collapse to 1
                _bias = _bias_correction.get_bias(
                    station=m.get("station") or "",
                    source=base_src,
                    days_out=_do,
                    forecast_type="min",
                )
                if _bias is not None and abs(_bias) > 0.0:
                    mu = float(mu) - float(_bias)
                    bias_applied_F = float(_bias)
                    mu_source = f"{mu_source}_bias_corr"
            except Exception:
                pass  # never let bias correction break trade decisions
        # Inflate sigma if HRRR and NBP (or NBM) disagree significantly —
        # disagreement = model uncertainty we haven't captured.
        disagreement = 0.0
        if hrrr is not None and nbp is not None:
            disagreement = max(disagreement, abs(hrrr - nbp["mu"]))
        if hrrr is not None and nbm is not None:
            disagreement = max(disagreement, abs(hrrr - nbm))
        if nbp is not None and nbm is not None:
            disagreement = max(disagreement, abs(nbp["mu"] - nbm))
        if disagreement > 2.0:
            # Linear inflation: 1x at 2°F disagreement, 1.5x at 5°F+
            inflation = min(1.5, 1.0 + (disagreement - 2.0) * 0.15)
            sigma = sigma * inflation
        # NBP staleness σ inflation (V2 port, 2026-04-29). NBP cycles every
        # ~6h; between cycles, forecast uncertainty grows. Linear ramp:
        # +5%/h after 1h, capped at +30% (== 7h stale).
        # Applies to any source path that consumes NBP-derived σ:
        #   - "nbp": d-1+ default
        #   - "hrrr_d1_override": d-1+ HRRR-primary cities (CHI/OKC); μ is
        #     HRRR but σ is still NBP's, so staleness applies
        #   - "nbp_d0_override": d-0 NBP-primary cities (NYC/DC/BOS); both μ
        #     and σ are NBP. Staleness matters MORE here because d-0 entries
        #     can be made anytime overnight, including 4-6h after the latest
        #     NBP cycle, while HRRR (the default d-0 path) refreshes hourly.
        if mu_source in ("nbp", "hrrr_d1_override", "nbp_d0_override"):
            with _nbp_cache_lock:
                _ts = _nbp_cache_ts
                _cdt = _nbp_cache_cycle_dt
            if _ts > 0:
                age_h = (time.time() - _ts) / 3600.0
                if age_h > 1.0:
                    stale_mult = min(1.30, 1.0 + 0.05 * (age_h - 1.0))
                    sigma = sigma * stale_mult
                    # 2026-05-04: cycle-aware alert. Old `age_h > 3.0` fired
                    # halfway through every normal 6h NBP inter-cycle gap,
                    # producing one false positive every ~6h. Now we alert
                    # only when `_nbp_alert_overdue` says the next cycle is
                    # past its expected landing time (cycle_dt + 6h + publish
                    # latency + 30min grace).
                    if _nbp_alert_overdue(_cdt, age_h):
                        _nbp_staleness_alert(age_h)
        # Per-station σ inflation (2026-04-29). Counters NBP forecasts that
        # are systematically too narrow at specific stations.
        per_series_mult = PER_SERIES_SIGMA_MULT.get(m["series"], 1.0)
        if per_series_mult != 1.0:
            sigma = sigma * per_series_mult
        # Running min (only meaningful for today)
        rm = get_running_min(station, today_cd) if is_today else None
        # Post-sunrise lock
        # 2026-04-25: post_sunrise_lock DISABLED. The "low locks at sunrise"
        # heuristic is empirically too tight — Apr 25 V1 positions had market
        # prices nowhere near 99/1 at mid-morning, proving the daily low can
        # still drop later in the climate day (cold-front passage, late-evening
        # radiative cooling, post-frontal drop). Keep full NBP σ all day; the
        # running_min+1°F truncation is the safety net (low can only go down).
        post_sr = False
        model_prob = calc_bracket_probability_min(
            mu=mu, sigma=sigma,
            floor=m.get("floor"), cap=m.get("cap"),
            running_min=rm,
            post_sunrise_lock=post_sr,
            station=station,
            date_str=today_cd if is_today else None,
        )
        # Filter wildly unlikely / crowded
        if model_prob < MIN_MODEL_PROB or model_prob > MAX_MODEL_PROB:
            # still LOG the candidate with a low-prob tag
            pass
        yes_ask = m.get("yes_ask")
        yes_bid = m.get("yes_bid")
        no_ask = m.get("no_ask")
        # 2026-04-24: permissive — log EVERY bracket with a forecast as a
        # candidate, even when market has no liquidity (yes_ask=100) or
        # edges are negative. This builds a calibration dataset: we can
        # later measure model_prob vs settled outcome regardless of whether
        # we'd have traded. Untradeable candidates get action=None.
        yes_ask_v = yes_ask if yes_ask is not None else 100
        yes_bid_v = yes_bid if yes_bid is not None else 0
        no_ask_v = no_ask if no_ask is not None else (100 - yes_bid_v)
        yes_ask_frac = yes_ask_v / 100.0
        no_ask_frac = no_ask_v / 100.0
        buy_yes_edge = model_prob - yes_ask_frac
        buy_no_edge = (1 - model_prob) - no_ask_frac
        action = None
        edge = max(buy_yes_edge, buy_no_edge)
        entry_price = None
        if buy_yes_edge > buy_no_edge and buy_yes_edge > 0 and yes_ask_frac >= MIN_ORDER_PRICE:
            action = "BUY_YES"
            edge = buy_yes_edge
            entry_price = yes_ask_frac
        elif buy_no_edge > 0 and no_ask_frac >= MIN_ORDER_PRICE:
            action = "BUY_NO"
            edge = buy_no_edge
            entry_price = no_ask_frac
        # else: action=None, edge may be negative — record as calibration only
        opps.append({
            **m,
            "mu": mu, "sigma": sigma, "mu_source": mu_source,
            "mu_nbp": nbp["mu"] if nbp else None,
            "sigma_nbp": nbp["sigma"] if nbp else None,
            "mu_nbm_om": nbm, "mu_hrrr": hrrr,
            "disagreement": disagreement,
            "running_min": rm, "post_sunrise_lock": post_sr,
            "is_today": is_today,
            "model_prob": model_prob,
            "yes_ask_frac": yes_ask_frac, "no_ask_frac": no_ask_frac,
            "action": action, "edge": edge, "entry_price": entry_price,
        })
    opps.sort(key=lambda o: o["edge"], reverse=True)
    return opps


# ═══════════════════════════════════════════════════════════════════════
# EXECUTOR
# ═══════════════════════════════════════════════════════════════════════

def _append_jsonl(path: Path, record: dict) -> None:
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception as e:
        log(f"  jsonl append failed {path}: {e}", "warn")


_open_positions: dict[str, dict] = {}  # market_ticker → position record
_positions_lock = threading.Lock()

# 2026-05-17 — settlement dedup. Tracks tickers we've already written a
# settlement record for. Set is seeded at startup from settlements.jsonl
# and grows as new settlements are written. Used by both:
#   • _reconcile_kalshi_positions — skip re-adding tickers Kalshi still lists
#     as "open" because their batch settlement lags ours (the source of the
#     2026-05-14 KXLOWTDC-26MAY14-B52.5 triple-settlement bug, $120 fake loss
#     across 5 tickers/6 records lifetime).
#   • check_settlements — guard against any future code path that bypasses
#     the existing settled-flag check.
# In-memory only; settlements.jsonl is the durable source of truth.
_settled_tickers: set[str] = set()

# ─── Per-cycle / per-day budget tracking ────────────────────────────────
_cycle_budget_lock = threading.Lock()
_cycle_new_count = 0                          # reset each cycle
_today_exposure_usd = 0.0                     # cost of today's entries
_today_date_utc: str = ""                     # tracks UTC midnight rollover

# 2026-04-30: per-ticker paused cooldown + account-wide insufficient_balance
# cooldown removed. Per `feedback_no_unnecessary_cooldowns.md`: don't add
# cooldowns/sleeps unless they prevent actual harm. Cost of retry on 409
# (trading_is_paused) is one HTTP RTT + log line — no fee, no fill, no rate-
# limit penalty observed. Faster retry = faster fill the moment Kalshi
# unpauses. Insufficient_balance self-recovers within 60s via the bankroll
# cache refresh, so a 5-min cooldown only delayed recovery without saving
# anything. The 11k-retry storm the original V2 H-2 fix was protecting
# against was a different bug (V1 didn't have the cycle-aligned scan loop
# that bounds retries to one per scan).


def _reset_cycle_budget() -> None:
    """Called at the top of scan_cycle()."""
    global _cycle_new_count, _today_exposure_usd, _today_date_utc
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with _cycle_budget_lock:
        _cycle_new_count = 0
        if today != _today_date_utc:
            _today_date_utc = today
            _today_exposure_usd = 0.0


def _open_count_for_event(event_ticker: str) -> int:
    """Count currently-open (non-settled) positions on a given event.

    2026-05-08: under-fill exemption. Positions with actual_cost below
    EVENT_CAP_UNDERFILL_THRESHOLD_USD don't count toward the cap. KXLOW
    markets are often thin enough that bot's first fill on a bracket is
    only $0.40-$1.50 (single-contract market depth at the maker bid).
    Without the exemption, that tiny first fill locks the entire (city,
    date) event slot, blocking better entries on different brackets in
    the same event for the rest of the day.

    Backtest n=8 unique event_cap blocks in 14d:
      - 2 tiny-blocker cases (existing <$3): TDAL-MAY08-T61 today
        (blocker $0.41), DAL-MAY05-B64.5 (blocker $1.16). Exemption
        would allow these entries.
      - 5 moderate-blocker cases (existing $4-$7): partial-fill scenarios
        where bot got a moderate position. Exemption at $3 leaves these
        untouched.
      - 1 full-blocker case (existing $29.58): correlated-bet protection
        as intended. Exemption preserves this.

    Threshold $3 matches V1's NO_CASCADE under-fill exemption (>$5 +
    50% under-filled), kept tighter for min_bot's smaller bet sizes.
    """
    if not event_ticker:
        return 0
    with _positions_lock:
        n = 0
        for tk, pos in _open_positions.items():
            if pos.get("settled"):
                continue
            if tk.rsplit("-", 1)[0] != event_ticker:
                continue
            cost = pos.get("actual_cost")
            if cost is None:
                cost = pos.get("cost", 0)
            try:
                cost_f = float(cost)
            except (TypeError, ValueError):
                cost_f = 0.0
            if cost_f < EVENT_CAP_UNDERFILL_THRESHOLD_USD:
                continue
            n += 1
        return n


def _budget_can_take(cost_usd: float, event_ticker: str = "",
                     is_addon: bool = False) -> tuple[bool, str]:
    """Check live-mode caps. Returns (ok, reason_if_blocked).

    Per-event cap counts against `_open_positions` (lifetime, not per-cycle),
    so once any bracket on an event is open we won't add another bracket on
    the same event until that one settles. Correlated-bet protection.

    is_addon=True (V2-style add-on filling out a previously partial-filled
    position): skip event-cap (the existing position already counts and is
    the one we're growing) and skip cycle-cap (this is not a NEW position).
    Daily $ cap still applies — add-on dollars come from the same wallet."""
    if not is_addon:
        if event_ticker:
            open_n = _open_count_for_event(event_ticker)
            if open_n >= MAX_OPEN_PER_EVENT:
                return False, f"event_cap({event_ticker} open={open_n})"
    with _cycle_budget_lock:
        if not is_addon:
            if _cycle_new_count >= MAX_NEW_POSITIONS_PER_CYCLE:
                return False, f"cycle_cap({_cycle_new_count}/{MAX_NEW_POSITIONS_PER_CYCLE})"
        if _today_exposure_usd + cost_usd > DAILY_EXPOSURE_CAP_USD:
            return False, f"daily_cap(${_today_exposure_usd:.2f}+${cost_usd:.2f}>${DAILY_EXPOSURE_CAP_USD:.2f})"
    return True, ""


def _budget_record(cost_usd: float, event_ticker: str = "",
                   is_addon: bool = False) -> None:
    """Record spend against daily exposure. Cycle-new-count is incremented only
    on first entries — add-ons don't open a new slot."""
    global _cycle_new_count, _today_exposure_usd
    with _cycle_budget_lock:
        if not is_addon:
            _cycle_new_count += 1
        _today_exposure_usd += cost_usd


def _compute_today_exposure() -> float:
    """Sum of cost field over all 'entry' records for today's UTC date.
    Survives bot restarts: the daily cap is enforced against actual on-disk
    spend, not just in-process state. Without this, every restart resets
    _today_exposure_usd to $0 and the bot can spend the full
    DAILY_EXPOSURE_CAP_USD again.

    Reads today's date-rotated file (`trades_YYYY-MM-DD.jsonl`) plus the
    legacy single-file `trades.jsonl` for backward-compat with entries
    written before rotation was deployed."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    paths = [_trades_file_today(), TRADES_FILE]
    for path in paths:
        if not path.exists():
            continue
        try:
            with open(path) as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("kind") != "entry":
                        continue
                    if not rec.get("ts", "").startswith(today):
                        continue
                    # Skip legacy PAPER-mode records (no real money). Live records
                    # written post-rename have no `mode` field; pre-rename live
                    # records have mode="LIVE".
                    if rec.get("mode") == "PAPER":
                        continue
                    # 2026-04-25: only count entries on markets whose date_str is
                    # today UTC or later. Excludes the V2-wallet 04:09–04:26 cascade
                    # ($21 on Apr 24 markets) and any other prior-day backstop
                    # entries. Approximates wallet-scoping: V2 cascade hit Apr 24
                    # markets while V1's actual trades today are on Apr 25.
                    if rec.get("date_str", "") < today:
                        continue
                    total += float(rec.get("cost", 0.0))
        except Exception as e:
            log(f"  today-exposure compute failed for {path}: {e}", "warn")
    return total


def _reconcile_kalshi_positions() -> int:
    """Fetch current open positions from Kalshi /portfolio/positions and add
    any non-zero KXLOWT* holdings that aren't already in _open_positions.
    Recovers from earlier-bug-induced 'ghost positions' where the bot lost
    track of holdings (positions.json drained during a deploy cascade).
    Returns count of positions added."""
    global _open_positions
    try:
        data = kalshi_get("/trade-api/v2/portfolio/positions", {"limit": 200})
    except Exception as e:
        log(f"  Kalshi reconcile fetch failed: {e}", "warn")
        return 0
    added = 0
    for p in (data.get("market_positions") or []):
        tk = p.get("ticker", "")
        if not tk.startswith("KXLOWT"):
            continue
        try:
            pf = float(p.get("position_fp") or 0)
        except (TypeError, ValueError):
            continue
        if abs(pf) < 0.01:
            continue
        with _positions_lock:
            if tk in _open_positions:
                continue  # already tracked locally
            # 2026-05-17 — settlement-dedup guard. If we've already written
            # a settlement record for this ticker, don't re-add it as an
            # unsettled stub. Kalshi's batch settlement can lag ours by
            # 10min-2h+; without this guard the next check_settlements
            # cycle re-settles the position and writes a duplicate record
            # (see _settled_tickers comment + the KDCA-MAY14 triple-settle
            # incident).
            if tk in _settled_tickers:
                continue
            # Add stub so dedupe blocks re-entry. Kalshi's settle loop will
            # do its thing; settle records will be missing some entry context
            # (mu, sigma at entry) but that's an analytics gap, not a safety one.
            action = "BUY_YES" if pf > 0 else "BUY_NO"
            count = int(round(abs(pf)))
            cost = float(p.get("market_exposure_dollars") or 0)
            entry_price = (cost / count) if count else 0.0
            # Parse station + date_str from ticker
            m = re.match(r"KXLOWT([A-Z]+)-(\d{2}[A-Z]{3}\d{2})-", tk)
            station = ""
            date_str = ""
            if m:
                # Map series prefix to station
                series = "KXLOWT" + m.group(1)
                meta = CITIES.get(series, {})
                station = meta.get("station", "")
                # Parse date e.g. 26APR25 → 2026-04-25
                yy, mon_s, dd = m.group(2)[:2], m.group(2)[2:5], m.group(2)[5:7]
                mon_map = {"JAN":"01","FEB":"02","MAR":"03","APR":"04","MAY":"05","JUN":"06",
                           "JUL":"07","AUG":"08","SEP":"09","OCT":"10","NOV":"11","DEC":"12"}
                date_str = f"20{yy}-{mon_map.get(mon_s, '01')}-{dd}"
            # Parse bracket bounds from ticker
            br = parse_market_bracket(tk)
            floor = br.get("floor") if br else None
            cap = br.get("cap") if br else None
            # T-tails leave floor=cap=None from parse_market_bracket alone — that
            # function can't tell T-low from T-high without sibling B-brackets to
            # compare against (resolve_tail_bracket needs the event). Fetch the
            # market's yes_sub_title and parse the threshold directly.
            # Why this matters: check_settlements defaults `in_bracket=True` when
            # both bounds are None, so a recovered T-tail position would always
            # report yes_wins=True regardless of CLI — silently inverting the
            # `won` flag for BUY_YES (always "won") and BUY_NO (always "lost").
            # Audit 2026-04-27 caught this on CHI-T48 BUY_YES (+$8.28 phantom),
            # SEA-T42 BUY_YES (+$3.32 phantom), LV-T58 BUY_NO (-$0.80 phantom).
            if br and br.get("kind") == "tail" and floor is None and cap is None:
                try:
                    md = kalshi_get(f"/trade-api/v2/markets/{tk}").get("market", {})
                    sub = (md.get("yes_sub_title") or md.get("subtitle") or "").lower()
                    sm = re.search(r"(\d+)\s*°?\s*or\s*(above|below)", sub)
                    if sm:
                        x = int(sm.group(1))
                        if sm.group(2) == "above":
                            floor = float(x) - 0.5   # T-high: YES if cli ≥ X
                        else:
                            cap = float(x) + 0.5     # T-low:  YES if cli ≤ X
                except Exception as e:
                    log(f"  reconcile: tail bounds fetch failed for {tk}: {e}", "warn")
                if floor is None and cap is None:
                    log(f"  reconcile: SKIP {tk} — unresolved tail bounds "
                        f"(adding without bounds would invert settlement)", "warn")
                    continue
            _open_positions[tk] = {
                "ts": p.get("last_updated_ts") or datetime.now(timezone.utc).isoformat(),
                "kind": "entry",
                "market_ticker": tk,
                "action": action,
                "entry_price": entry_price,
                "count": count,
                "cost": cost,
                "station": station,
                "date_str": date_str,
                "floor": floor,
                "cap": cap,
                "label": (CITIES.get("KXLOWT" + m.group(1), {}) if m else {}).get("label", ""),
                "_recovered_from_kalshi": True,
            }
            added += 1
            log(f"  recovered ghost position {tk}: {action} {count}x @ {int(entry_price*100)}c")
    if added:
        _save_positions()
        log(f"  reconciliation: recovered {added} ghost positions from Kalshi")
    return added


def _reconcile_from_trades_log() -> int:
    """Closes the Kalshi `/portfolio/positions` API-lag window after a deploy
    or rapid restart where positions.json was clobbered before Kalshi had
    propagated our recent fills.

    Reads today's `kind=entry` records and adds any whose market_ticker is
    missing from `_open_positions` *and* not already in SETTLEMENTS_FILE.
    Worst-case false-positive (manually-closed-then-not-re-entered) is
    benign — it just blocks a redundant entry until next cycle's reconcile
    catches up. The bug it prevents (CHI T48 double-entry 2026-04-25) is
    much worse: real money on correlated dupes.

    Reads today's date-rotated file plus the legacy single-file trades.jsonl
    (so a deploy-day restart picks up entries written before rotation took
    over).
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    paths = [_trades_file_today(), TRADES_FILE]
    if not any(p.exists() for p in paths):
        return 0

    settled_tk: set[str] = set()
    if SETTLEMENTS_FILE.exists():
        try:
            with open(SETTLEMENTS_FILE) as f:
                for line in f:
                    try:
                        s = json.loads(line)
                    except Exception:
                        continue
                    tk = s.get("market_ticker")
                    if tk:
                        settled_tk.add(tk)
        except Exception as e:
            log(f"  trade-log reconcile: settlements read failed: {e}", "warn")

    entries: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            with open(path) as f:
                for line in f:
                    try:
                        t = json.loads(line)
                    except Exception:
                        continue
                    if t.get("kind") != "entry":
                        continue
                    if t.get("mode") == "PAPER":
                        continue
                    ts = t.get("ts", "")
                    if not ts.startswith(today):
                        continue
                    tk = t.get("market_ticker")
                    if not tk or tk in settled_tk:
                        continue
                    entries[tk] = t
        except Exception as e:
            log(f"  trade-log reconcile read failed for {path}: {e}", "warn")
            # Don't return — try the other path

    added = 0
    with _positions_lock:
        for tk, t in entries.items():
            if tk in _open_positions:
                continue
            stub = dict(t)
            stub["_recovered_from_trades_log"] = True
            _open_positions[tk] = stub
            added += 1
            log(f"  recovered from trades.jsonl: {tk} ({t.get('action')} "
                f"{t.get('count')}x @ {int(float(t.get('entry_price',0))*100)}c)")
    if added:
        _save_positions()
        log(f"  trade-log reconciliation: recovered {added} entries (Kalshi API hadn't propagated)")
    return added


def _seed_settled_tickers() -> int:
    """Populate _settled_tickers from settlements.jsonl. Best-effort: any
    parse error is swallowed (an unparseable line just means we won't dedup
    that one ticker on this boot, which is the existing behavior anyway).

    Returns the count loaded for logging.
    """
    if not SETTLEMENTS_FILE.exists():
        return 0
    n = 0
    try:
        with open(SETTLEMENTS_FILE) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "settlement":
                    continue
                tk = rec.get("market_ticker")
                if tk:
                    _settled_tickers.add(tk)
                    n += 1
    except Exception as e:
        log(f"  settled-tickers seed failed: {e}", "warn")
        return 0
    if n:
        log(f"  seeded {len(_settled_tickers)} settled tickers from {n} records")
    return n


def _load_positions() -> None:
    """Load positions.json, dropping any whose climate day is more than
    POSITION_TTL_DAYS in the past. Defends against orphaned positions that
    never settle (data error) accumulating indefinitely.

    2026-05-03: also prune already-settled positions whose climate day is
    in the past (see _prune_settled_past_positions). Without this, settled
    records accumulate in positions.json for up to POSITION_TTL_DAYS even
    though they no longer have any operational role — only the TTL drop
    on the next cold restart removes them. Found 31 such records on
    2026-05-03 (Apr 30 → May 2 dates, all settled=True). Bot was
    filtering them correctly during scans but the file grew unbounded
    until restart.
    """
    global _open_positions
    try:
        if not POSITIONS_FILE.exists():
            return
        with open(POSITIONS_FILE) as f:
            raw = json.load(f)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=POSITION_TTL_DAYS)).strftime("%Y-%m-%d")
        keep, dropped = {}, 0
        for tk, pos in raw.items():
            ds = pos.get("date_str") or ""
            if ds and ds < cutoff:
                dropped += 1
                continue
            keep[tk] = pos
        _open_positions = keep
        # 2026-05-17: seed _settled_tickers from settlements.jsonl so the
        # reconcile-on-restart path can skip tickers we've already settled.
        # Without this, Kalshi's slower settlement causes reconcile to re-add
        # our settled positions as fresh stubs → next check_settlements writes
        # duplicate records (the 2026-05-14 KDCA triple-settlement bug).
        _seed_settled_tickers()
        # 2026-05-03: prune settled positions for past climate-days BEFORE
        # logging the count, so the load message reflects the post-prune
        # count of operationally-active positions.
        pruned_settled = _prune_settled_past_positions()
        if pruned_settled > 0:
            log(f"  loaded {len(_open_positions)} positions "
                f"(dropped {dropped} > {POSITION_TTL_DAYS}d old, "
                f"pruned {pruned_settled} settled-past)")
        else:
            log(f"  loaded {len(_open_positions)} positions (dropped {dropped} > {POSITION_TTL_DAYS}d old)")
    except Exception as e:
        log(f"  positions load failed: {e}", "warn")


def _prune_settled_past_positions() -> int:
    """Remove settled positions whose climate-day is in the past (UTC).

    Conservative criteria — ALL of:
      - pos.get("settled") truthy
      - pos.get("date_str") non-empty
      - date_str < today (UTC)

    Skips:
      - Today's settled positions (climate-day may not be over for all
        timezones; bot may still want to read for late-day reporting)
      - Past-date but unsettled positions (visibility for stuck records)
      - Records without date_str (defensive — don't accidentally drop
        partially-populated records)

    Returns count removed. Persists via _save_positions if anything was
    removed. Safe to call at startup (after _load_positions assigns
    _open_positions) or at runtime — uses _positions_lock either way.

    Found on 2026-05-03 with 31 stale records: Apr 30 → May 2 dates, all
    settled=True. The TTL prune in _load_positions only fires after
    POSITION_TTL_DAYS=3, so settled positions accumulate for up to 3
    days. This complementary prune zeroes the lag.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    removed: list[str] = []
    with _positions_lock:
        for tk in list(_open_positions.keys()):
            pos = _open_positions[tk]
            ds = pos.get("date_str") or ""
            if pos.get("settled") and ds and ds < today:
                del _open_positions[tk]
                removed.append(tk)
    if removed:
        _save_positions()
    return len(removed)


def _save_positions() -> None:
    with _positions_lock:
        snap = dict(_open_positions)
    _atomic_write_json(POSITIONS_FILE, snap)


# ═══════════════════════════════════════════════════════════════════════
# RICH POSITION TELEMETRY (2026-05-02)
# ═══════════════════════════════════════════════════════════════════════
# Per-cycle live snapshot of every open position, written to pos["live"].
# Gives backtests a full state trajectory: market quotes, observed running_min,
# re-resolved forecast (mu/sigma/source/disagreement), recomputed model_prob
# and edge — all at the resolution of the bot's scan cycle. Plus per-position
# high-water marks (peak_mtm_pct, trough_mtm_pct, peak_running_min etc.) for
# fast queries without scanning the full telemetry log.
#
# Pure additive — does not change any decision logic. The fields are written
# to positions.json under the `live` sub-dict so they persist across restarts
# and cleanly separate from entry-time fields. JSONL telemetry log
# (data/position_telemetry_YYYY-MM-DD.jsonl) gives append-only history for
# replays.
#
# Trigger: V2's TSATX/LAX/MIA losses where post-hoc analysis was hampered by
# missing forecast/market state at exit time — entry-only fields don't tell
# you whether the bot's forecast drifted, market re-priced, etc. between
# entry and exit. With telemetry, every exit has a reconstructable trajectory.
def _resolve_live_min_forecast(series: str, station: str, date_str: str,
                               is_today: bool) -> Optional[dict]:
    """Re-resolve the forecast for an open position using the same priority
    + sigma-inflation logic as scan_and_trade. Returns None when no forecast
    source has data; otherwise a dict with mu, sigma, mu_source, components.

    All reads are in-memory cache lookups (NBP, HRRR, NBM-OM, sigma table) —
    cheap enough to call per-position per-scan-cycle.

    Mirror of the priority logic at scan_and_trade ~L2360. Keep in sync.
    """
    nbp = get_nbp_forecast(series, date_str)
    nbm = get_nbm_om_min(series, date_str)
    hrrr = get_hrrr_min(series, date_str)
    mu = sigma = None
    mu_source = ""
    _d0p = get_d0_primary(series)
    if (is_today
            and _d0p == "nbp"
            and nbp):
        mu, sigma, mu_source = nbp["mu"], nbp["sigma"], "nbp_d0_override"
    elif is_today and hrrr is not None:
        mu, sigma, mu_source = hrrr, (nbp["sigma"] if nbp else 2.5), "hrrr"
    elif (not is_today and USE_D1_CONSENSUS
          and (_cm := _d1_consensus_mu(series, hrrr, nbm, nbp)) is not None):
        mu, sigma, mu_source = _cm
    elif (not is_today
          and get_d1_primary(series) == "hrrr"
          and hrrr is not None):
        mu, sigma, mu_source = hrrr, (nbp["sigma"] if nbp else 2.5), "hrrr_d1_override"
    elif nbp:
        mu, sigma, mu_source = nbp["mu"], nbp["sigma"], "nbp"
    elif nbm is not None:
        mu, sigma, mu_source = nbm, 2.5, "nbm_om"
    if mu is None:
        return None
    # Disagreement-based sigma inflation (mirrors scan_and_trade)
    disagreement = 0.0
    if hrrr is not None and nbp is not None:
        disagreement = max(disagreement, abs(hrrr - nbp["mu"]))
    if hrrr is not None and nbm is not None:
        disagreement = max(disagreement, abs(hrrr - nbm))
    if nbp is not None and nbm is not None:
        disagreement = max(disagreement, abs(nbp["mu"] - nbm))
    if disagreement > 2.0:
        sigma = sigma * min(1.5, 1.0 + (disagreement - 2.0) * 0.15)
    # NBP staleness inflation
    if mu_source in ("nbp", "hrrr_d1_override", "nbp_d0_override"):
        with _nbp_cache_lock:
            _ts = _nbp_cache_ts
        if _ts > 0:
            age_h = (time.time() - _ts) / 3600.0
            if age_h > 1.0:
                sigma = sigma * min(1.30, 1.0 + 0.05 * (age_h - 1.0))
    # Per-station sigma multiplier
    sigma = sigma * PER_SERIES_SIGMA_MULT.get(series, 1.0)
    return {
        "mu": mu, "sigma": sigma, "mu_source": mu_source,
        "disagreement": disagreement,
        "nbp_mu": nbp["mu"] if nbp else None,
        "nbp_sigma": nbp["sigma"] if nbp else None,
        "hrrr": hrrr, "nbm_om": nbm,
    }


def _compute_position_telemetry(pos: dict, mkt: Optional[dict]) -> dict:
    """Build a `live` snapshot dict for one open position. Pure read —
    does not mutate `pos`. Caller merges return value into pos["live"]
    and updates running peaks. Always returns a dict (possibly minimal)
    so the position has a uniform schema even when forecast/obs unavailable.
    """
    series = pos.get("series")
    if not series:
        # Older position records (pre-2026-04-30 entry-awareness fields) may
        # lack `series`. Derive from market_ticker: "KXLOWT<series>-<date>-<bracket>".
        mt = pos.get("market_ticker", "") or ""
        parts = mt.split("-")
        series = parts[0] if parts and parts[0].startswith("KXLOWT") else None
    station = pos.get("station")
    date_str = pos.get("date_str")
    floor = pos.get("floor")
    cap = pos.get("cap")
    action = pos.get("action")
    now = time.time()
    snap: dict = {
        "ts": now,
        "ts_iso": datetime.now(timezone.utc).isoformat(),
    }
    # Market state
    if mkt is not None:
        snap["yes_bid_c"] = mkt.get("yes_bid")
        snap["yes_ask_c"] = mkt.get("yes_ask")
        snap["no_bid_c"] = mkt.get("no_bid")
        snap["no_ask_c"] = mkt.get("no_ask")
        snap["spread_c"] = (
            (mkt["yes_ask"] - mkt["yes_bid"]) if (
                mkt.get("yes_bid") is not None and mkt.get("yes_ask") is not None)
            else None)
        if action == "BUY_YES":
            cb = mkt.get("yes_bid")
            snap["current_bid_side_c"] = cb
        elif action == "BUY_NO":
            cb = mkt.get("no_bid")
            snap["current_bid_side_c"] = cb
        else:
            cb = None
        if cb is not None:
            snap["current_price"] = cb / 100.0
            entry_price = float(pos.get("entry_price", 0) or 0)
            if entry_price > 0:
                # MTM PnL %: positive = winning, negative = losing
                snap["current_mtm_pct"] = (snap["current_price"] - entry_price) / entry_price
    # Obs state
    if station and date_str:
        try:
            rm = get_running_min(station, date_str)
        except Exception:
            rm = None
        if rm is not None:
            snap["running_min"] = float(rm)
    # Forecast re-resolution
    if series and station and date_str:
        try:
            # 2026-05-03: was `config.STATIONS.get(station, ...)` — but min_bot
            # has no `config` import. CITIES is keyed by series → metadata.
            tz_name = CITIES.get(series, {}).get("tz")
            today_cd = _climate_date_nws(tz_name) if tz_name else None
        except Exception:
            today_cd = None
        is_today = (today_cd is not None and date_str == today_cd)
        try:
            fc = _resolve_live_min_forecast(series, station, date_str, is_today)
        except Exception:
            fc = None
        if fc is not None:
            snap["mu"] = fc["mu"]
            snap["sigma"] = fc["sigma"]
            snap["mu_source"] = fc["mu_source"]
            snap["disagreement"] = fc["disagreement"]
            snap["nbp_mu"] = fc["nbp_mu"]
            snap["nbp_sigma"] = fc["nbp_sigma"]
            snap["hrrr"] = fc["hrrr"]
            snap["nbm_om"] = fc["nbm_om"]
            # Recompute model_prob using current obs + forecast
            try:
                live_mp = calc_bracket_probability_min(
                    mu=fc["mu"], sigma=fc["sigma"],
                    floor=floor, cap=cap,
                    running_min=snap.get("running_min"),
                    post_sunrise_lock=False,  # mirrors scan_and_trade default
                    station=station,
                    date_str=date_str,
                )
                snap["model_prob"] = float(live_mp) if live_mp is not None else None
            except Exception:
                snap["model_prob"] = None
            # Recompute edge using live model_prob and live market quote
            mp = snap.get("model_prob")
            if mp is not None and mkt is not None:
                if action == "BUY_NO":
                    yb = mkt.get("yes_bid")
                    if yb is not None:
                        snap["edge"] = yb / 100.0 - mp
                elif action == "BUY_YES":
                    ya = mkt.get("yes_ask")
                    if ya is not None:
                        snap["edge"] = mp - ya / 100.0
    # Local hour for daypart-bucketed analysis
    if station:
        try:
            # 2026-05-03: was `config.STATIONS.get(station, ...)` — see note above.
            tz_name = CITIES.get(series, {}).get("tz") if series else None
            if tz_name:
                snap["local_hour"] = datetime.now(ZoneInfo(tz_name)).hour
        except Exception:
            pass
    return snap


def _telemetry_log_path() -> Path:
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return DATA_DIR / f"position_telemetry_{today_utc}.jsonl"


def _update_open_positions_telemetry(market_quotes: dict) -> None:
    """Walk all open positions and write a `live` snapshot per position.
    Pure additive — no decision logic changes. Updates pos["live"] in place
    AND appends one JSONL row per open position to today's telemetry log
    for backtest replay.

    Should be called once per scan cycle, after market_quotes is built.
    """
    with _positions_lock:
        ticker_list = [t for t, p in _open_positions.items() if not p.get("settled")]
    if not ticker_list:
        return
    log_path = _telemetry_log_path()
    log_rows: list[dict] = []
    for ticker in ticker_list:
        with _positions_lock:
            pos = _open_positions.get(ticker)
            if pos is None or pos.get("settled"):
                continue
            mkt = market_quotes.get(ticker)
            try:
                snap = _compute_position_telemetry(pos, mkt)
            except Exception as e:
                # Don't let telemetry break the scan cycle; log and skip.
                log(f"  telemetry compute {ticker} failed: {type(e).__name__}: {e}", "warn")
                continue
            # Merge into pos["live"], updating high-water marks.
            live = pos.get("live") or {}
            cycles = int(live.get("cycles", 0)) + 1
            live.update(snap)
            live["cycles"] = cycles
            # MTM peak/trough
            mtm = snap.get("current_mtm_pct")
            if mtm is not None:
                if "peak_mtm_pct" not in live or mtm > live["peak_mtm_pct"]:
                    live["peak_mtm_pct"] = mtm
                if "trough_mtm_pct" not in live or mtm < live["trough_mtm_pct"]:
                    live["trough_mtm_pct"] = mtm
            # running_min lowest-wins (for min-temp brackets)
            rm = snap.get("running_min")
            if rm is not None:
                if "peak_running_min" not in live or rm < live["peak_running_min"]:
                    live["peak_running_min"] = rm
            # market peak: best bid we've seen on the side we hold
            cb = snap.get("current_bid_side_c")
            if cb is not None:
                if "peak_bid_side_c" not in live or cb > live["peak_bid_side_c"]:
                    live["peak_bid_side_c"] = cb
                if "trough_bid_side_c" not in live or cb < live["trough_bid_side_c"]:
                    live["trough_bid_side_c"] = cb
            pos["live"] = live
            # Build telemetry log row (full per-cycle snapshot, NOT
            # high-water-mark — those live in pos.live).
            row = dict(snap)
            row["market_ticker"] = ticker
            row["station"] = pos.get("station")
            row["series"] = pos.get("series")
            row["date_str"] = pos.get("date_str")
            row["action"] = pos.get("action")
            row["floor"] = pos.get("floor")
            row["cap"] = pos.get("cap")
            row["count"] = pos.get("count")
            row["entry_price"] = pos.get("entry_price")
            row["entry_model_prob"] = pos.get("model_prob")
            row["entry_mu"] = pos.get("mu")
            row["entry_sigma"] = pos.get("sigma")
            row["entry_mu_source"] = pos.get("mu_source")
            row["cycles_since_entry"] = cycles
            log_rows.append(row)
    if log_rows:
        try:
            with open(log_path, "a") as f:
                for r in log_rows:
                    f.write(json.dumps(r, default=str) + "\n")
        except Exception as e:
            log(f"  telemetry log write failed: {type(e).__name__}: {e}", "warn")
    _save_positions()


def _compute_primary_outlier_diff(opp: dict) -> Optional[float]:
    """Shadow-log diagnostic: |primary_mu - mean(other available source mus)|.

    Flags trades where the bot's chosen forecast source disagrees with the
    rest of the cluster — a candidate boundary-rounding-loss signal. Logged
    on every entry but NOT yet used as a gate. Validate forward on ~60+
    BUY_NO trades before considering a SKIP_PRIMARY_OUTLIER filter at e.g.
    2.0°F. Reference: HOU 26APR30-B68.5 had primary HRRR=69.4 vs NBP=74 /
    NBM=69.5 → diff=2.35°F → settled $-29.44 (n=2 settled saves so far,
    insufficient to deploy as gate)."""
    src = opp.get("mu_source") or ""
    if "nbp" in src:
        primary_key = "mu_nbp"
    elif "hrrr" in src:
        primary_key = "mu_hrrr"
    elif src == "nbm_om":
        primary_key = "mu_nbm_om"
    else:
        return None
    primary = opp.get(primary_key)
    if primary is None:
        return None
    others = []
    for key in ("mu_nbp", "mu_hrrr", "mu_nbm_om"):
        if key == primary_key:
            continue
        v = opp.get(key)
        if v is not None:
            others.append(float(v))
    if not others:
        return None
    return round(abs(float(primary) - sum(others) / len(others)), 3)


def _evaluate_gates(opp: dict) -> tuple[Optional[str], Optional[str]]:
    """⚠️ AUDIT / CALIBRATION ONLY — THIS DOES NOT BLOCK LIVE TRADES. ⚠️

    The LIVE entry-gate path is the inline gate sequence in
    `execute_opportunity` (each gate there calls `_audit_skip(...)` then
    `return False`). THIS function is a read-only REPLICA of that sequence,
    used only to stamp a `blocked_by` reason on every candidate for
    calibration analysis. It is NOT dead code: `record_candidate` writes its
    result to the candidate log, and `tools/gate_audit.py` imports and calls
    it directly — so it cannot be deleted, but it also cannot block anything.

    => A new gate MUST be added in BOTH places, in the same order:
         (1) HERE, so the candidate audit log attributes the block, AND
         (2) `execute_opportunity`, so the trade is actually blocked.
       Added only here  → logged but NEVER blocks (the 2026-05 COASTAL +
       THIN_MARGIN near-miss). Added only there → blocks but is unattributed
       in the audit pool. Keep the two sequences in sync.

    Replays all entry gates in `execute_opportunity` order against `opp` and
    returns `(blocked_by, reason)` for the FIRST gate that blocks. Returns
    `(None, "obs_alive_bypass")` when `_obs_confirmed_alive` triggers and
    bypasses forecast gates, or `(None, None)` when all gates pass and the
    bot would enter (modulo per-ticker dedupe + budget caps which are stateful
    and not modeled here). Pure function: no side effects, no Kalshi calls."""
    action = opp.get("action")
    entry_price = opp.get("entry_price")
    edge = float(opp.get("edge") or 0)
    mp_v = opp.get("model_prob")

    # No-action / no-edge candidates are not "blocked" — they're just below
    # the bot's interest threshold (find_opportunities found no positive edge
    # on either side). Tag separately so analysis can split them out.
    if action is None or entry_price is None:
        return ("NO_ACTION", "no positive edge on either side")
    mp = float(mp_v or 0)

    # Edge floor — depends on obs_alive bypass.
    obs_alive = _check_obs_confirmed_alive(opp)
    edge_floor = OBS_ALIVE_MIN_EDGE if obs_alive else MIN_EDGE
    if edge < edge_floor:
        return ("MIN_EDGE", f"{edge:.2%} < {edge_floor:.0%}")

    # obs_alive bypasses everything else (matches execute_opportunity).
    if obs_alive:
        return (None, "obs_alive_bypass")

    if _check_obs_confirmed_loser(opp):
        return ("OBS_CONFIRMED_LOSER",
                f"rm={opp.get('running_min')} in YES territory")
    # 2026-05-27 ENTRY_TIME_WINDOW: block BUY_NO entries fired too early
    # (d-1 lead forecast σ underestimates actual error). See constants block.
    etw_block = _check_entry_time_window(opp)
    if etw_block:
        return ("ENTRY_TIME_WINDOW", etw_block)
    if edge > MAX_EDGE:
        return ("MAX_EDGE", f"{edge:.2%} > {MAX_EDGE:.0%}")
    if mp < MIN_MODEL_PROB or mp > MAX_MODEL_PROB:
        if not _nbp_consistent_with_recent_cli(opp):
            return ("MP_RANGE",
                    f"mp {mp:.0%} outside [{MIN_MODEL_PROB:.0%}, {MAX_MODEL_PROB:.0%}]")
    if action == "BUY_NO" and mp > DIRECTIONAL_BUY_NO_MAX_MP:
        return ("DIRECTIONAL_BUY_NO", f"mp {mp:.0%} > {DIRECTIONAL_BUY_NO_MAX_MP:.0%}")
    if action == "BUY_YES" and mp < DIRECTIONAL_BUY_YES_MIN_MP:
        return ("DIRECTIONAL_BUY_YES", f"mp {mp:.0%} < {DIRECTIONAL_BUY_YES_MIN_MP:.0%}")
    # 2026-05-08: BUY_NO_EXTREME_SIGMA. Block when σ ≥ 8 AND mp < 10% —
    # see constant block above for mechanism.
    if (action == "BUY_NO"
            and (opp.get("sigma") or 0) >= BUY_NO_EXTREME_SIGMA_THRESHOLD
            and (opp.get("model_prob") or 1.0) < BUY_NO_EXTREME_SIGMA_MAX_MP):
        return ("BUY_NO_EXTREME_SIGMA",
                f"σ={opp.get('sigma'):.2f}≥{BUY_NO_EXTREME_SIGMA_THRESHOLD} "
                f"AND mp={mp:.1%}<{BUY_NO_EXTREME_SIGMA_MAX_MP:.0%} "
                f"(Gaussian-flattening artifact at high uncertainty)")
    # 2026-05-08: KLAX_BUY_NO_HIGH_SIGMA. KLAX-specific: block BUY_NO
    # B-bracket when σ ≥ 2.5°F. See constant block for mechanism + backtest.
    if (action == "BUY_NO"
            and "KXLOWTLAX" in (opp.get("market_ticker") or "")
            and opp.get("floor") is not None and opp.get("cap") is not None
            and (opp.get("sigma") or 0) >= LAX_BUY_NO_HIGH_SIGMA_THRESHOLD):
        return ("KLAX_BUY_NO_HIGH_SIGMA",
                f"KLAX BUY_NO B-bracket with σ={opp.get('sigma'):.2f}"
                f"≥{LAX_BUY_NO_HIGH_SIGMA_THRESHOLD} "
                f"(Pacific microclimate uncertainty; backtest 0W:3L, lift +$70)")
    # 2026-05-15: BUY_NO_LOW_BRACKET_TRAP — see constant block for full
    # mechanism + backtest (catches 3/3 5/14 V1-min losers, 0/9 winners).
    if (action == "BUY_NO"
            and opp.get("floor") is not None
            and opp.get("cap") is not None):
        _bt_fl = float(opp["floor"]); _bt_cp = float(opp["cap"])
        _bt_mu = opp.get("mu")
        _bt_disg = opp.get("disagreement")
        _bt_rm = opp.get("running_min")
        if _bt_mu is not None:
            # Mechanism A: mu > cap AND models tightly agree (consensus near cap)
            if (float(_bt_mu) > _bt_cp
                    and _bt_disg is not None
                    and float(_bt_disg) < BUY_NO_LOW_BRACKET_TRAP_DISAGREE_MAX):
                return ("BUY_NO_LOW_BRACKET_TRAP_CONSENSUS",
                        f"μ={_bt_mu:.1f}>cap={_bt_cp} AND "
                        f"disagree={_bt_disg:.2f}<{BUY_NO_LOW_BRACKET_TRAP_DISAGREE_MAX} "
                        f"(NBP/HRRR/NBM consensus stacked above bracket — "
                        f"caught DAL-5/14 -$31, DC-5/14 -$61)")
            # Mechanism B: mu < floor AND rm not yet below cap (bracket in path)
            if (float(_bt_mu) < _bt_fl
                    and _bt_rm is not None
                    and float(_bt_rm) >= _bt_cp):
                return ("BUY_NO_LOW_BRACKET_TRAP_RM_ABOVE",
                        f"μ={_bt_mu:.1f}<floor={_bt_fl} AND "
                        f"rm={_bt_rm:.1f}≥cap={_bt_cp} "
                        f"(bracket sits between current rm and forecast μ — "
                        f"caught MIN-5/14 -$7)")
    # Global BUY_NO T-high block (2026-05-01, was per-station LAX-only).
    # Backtest helps:hurts 6:1, net +$2.93 across n=7 historical entries.
    # Forward audit: candidate log records blocked_by="NO_THIGH" so future
    # analysis can confirm filter isn't blocking an emerging winner.
    if (action == "BUY_NO"
            and opp.get("floor") is not None and opp.get("cap") is None):
        return ("NO_THIGH",
                f"BUY_NO on T-high market blocked (structurally fights "
                f"nighttime cooling; backtest 6L:1W on n=7)")
    # 2026-05-07: BUY_YES T-tail block. Backtest 4/30-5/7 on settled pool:
    # 0W:6L net -$47.07 / -61% ROI (DC-T46, OKC-T47, ATL-T53 x2, OKC-T51,
    # MIN-T38; PHIL-T52 today pending → likely 0:7). Mechanism: mu within
    # 1-2F of threshold = effective coinflip, but asymmetric loss profile
    # (pay 30-50c, lose 100% on miss). DIRECTIONAL_BUY_YES_MIN_MP tightening
    # (2026-05-04: 0.60→0.65) did not fix — PHIL-T52 had mp=77%.
    # B-band BUY_YES (3W:0L) and other B-bracket BUY_YES are NOT blocked.
    if (action == "BUY_YES"
            and opp.get("floor") is not None and opp.get("cap") is None):
        return ("YES_TTAIL",
                f"BUY_YES on T-tail market blocked (mu near threshold = "
                f"coinflip with asymmetric loss; backtest 0W:6L, -$47/-61%)")
    # BUY_YES tail margin gate (2026-05-01). Live-pool losers had margin
    # ≤ +0.5°F into YES region; winners ≥ +0.9°F. Skip when < 1.0°F.
    # Triggered by today's DC-T46 stuck loss (μ=47.0, floor=46.5,
    # margin +0.5°F → -$24 unrealized).
    if action == "BUY_YES":
        _yt_fl = opp.get("floor"); _yt_cp = opp.get("cap")
        _yt_mu = opp.get("mu")
        # tail = exactly one of (floor, cap) is set
        _yt_is_tail = (_yt_fl is not None) ^ (_yt_cp is not None)
        if _yt_is_tail and _yt_mu is not None:
            if _yt_fl is not None:
                _yt_margin = float(_yt_mu) - float(_yt_fl)  # T-high: μ above floor
            else:
                _yt_margin = float(_yt_cp) - float(_yt_mu)  # T-low: μ below cap
            if _yt_margin < YES_TAIL_MIN_MARGIN_F:
                return ("YES_TAIL_MARGIN",
                        f"BUY_YES tail margin {_yt_margin:+.1f}°F < "
                        f"{YES_TAIL_MIN_MARGIN_F}°F into YES region")
    if action == "BUY_NO":
        fl = opp.get("floor"); cp = opp.get("cap")
        if fl is not None and cp is not None:
            bracket_mid = (float(fl) + float(cp)) / 2.0
        elif fl is not None:
            bracket_mid = float(fl)
        elif cp is not None:
            bracket_mid = float(cp)
        else:
            bracket_mid = None
        if bracket_mid is not None and ABS_DISTANCE_ENABLED:
            mu_val = float(opp.get("mu", 0.0))
            sigma_v = float(opp.get("sigma") or 0)
            abs_dist = abs(mu_val - bracket_mid)
            # σ-relative threshold (2026-04-30 PM): wider σ requires more
            # distance from bracket center.
            min_dist = max(MIN_ABS_DISTANCE_F, MIN_ABS_DISTANCE_SIGMA_K * sigma_v)
            if abs_dist < min_dist:
                return ("ABS_DIST",
                        f"|μ-mid|={abs_dist:.1f}°F < {min_dist:.2f}°F "
                        f"(floor=0.5, σ-rel={MIN_ABS_DISTANCE_SIGMA_K}×{sigma_v:.1f})")
    f2a = _check_f2a_gate(opp)
    if f2a:
        return ("F2A", f2a)
    # 2026-05-03 COASTAL_TIGHT_FLOOR: known marine-cold-bias cities require a
    # 2°F cushion between bracket floor and bot's mu before BUY_NO B-bracket.
    # See COASTAL_TIGHT_FLOOR_STATIONS docstring for backtest evidence.
    if _COASTAL_TIGHT_FLOOR_ENABLED and action == "BUY_NO":
        _ctf_station = opp.get("station")
        _ctf_floor = opp.get("floor")
        _ctf_cap = opp.get("cap")
        _ctf_mu = opp.get("mu")
        if (_ctf_station in COASTAL_TIGHT_FLOOR_STATIONS
                and _ctf_floor is not None and _ctf_cap is not None  # B-bracket only
                and _ctf_mu is not None):
            _ctf_gap = float(_ctf_floor) - float(_ctf_mu)
            if _ctf_gap < COASTAL_TIGHT_FLOOR_MIN_GAP_F:
                return ("COASTAL_TIGHT_FLOOR",
                        f"floor-mu gap {_ctf_gap:+.1f}°F < {COASTAL_TIGHT_FLOOR_MIN_GAP_F}°F "
                        f"(station={_ctf_station} marine cold-bias)")
    # THIN_MARGIN (2026-05-26): skip BUY_NO when μ is within THIN_MARGIN_MIN_F
    # of the NEAR bracket edge (μ−cap if μ>cap, else floor−μ) — a boundary
    # coin-flip that loses on the NO payoff asymmetry (breakeven WR == price).
    # Distinct from the disabled ABS_DIST (|μ−mid|, 0.5°F). Placed after
    # COASTAL_TIGHT_FLOOR so that gate keeps its specific reason when enabled.
    if THIN_MARGIN_GATE_ENABLED and action == "BUY_NO":
        _tm_mu = opp.get("mu")
        _tm_fl = opp.get("floor")
        _tm_cp = opp.get("cap")
        if _tm_mu is not None:
            _tm_mu = float(_tm_mu)
            _tm_margin = None
            if _tm_cp is not None and _tm_mu > float(_tm_cp):
                _tm_margin = _tm_mu - float(_tm_cp)
            elif _tm_fl is not None and _tm_mu < float(_tm_fl):
                _tm_margin = float(_tm_fl) - _tm_mu
            if _tm_margin is not None and _tm_margin <= THIN_MARGIN_MIN_F:
                return ("THIN_MARGIN",
                        f"μ {_tm_margin:.1f}°F outside near edge ≤ "
                        f"{THIN_MARGIN_MIN_F}°F (boundary coin-flip)")
    mmd_block = _check_model_market_disagree(opp)
    if mmd_block:
        return ("MODEL_MARKET_DISAGREE", mmd_block)
    # SKIP_MU_NEAR_FLOOR + SKIP_MU_NEAR_BELOW_BRACKET (V1 mirror, 2026-05-06).
    # See _check_mu_position_filter docstring + constant block for backtest.
    mu_block = _check_mu_position_filter(opp)
    if mu_block is not None:
        return mu_block
    msg_block = _check_msg_gate(opp)
    if msg_block:
        return ("MSG", msg_block)
    cold_block = _check_cold_source_outlier(opp)
    if cold_block:
        return ("COLD_SOURCE_OUTLIER", cold_block)
    hrrr_dissent_block = _check_hrrr_dissent(opp)
    if hrrr_dissent_block:
        return ("HRRR_DISSENT", hrrr_dissent_block)
    nbm_bracket_block = _check_nbm_in_bracket(opp)
    if nbm_bracket_block:
        return ("BUY_NO_NBM_IN_BRACKET", nbm_bracket_block)
    high_mp_block = _check_high_mp_trap(opp)
    if high_mp_block:
        return ("BUY_NO_HIGH_MP_TRAP", high_mp_block)
    tail_risk_block = _check_buy_no_tail_risk(opp)
    if tail_risk_block:
        return ("BUY_NO_TAIL_RISK", tail_risk_block)
    hrrr_in_bracket_block = _check_hrrr_in_bracket_warm(opp)
    if hrrr_in_bracket_block:
        return ("BUY_NO_HRRR_IN_BRACKET_WARM", hrrr_in_bracket_block)
    # BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP gate (2026-05-20 PM). Catches
    # SATX-class mp~0 outliers where yes_ask >> 8x mp.
    ext_md_block = _check_extreme_market_disagree_low_mp(opp)
    if ext_md_block:
        return ("BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP", ext_md_block)
    hcdt_block = _check_high_conviction_disag_trap(opp)
    if hcdt_block:
        return ("HIGH_CONVICTION_DISAG_TRAP", hcdt_block)
    if action == "BUY_NO" and not opp.get("is_today", False):
        disag = float(opp.get("disagreement") or 0)
        if disag > H_2_0_DISAGREE_F:
            return ("H_2_0", f"disagreement {disag:.1f}°F > {H_2_0_DISAGREE_F}°F")
    if action == "BUY_YES":
        disag = float(opp.get("disagreement") or 0)
        if disag >= BUY_YES_DISAGREE_MAX_F:
            return ("BUY_YES_DISAGREE",
                    f"BUY_YES disagreement {disag:.1f}°F ≥ {BUY_YES_DISAGREE_MAX_F}°F")
    if _DISAGREEMENT_ENABLED:
        disag = float(opp.get("disagreement") or 0)
        if disag > MAX_DISAGREEMENT_F:
            return ("MAX_DISAGREEMENT", f"{disag:.1f}°F > {MAX_DISAGREEMENT_F}°F")
    rm = opp.get("running_min")
    if rm is not None and not opp.get("post_sunrise_lock"):
        mu_check = float(opp.get("mu") or 0)
        if abs(mu_check - float(rm)) > MAX_MU_VS_RM_DIFF_F:
            return ("MU_VS_RM",
                    f"|μ-rm|={abs(mu_check-float(rm)):.1f}°F > {MAX_MU_VS_RM_DIFF_F}°F")
    side = "yes" if action == "BUY_YES" else "no"
    if side == "yes":
        ya, yb = opp.get("yes_ask"), opp.get("yes_bid")
        spread = (ya - yb) if (ya is not None and yb is not None) else 0
    else:
        na, nb = opp.get("no_ask"), opp.get("no_bid")
        spread = (na - nb) if (na is not None and nb is not None) else 0
    if spread > MAX_SPREAD_CENTS:
        return ("SPREAD", f"{spread}c > {MAX_SPREAD_CENTS}c")

    # All gates pass. (Bot may still skip due to per-ticker dedupe in
    # _open_positions or per-cycle/daily/event budget — those are stateful
    # and not modeled here.)
    return (None, None)


def record_candidate(opp: dict) -> None:
    """Record a candidate opportunity for calibration analysis. Every
    generated opp goes here — not just taken ones — so we can back-test
    the model's probability calibration against settled outcomes.

    2026-04-25 fix: opp has its own `kind` field (bracket/tail_low/tail_high)
    which was silently overwriting our `kind: candidate` discriminator via
    dict-spread. Rename to bracket_kind so the candidate/entry filter works.

    2026-04-29: also writes `blocked_by` + `block_reason` fields via
    `_evaluate_gates` so downstream analysis can identify which gate is
    blocking winners (V2-style shadow logging). `blocked_by=None` means the
    candidate would have been entered (subject to dedupe + budget caps).

    2026-05-13 PM: added cohort fields days_out, entry_local_hour,
    entry_local_dow so candidate-pool backtests can split by d-0 vs d-1+
    and by bot-decision hour without recomputing from date_str + bot clock.
    Mirrors entry-record fields populated in execute_opportunity."""
    fields = ("event_ticker", "market_ticker", "station", "series", "date_str",
              "label", "floor", "cap",
              "yes_bid", "yes_ask", "no_bid", "no_ask",
              "volume", "mu", "sigma", "mu_source",
              "mu_nbp", "sigma_nbp", "mu_nbm_om", "mu_hrrr", "disagreement",
              "running_min",
              "post_sunrise_lock", "is_today", "model_prob",
              "yes_ask_frac", "no_ask_frac",
              "action", "edge", "entry_price")
    blocked_by, block_reason = _evaluate_gates(opp)
    # 2026-05-13 PM: derive cohort fields at log time.
    _tz_name = opp.get("tz")
    _entry_local_hour = None
    _entry_local_dow = None
    if _tz_name:
        try:
            _now_local = datetime.now(ZoneInfo(_tz_name))
            _entry_local_hour = _now_local.hour
            _entry_local_dow = _now_local.strftime("%a")
        except Exception:
            pass
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "candidate",
        "bracket_kind": opp.get("kind"),
        "blocked_by": blocked_by,
        "block_reason": block_reason,
        "days_out": _days_out_int(opp),
        "entry_local_hour": _entry_local_hour,
        "entry_local_dow": _entry_local_dow,
        **{k: opp.get(k) for k in fields},
    }
    _append_jsonl(_trades_file_today(), record)


# 2026-05-26 GATE UNIFICATION: execute_opportunity now sources its
# forecast/market gate decisions from _evaluate_gates (single source of
# truth — TestGatePathParity guards equivalence). These two tables preserve
# the live-path observable behavior the unification must not change:
#   _AUDIT_TO_LIVE_DECISION — _evaluate_gates returns audit decision names;
#     map them to the bot_decisions.sqlite names the live path emitted before
#     unification, so historical decision-name queries keep matching.
#   _GATE_BLOCK_ALERTS — the gates that fire a Discord block-alert.
_AUDIT_TO_LIVE_DECISION = {
    "MAX_EDGE": "MAX_EDGE_EXCEEDED",
    "MP_RANGE": "MODEL_PROB_OUT_OF_RANGE",
    "DIRECTIONAL_BUY_NO": "DIRECTIONAL_NO_DISAGREE",
    "DIRECTIONAL_BUY_YES": "DIRECTIONAL_YES_DISAGREE",
    "ABS_DIST": "ABS_DISTANCE",
    "H_2_0": "H_2_DISAGREE",
    "MAX_DISAGREEMENT": "DISAGREEMENT",
}
_GATE_BLOCK_ALERTS = frozenset({
    "BUY_NO_EXTREME_SIGMA",
    "KLAX_BUY_NO_HIGH_SIGMA",
    "BUY_NO_LOW_BRACKET_TRAP_CONSENSUS",
    "BUY_NO_LOW_BRACKET_TRAP_RM_ABOVE",
})


def execute_opportunity(opp: dict) -> bool:
    """Enforce caps, place a real Kalshi limit-buy at the ask, wait up to
    ORDER_FILL_TIMEOUT_SEC for fill via WS cache, cancel any unfilled
    remainder, record actual fill_count + cost. Returns True on a real
    entry, False if any gate blocked or no fill landed.

    2026-04-30 PM: V2-style add-on path. Once a position partial-fills, the
    bot can re-attempt to fill the remaining gap on subsequent scans (up to
    the original Kelly-determined intended count, capped by remaining
    MAX_BET_USD budget). Same gate stack as entry — if model edge has
    eroded, no add-on. No add-on after partial-exit / hard-stop."""
    if opp.get("action") is None or opp.get("entry_price") is None:
        return False
    edge = float(opp["edge"])
    ticker = opp["market_ticker"]

    # Stale bracket gate — Kalshi keeps markets tradeable until CLI publishes,
    # which can lag bracket settlement by days (saw 26APR28 / 26MAY01 still
    # firing entries on 26MAY07). days_out < 0 means bracket date already
    # passed in opp.tz; no real edge, only retry churn.
    _do = _days_out_int(opp)
    if _do is not None and _do < 0:
        _audit_skip(opp, "STALE_BRACKET",
            f"  skip {ticker}: STALE_BRACKET — bracket {opp.get('date_str')} "
            f"already passed (days_out={_do}); awaiting CLI settlement")
        return False

    # Per-ticker check: existing position may be eligible for ADD-ON, or
    # block (settled / exited / fully-filled).
    is_addon = False
    addon_intended = 0     # original Kelly target
    addon_filled = 0       # contracts already owned
    addon_existing_cost = 0.0
    with _positions_lock:
        existing = _open_positions.get(ticker)
        if existing is not None:
            if existing.get("settled"):
                return False  # already settled, never re-enter
            if existing.get("exited_ts") is not None:
                return False  # was exited (hard-stop full fill); don't re-enter
            if existing.get("_partial_exit_count", 0) > 0:
                return False  # partial-exited via hard-stop; don't average back in
            cur_count = int(existing.get("count", 0))
            cur_cost = float(existing.get("cost", 0.0))
            # _intended_count missing on legacy records → treat as fully filled
            intended = int(existing.get("_intended_count", cur_count))
            if cur_count >= intended:
                return False  # already at intended size
            if cur_cost >= MAX_BET_USD:
                return False  # already at MAX_BET_USD; no further capacity
            # Eligible for add-on
            is_addon = True
            addon_intended = intended
            addon_filled = cur_count
            addon_existing_cost = cur_cost
            # 2026-05-07: V2-port adverse-addon gate. Block if fill price has
            # risen >= 5pp from first entry (BUY_NO/YES symmetric: entry_price
            # is what we pay regardless of action).
            _f1_first_px = existing.get("_first_entry_price")
            _f1_now_px = opp.get("entry_price")
            if (_f1_first_px is not None and _f1_now_px is not None
                    and (_f1_now_px - _f1_first_px) >= _FIRST_ENTRY_ADVERSE_ABS_THRESHOLD):
                _audit_skip(opp, "ADDON_ADVERSE_PRICE",
                    "  skip {}: addon blocked — entry_price {:.2f} - first {:.2f} = {:+.2f} >= {:.2f} (V2-port)".format(
                        ticker, _f1_now_px, _f1_first_px,
                        _f1_now_px - _f1_first_px,
                        _FIRST_ENTRY_ADVERSE_ABS_THRESHOLD))
                return False

    # _obs_confirmed_alive: rm has decisively settled the bracket in our favor.
    # When True, bypass forecast-based gates (directional, abs_dist, F2A, MSG,
    # disagreement, mu-vs-rm, mp range) and lower the edge floor. Only the
    # spread filter and budget gates still apply.
    obs_alive = _check_obs_confirmed_alive(opp)
    edge_floor = OBS_ALIVE_MIN_EDGE if obs_alive else MIN_EDGE
    if edge < edge_floor:
        return False

    # σ extracted for Kelly-shrink below. (σ-cap removed 2026-04-30 PM —
    # other defenses now cover the wide-σ disaster path; see SIGMA_REF_F docs.)
    sigma = float(opp.get("sigma") or 0)

    # Kelly sizing — anchor on bankroll (V2 fix; pre-fix anchored on MAX_BET_USD,
    # under-sizing every trade by ~4× when bankroll > MAX_BET_USD). Kelly boost
    # via SIGNAL_KELLY_MULT when obs_confirmed_alive (V2 _SIGNAL_KELLY_MULT port).
    # σ-aware shrink (2026-04-30): at SIGMA_REF_F, no shrink; quadratic
    # shrink as σ grows. At σ=4.0 → 39% of base; at σ=5.7 → 19% of base.
    # Recognizes that same-edge-with-wider-σ is a weaker signal.
    price = float(opp["entry_price"])
    kelly = KELLY_FRACTION * edge / max(1 - price, 0.01)
    sigma_shrink = min(1.0, (SIGMA_REF_F / max(sigma, 1.0)) ** 2)
    kelly *= sigma_shrink
    if obs_alive:
        kelly *= SIGNAL_KELLY_MULT
    bankroll = _get_bankroll_cached()
    if bankroll <= 0:
        # Cold start with no successful balance fetch (e.g. Kalshi 401-ing).
        # Refuse rather than size against a synthetic fallback. Caller will
        # try again next scan; balance refresh runs inside _get_bankroll_cached.
        _audit_skip(opp, "NO_BANKROLL",
            f"  skip {ticker}: no verified bankroll yet "
            f"(get_kalshi_balance returned None); refusing trade")
        return False
    # 2026-05-01: per-action MAX_BET cap. ALL BUY_YES entries capped at $5
    # (was tail-only; extended 2026-05-02 to B-brackets too after PHIL B49.5
    # BUY_YES became viable post-bracket-math-fix). BUY_NO uses MAX_BET_USD
    # ($30). Limits blast radius on the asymmetric loss profile of BUY_YES
    # (small wins, full-cost losses on forecast misses like DC-T46).
    _bet_action = opp.get("action")
    if _bet_action == "BUY_YES":
        _effective_max_bet = MAX_BET_BUY_YES_USD
    else:
        _effective_max_bet = MAX_BET_USD

    if is_addon:
        # Add-on path: skip fresh Kelly recompute. Fill the gap up to the
        # original intended count, capped by remaining budget (per-action
        # cap). If price moved up since first entry, max_count_by_budget
        # shrinks naturally — we never spend more than the cap per ticker.
        remaining_to_intended = max(0, addon_intended - addon_filled)
        remaining_budget_usd = max(0.0, _effective_max_bet - addon_existing_cost)
        max_count_by_budget = int(remaining_budget_usd / price) if price > 0 else 0
        count = min(remaining_to_intended, max_count_by_budget)
        if count < 1:
            return False  # nothing to add
    else:
        # Flat sizing for BUY_NO (2026-05-26): the model's confidence signals
        # don't predict per-contract outcome, so Kelly×bankroll just injects
        # noise that has landed on losers. Flat stake removes it. BUY_YES keeps
        # Kelly (already bounded tiny by MAX_BET_BUY_YES_USD).
        if FLAT_SIZING_NO_ENABLED and _bet_action == "BUY_NO":
            # gentle σ-haircut: shrink the flat stake when the forecast is
            # unreliable (wide σ). Tail-variance control on correlated
            # regime-bust nights. See MIN_NO_SIGMA_HAIRCUT_* constants.
            _no_hc = 1.0
            if MIN_NO_SIGMA_HAIRCUT_ENABLED:
                _no_sigma = float(opp.get("sigma") or 0.0)
                if _no_sigma >= MIN_NO_SIGMA_HAIRCUT_HI_THRESH:
                    _no_hc = MIN_NO_SIGMA_HAIRCUT_HI_MULT
                elif _no_sigma >= MIN_NO_SIGMA_HAIRCUT_LO_THRESH:
                    _no_hc = MIN_NO_SIGMA_HAIRCUT_LO_MULT
            bet_usd = FLAT_BET_NO_USD * _no_hc
        else:
            bet_usd = min(_effective_max_bet, max(MIN_BET_USD, kelly * bankroll))
        count = max(1, int(bet_usd / price))
        count = max(count, math.ceil(MIN_COST_USD / price))
        count = min(count, max(1, int(_effective_max_bet / price)))
    intended_cost = count * price
    action = opp["action"]
    mp = float(opp.get("model_prob", 0.0))

    # --- Forecast/market gates: SINGLE SOURCE OF TRUTH = _evaluate_gates ---
    # 2026-05-26 unification. _evaluate_gates replays EXACTLY this sequence
    # (OBS_CONFIRMED_LOSER through MU_VS_RM) and is the same evaluator the
    # candidate audit log uses, so the live and audit paths can no longer
    # drift (TestGatePathParity guards block + tag + alert equivalence). It
    # returns (None, "obs_alive_bypass") under obs_alive, preserving the
    # bypass; SPREAD + BUDGET below still apply regardless. MIN_EDGE (silent
    # return, above) and SPREAD (custom FILTERED_SPREAD logging, below) stay
    # inline, so they are excluded from the block handler here.
    blocked_by, block_reason = _evaluate_gates(opp)
    if blocked_by is not None and blocked_by not in ("NO_ACTION", "MIN_EDGE", "SPREAD"):
        _live_tag = _AUDIT_TO_LIVE_DECISION.get(blocked_by, blocked_by)
        if blocked_by in _GATE_BLOCK_ALERTS:
            _gf = opp.get("floor"); _gc = opp.get("cap")
            _gbr = ("[{},{}]".format(_gf, _gc) if _gf is not None and _gc is not None
                    else (">={}".format(_gf) if _gc is None else "<={}".format(_gc)))
            _discord_skip_send(ticker,
                f"BLOCKED {action} `{ticker}` ({opp.get('label','?')}) - "
                f"{_live_tag}: {block_reason} | mu {(opp.get('mu') or 0):.1f}F "
                f"sigma {(opp.get('sigma') or 0):.2f} bracket {_gbr} "
                f"src={opp.get('mu_source')} "
                f"NBP={opp.get('mu_nbp')} HRRR={opp.get('mu_hrrr')} NBM={opp.get('mu_nbm_om')}")
        # Keep the live decision name in the log text so log/audit greps by
        # tag still work (the per-gate inline messages used to include it).
        _audit_skip(opp, _live_tag, f"  skip {ticker}: {_live_tag} — {block_reason}")
        return False
    if obs_alive:
        log(f"  OBS_CONFIRMED_ALIVE {ticker}: bypassing forecast gates "
            f"(rm={opp.get('running_min')}, action={action}); kelly x{SIGNAL_KELLY_MULT}")
    # Spread filter — always applies (even on obs_alive bypass; thin books are
    # untradable regardless of obs confirmation).
    side = "yes" if action == "BUY_YES" else "no"
    if side == "yes":
        ya = opp.get("yes_ask"); yb = opp.get("yes_bid")
        spread = (ya - yb) if (ya is not None and yb is not None) else 0
    else:
        na = opp.get("no_ask"); nb = opp.get("no_bid")
        spread = (na - nb) if (na is not None and nb is not None) else 0
    if spread > MAX_SPREAD_CENTS:
        _log_skip(ticker, f"  skip {ticker}: spread {spread}c > {MAX_SPREAD_CENTS}c")
        # 2026-05-04: cross-bot decision audit log — Phase B min_bot wiring.
        # min_bot uses cents (yes_ask − yes_bid) instead of fraction; convert
        # to fraction for cross-bot consistency in the schema.
        if _dlog is not None:
            try:
                _dlog.record(
                    bot='min_bot', stage='filter', decision='FILTERED_SPREAD',
                    ticker=ticker,
                    series=opp.get('series'),
                    settlement_date=opp.get('date_str'),
                    station=opp.get('station'),
                    floor=opp.get('floor'), cap=opp.get('cap'),
                    days_out=_days_out_int(opp),
                    next_action=action,
                    filter_name='SPREAD',
                    filter_value=spread / 100.0,  # cents → fraction
                    filter_threshold=MAX_SPREAD_CENTS / 100.0,
                    yes_bid=(opp.get('yes_bid') / 100.0) if opp.get('yes_bid') is not None else None,
                    yes_ask=(opp.get('yes_ask') / 100.0) if opp.get('yes_ask') is not None else None,
                    no_bid=(opp.get('no_bid') / 100.0) if opp.get('no_bid') is not None else None,
                    no_ask=(opp.get('no_ask') / 100.0) if opp.get('no_ask') is not None else None,
                    spread=spread / 100.0,
                    model_prob=opp.get('model_prob'),
                    edge=edge,
                    mu_blended=opp.get('mu'),
                    sigma_final=opp.get('sigma'),
                    nbp_mu=opp.get('mu_nbp'), nbp_sigma=opp.get('sigma_nbp'),
                    hrrr_high=opp.get('mu_hrrr'),
                    nbm_high=opp.get('mu_nbm_om'),
                    running_min=opp.get('running_min'),
                    is_addon=is_addon,
                )
            except Exception:
                pass
        return False
    # Per-cycle / daily / per-event budget.
    ok, reason = _budget_can_take(intended_cost, opp.get("event_ticker", ""),
                                   is_addon=is_addon)
    if not ok:
        _audit_skip(opp, "BUDGET", f"  skip {ticker}: {reason}")
        return False
    price_cents = int(round(price * 100))
    order_id = place_kalshi_order(ticker, side, count, price_cents)
    if not order_id:
        return False
    status, filled = wait_for_fill(order_id, count, ORDER_FILL_TIMEOUT_SEC)
    if filled <= 0:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{order_id}")
        except Exception as _cx:
            # noqa: should_log — cancel is best-effort; Kalshi auto-GCs stale
            # orders, but a persistent cancel-failure can leak ghost orders
            # against bankroll headroom. Log so it's visible.
            log(f"  cancel failed for {order_id} on {ticker}: {type(_cx).__name__}: {_cx}", "warn")
        log(f"  no fill on {ticker} (status={status}); cancelled")
        return False
    if filled < count:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{order_id}")
        except Exception as _cx:
            # noqa: should_log — see above; partial-fill remainder cancel.
            log(f"  cancel-remainder failed for {order_id} on {ticker}: {type(_cx).__name__}: {_cx}", "warn")
        log(f"  partial fill {filled}/{count} on {ticker}; cancelled remainder")
    actual_cost = filled * price

    # 2026-05-03 LADDER CHASE — close the under-fill gap on thin Kalshi books.
    # First-entry partial fills at the ask were leaving 80%+ of intended Kelly
    # orphaned (PHX-MAY04 1/34, AUS-MAY03 2/26, SFO-MAY03 10/49). Walk the ask
    # up by 1c at a time, re-checking edge at the new price. Stop when:
    #   - remainder filled
    #   - new edge below MIN_EDGE (Chris's gate — preserve the existing entry bar)
    #   - cumulative cost would breach the action's per-trade cap
    #   - LADDER_MAX_RETRIES hit (latency cap)
    # 2026-05-04: extended from BUY_NO-only to BUY_YES too. BUY_YES uses the
    # yes_ask side and the $5 MAX_BET_BUY_YES_USD cap; chase budget is small
    # but the orphan rate on min-temp BUY_YES (e.g. AUS-MAY04 1/9) was the
    # same structural issue. Add-ons still iterate via the existing scan loop.
    if (filled > 0 and filled < count and not is_addon
            and action in ("BUY_NO", "BUY_YES")
            and edge >= MIN_EDGE):
        mp_yes = float(opp.get("model_prob") or 0)
        last_ask_c = price_cents
        cumulative_filled = filled
        cumulative_cost = actual_cost
        # Per-action ladder config: which side's ask to chase, which cap to
        # respect, and how to recompute edge at the new price.
        if action == "BUY_NO":
            _ask_field = "no_ask_dollars"
            _max_cap_usd = MAX_BET_USD
        else:  # BUY_YES
            _ask_field = "yes_ask_dollars"
            _max_cap_usd = MAX_BET_BUY_YES_USD
        # 2026-05-04 OVERFILL CAP: pessimistic worst-case spend tracker.
        # `cumulative_cost` is the bot's tally based on wait_for_fill, which
        # can lag actual Kalshi state if the order endpoint hasn't propagated
        # the fill at fetch time (V1 PHX-26MAY04-B81.5: bot saw 6 fills /
        # $3.12, reality 78 / $41.58 — kalshi_get returned fill_count_fp=0
        # 100ms after place_order on a fresh-executed IOC rung). Bound damage
        # by tracking the WORST-CASE cost (assume every placed order fills in
        # full at its placement price) and refusing the next rung if that
        # pessimistic total exceeds {action_cap} * 1.05.
        _placed_max_cost = count * (price_cents / 100.0)
        _LADDER_BUDGET_CAP = _max_cap_usd * 1.05
        for _retry in range(LADDER_MAX_RETRIES):
            remainder = count - cumulative_filled
            if remainder <= 0:
                break
            try:
                _md = kalshi_get(f"/trade-api/v2/markets/{ticker}").get("market", {})
                _new_ask_c = int(round(float(_md.get(_ask_field, 0)) * 100))
            except Exception as _le:
                log(f"  ladder fetch failed on {ticker}: {type(_le).__name__}", "warn")
                break
            if _new_ask_c <= last_ask_c:
                _new_ask_c = last_ask_c + 1  # ensure progress against stale quote
            _new_price = _new_ask_c / 100.0
            if action == "BUY_NO":
                _new_edge = 1.0 - mp_yes - _new_price
            else:  # BUY_YES
                _new_edge = mp_yes - _new_price
            if _new_edge < MIN_EDGE:
                log(f"  ladder stop {ticker}: new_edge={_new_edge:.2%} < MIN_EDGE "
                    f"at {_new_ask_c}c (filled {cumulative_filled}/{count})")
                break
            # Pessimistic budget = max(bot tally, placed-orders worst case)
            _pessimistic_cost = max(cumulative_cost, _placed_max_cost)
            _budget_left = _LADDER_BUDGET_CAP - _pessimistic_cost
            if _budget_left < _new_price:
                log(f"  ladder stop {ticker}: at {action} cap "
                    f"(cum=${cumulative_cost:.2f}, "
                    f"placed_max=${_placed_max_cost:.2f}, "
                    f"ask {_new_ask_c}c, cap=${_max_cap_usd:.2f})")
                break
            _max_count = min(remainder, int(_budget_left / _new_price))
            if _max_count < 1:
                break
            _retry_id = place_kalshi_order(ticker, side, _max_count, _new_ask_c)
            if not _retry_id:
                break
            # 2026-05-04 OVERFILL CAP: bump worst-case-cost tracker immediately
            # after successful place_order, before any fill-detection race.
            _placed_max_cost += _max_count * _new_price
            _r_status, _r_filled = wait_for_fill(_retry_id, _max_count, ORDER_FILL_TIMEOUT_SEC)
            if _r_filled > 0:
                if _r_filled < _max_count:
                    try:
                        kalshi_delete(f"/trade-api/v2/portfolio/orders/{_retry_id}")
                    except Exception as _cx:
                        log(f"  ladder cancel-remainder failed for {_retry_id} on {ticker}: "
                            f"{type(_cx).__name__}: {_cx}", "warn")
                cumulative_filled += _r_filled
                cumulative_cost += _r_filled * _new_price
                log(f"  LADDER +{_r_filled}x @ {_new_ask_c}c on {ticker} "
                    f"(cum {cumulative_filled}/{count}, ${cumulative_cost:.2f}, "
                    f"new_edge={_new_edge:.2%})", "trade")
            else:
                try:
                    kalshi_delete(f"/trade-api/v2/portfolio/orders/{_retry_id}")
                except Exception as _cx:
                    log(f"  ladder cancel failed for {_retry_id} on {ticker}: "
                        f"{type(_cx).__name__}: {_cx}", "warn")
                # 2026-05-04 OPTION B (originally commit 5013a93, silently
                # reverted by db6168c PER_SERIES routing-audit commit 3 min
                # later — rebase/merge mishap; re-applied 2026-05-10):
                # don't stop on first 0-fill price level. Kalshi can have 0
                # contracts at one cent and >0 at the next. Canonical case:
                # PHX-26MAY04-B64.5 filled 1 at 50c on 5/3 20:39, ladder
                # fetched ask=51c, got 0 fill, broke, left 33 contracts
                # orphaned and the position stuck at 1/34 for 10+ hours while
                # the market re-priced toward NO=94c (rendering all subsequent
                # addons unprofitable). Removing this break lets the ladder
                # walk the ask up to LADDER_MAX_RETRIES cents above initial
                # fill, with the existing edge-floor + MAX_BET-cap +
                # remainder-zero terminators still bounding the loop. Risk is
                # bounded: max walk is LADDER_MAX_RETRIES (now 5) cents above
                # the initial fill price, so worst-case extra cost per
                # contract is ~5c at the same edge requirement.
                log(f"  ladder no-fill on {ticker} @ {_new_ask_c}c — trying next rung")
            last_ask_c = _new_ask_c
        # Update fill totals after ladder. _budget_record below uses these,
        # and trade_record records weighted-avg price (matches addon convention).
        if cumulative_filled > filled:
            filled = cumulative_filled
            actual_cost = cumulative_cost
            price = actual_cost / filled if filled > 0 else price
    _budget_record(actual_cost, opp.get("event_ticker", ""),
                   is_addon=is_addon)
    # Per-order trade record (always reflects THIS order, not cumulative
    # position state — keeps trades.jsonl as a clean per-order audit log).
    # 2026-05-01: entry-context enrichment (V2-parity). The settlement record
    # needs everything required to retro-evaluate filter ideas (catching-knife,
    # late-day, per-station bias, etc.). All fields below are captured at
    # ENTRY time and flow through pos → settlement record unchanged.
    _entry_tz_name = opp.get("tz")
    _entry_local_hour = None
    _entry_local_dow = None
    _entry_local_ts = None
    _entry_hours_to_sunrise = None
    if _entry_tz_name:
        try:
            _now_local = datetime.now(ZoneInfo(_entry_tz_name))
            _entry_local_hour = _now_local.hour
            _entry_local_dow = _now_local.strftime("%a")
            _entry_local_ts = _now_local.isoformat(timespec="seconds")
        except Exception:
            pass
        try:
            _lat = opp.get("lat"); _lon = opp.get("lon")
            if _lat is not None and _lon is not None:
                _entry_hours_to_sunrise = round(_hours_to_sunrise(
                    _entry_tz_name, float(_lat), float(_lon)), 2)
        except Exception:
            pass
    trade_record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "entry",
        "market_ticker": ticker, "action": opp["action"],
        "entry_price": price, "count": filled, "cost": actual_cost,
        "order_id": order_id,
        "edge": edge, "model_prob": opp["model_prob"],
        "mu": opp["mu"], "sigma": opp["sigma"], "mu_source": opp["mu_source"],
        "running_min": opp["running_min"],
        "floor": opp.get("floor"), "cap": opp.get("cap"),
        "station": opp["station"], "date_str": opp["date_str"], "label": opp["label"],
        # ─── ENTRY AWARENESS (2026-05-01) ───
        "entry_local_hour": _entry_local_hour,
        "entry_local_dow": _entry_local_dow,
        "entry_local_ts": _entry_local_ts,
        "entry_hours_to_sunrise": _entry_hours_to_sunrise,
        "entry_tz": _entry_tz_name,
        # Market quotes at entry (cents, 0-100). yes_ask/no_ask are the prices
        # we'd pay; yes_bid/no_bid are the resting bids (counterparty side).
        "entry_yes_bid_cents": opp.get("yes_bid"),
        "entry_yes_ask_cents": opp.get("yes_ask"),
        "entry_no_bid_cents":  opp.get("no_bid"),
        "entry_no_ask_cents":  opp.get("no_ask"),
        "entry_volume":        opp.get("volume"),
        "entry_spread_cents":  ((opp.get("yes_ask") or 0) - (opp.get("yes_bid") or 0))
                               if (opp.get("yes_ask") is not None and opp.get("yes_bid") is not None)
                               else None,
        # Forecast cluster detail (V2-equivalent ens_*, gfs_*, ecmwf_* etc.)
        "mu_nbp_at_entry":     opp.get("mu_nbp"),
        "sigma_nbp_at_entry":  opp.get("sigma_nbp"),
        "mu_nbm_om_at_entry":  opp.get("mu_nbm_om"),
        "mu_hrrr_at_entry":    opp.get("mu_hrrr"),
        "disagreement_at_entry": opp.get("disagreement"),
        "primary_outlier_diff_at_entry": _compute_primary_outlier_diff(opp),
        "post_sunrise_lock_at_entry": opp.get("post_sunrise_lock"),
        "is_today_at_entry":   opp.get("is_today"),
        # 2026-05-02 data-capture additions: integer days_out (companion to
        # is_today_at_entry boolean which collapses d-1/d-2/d-3 together)
        # and per-source forecast freshness ages in minutes (captures
        # stale-forecast patterns at entry time for future bias studies).
        "days_out":            _days_out_int(opp),
        "nbp_age_min":         _cache_entry_age_min(_nbp_cache, _nbp_cache_lock,
                                                    opp.get("station"), opp.get("date_str")),
        "hrrr_age_min":        _cache_entry_age_min(_hrrr_cache, _hrrr_cache_lock,
                                                    opp.get("series"), opp.get("date_str")),
        "nbm_om_age_min":      _cache_entry_age_min(_nbm_om_cache, _nbm_om_cache_lock,
                                                    opp.get("series"), opp.get("date_str")),
        # Edge breakdown (mirrors V2's edge / edge_vs_mid)
        "yes_ask_frac_at_entry": opp.get("yes_ask_frac"),
        "no_ask_frac_at_entry":  opp.get("no_ask_frac"),
        "_is_addon": is_addon,
    }
    _append_jsonl(_trades_file_today(), trade_record)

    # 2026-05-04: cross-bot decision audit log — Phase B min_bot wiring.
    # Mirrors trade_record above with consistent column names; queryable
    # via SQL alongside V1+V2 entries in bot_decisions.sqlite.
    if _dlog is not None:
        try:
            _dlog.record(
                bot='min_bot', stage='exec', decision='ENTRY_FILLED',
                ticker=ticker,
                series=opp.get('series'),
                settlement_date=opp.get('date_str'),
                station=opp.get('station'),
                floor=opp.get('floor'), cap=opp.get('cap'),
                days_out=_days_out_int(opp),
                local_hour=_entry_local_hour,
                next_action=opp['action'],
                # Quotes (cents → fraction)
                yes_bid=(opp.get('yes_bid') / 100.0) if opp.get('yes_bid') is not None else None,
                yes_ask=(opp.get('yes_ask') / 100.0) if opp.get('yes_ask') is not None else None,
                no_bid=(opp.get('no_bid') / 100.0) if opp.get('no_bid') is not None else None,
                no_ask=(opp.get('no_ask') / 100.0) if opp.get('no_ask') is not None else None,
                spread=trade_record.get('entry_spread_cents') / 100.0
                       if trade_record.get('entry_spread_cents') is not None else None,
                volume_24h=opp.get('volume'),
                # Model
                model_prob=opp['model_prob'],
                edge=edge,
                mu_blended=opp['mu'],
                sigma_final=opp['sigma'],
                # Forecast sources
                nbp_mu=opp.get('mu_nbp'), nbp_sigma=opp.get('sigma_nbp'),
                hrrr_high=opp.get('mu_hrrr'),
                nbm_high=opp.get('mu_nbm_om'),
                disagree_ratio=opp.get('disagreement'),
                # Obs
                running_min=opp.get('running_min'),
                # Freshness ages
                hrrr_age_min=trade_record.get('hrrr_age_min'),
                nbm_age_min=trade_record.get('nbm_om_age_min'),
                nbp_age_min=trade_record.get('nbp_age_min'),
                # Execution
                fill_count=filled,
                actual_cost=actual_cost,
                order_id=order_id,
                fill_price_cents=int(round(price * 100)),
                # Context
                is_addon=is_addon,
            )
        except Exception:
            pass

    cumulative_count = filled
    cumulative_cost = actual_cost
    with _positions_lock:
        if is_addon:
            existing = _open_positions.get(ticker)
            if existing is None:
                # Defensive: another thread cleared the position between
                # eligibility check and fill arrival. Treat this fill as a
                # standalone first entry. Stamp _intended_count = filled so
                # we don't try to add more on top of an unknown plan.
                position_record = dict(trade_record)
                position_record["_intended_count"] = filled
                position_record["_filled_count"] = filled
                position_record["_n_orders"] = 1
                position_record["_is_addon"] = False
                position_record["_first_entry_price"] = float(price)
                _open_positions[ticker] = position_record
            else:
                # Weighted-avg entry_price, accumulated count + cost.
                # Stat fields (edge / mp / mu / sigma / running_min) are kept
                # as the FIRST-entry snapshot — settlement records read them
                # as `running_min_at_entry`. Hard-stop reads running_min from
                # pos but falls back to live get_running_min when missing.
                new_count = int(existing.get("count", 0)) + filled
                new_cost = float(existing.get("cost", 0.0)) + actual_cost
                existing["count"] = new_count
                existing["cost"] = new_cost
                existing["entry_price"] = (
                    new_cost / new_count) if new_count > 0 else price
                existing["_filled_count"] = new_count
                existing["_n_orders"] = int(existing.get("_n_orders", 1)) + 1
                existing["_last_addon_ts"] = trade_record["ts"]
                existing["_last_addon_price"] = price
                existing["_last_addon_count"] = filled
                cumulative_count = new_count
                cumulative_cost = new_cost
        else:
            # First entry: stamp _intended_count for future add-ons. Note
            # `count` here is the pre-fill kelly target — the actual fill
            # may be smaller (partial). Future scans use _intended_count to
            # know the gap they're allowed to fill.
            position_record = dict(trade_record)
            position_record["_intended_count"] = count
            position_record["_filled_count"] = filled
            position_record["_n_orders"] = 1
            # 2026-05-07: stamp first-fill entry_price for adverse-addon gate.
            # Never overwritten on addons (entry_price gets weighted-averaged).
            position_record["_first_entry_price"] = float(price)
            _open_positions[ticker] = position_record
    _save_positions()
    if is_addon:
        log(f"  ADDON {opp['action']} +{filled}x @ {price_cents}c on {ticker} "
            f"| now {cumulative_count}/{addon_intended} contracts "
            f"| +${actual_cost:.2f} cum ${cumulative_cost:.2f} "
            f"| edge={edge:.1%} mp={opp['model_prob']:.0%} mu={opp['mu']:.1f}°F "
            f"σ={opp['sigma']:.1f}°F rm={opp['running_min']} "
            f"| day=${_today_exposure_usd:.2f}/${DAILY_EXPOSURE_CAP_USD:.2f}")
        discord_send(
            f"➕ **ADDON** {opp['action']} +{filled}x @ {price_cents}c "
            f"on `{ticker}` ({opp['label']})\n"
            f"now {cumulative_count}/{addon_intended}  edge {edge:.0%}  "
            f"mp {opp['model_prob']:.0%}  μ {opp['mu']:.1f}°F  σ {opp['sigma']:.1f}°F  "
            f"rm {opp['running_min']}  +${actual_cost:.2f} (cum ${cumulative_cost:.2f})"
        )
    else:
        log(f"  ENTRY {opp['action']} {filled}x @ {price_cents}c on {ticker} "
            f"| edge={edge:.1%} mp={opp['model_prob']:.0%} mu={opp['mu']:.1f}°F "
            f"σ={opp['sigma']:.1f}°F rm={opp['running_min']} "
            f"| day=${_today_exposure_usd:.2f}/${DAILY_EXPOSURE_CAP_USD:.2f}")
        notify_discord_entry(trade_record, opp)
    return True


# ═══════════════════════════════════════════════════════════════════════
# MID-CYCLE EXIT (V2 PORT: HARD STOP + OBS-WINNER OVERRIDE)
# ═══════════════════════════════════════════════════════════════════════

def _check_position_obs_winning(pos: dict, rm: float) -> bool:
    """True if the running_min observation already confirms our position is
    winning. Used as override against hard-stop on confirmed winners
    (mirror of V2's _obs_confirmed_winner). Decision rules: same as
    `_check_obs_confirmed_alive` but with a smaller (1°F) buffer since
    `pos` is an existing holding — we're not deciding whether to take a new
    bet, we're deciding whether to sell what we hold."""
    floor = pos.get("floor")
    cap = pos.get("cap")
    action = pos.get("action")
    if action == "BUY_NO":
        # B-bracket: rm well below bracket → NO wins
        if floor is not None and cap is not None:
            if rm < float(floor) - 1.0:
                return True
            # 2026-05-29: warm-side winner (Chris). Running min locked ABOVE the
            # bracket (low stayed warmer than [floor,cap]) -> BUY_NO wins. Mirror of
            # entry-side _check_obs_confirmed_alive (~L3766). POST-LOW-LOCK gated:
            # rm (running min) can still fall into the bracket pre-lock, so only
            # confirm a warm winner after the low is set (_is_post_sunrise, >=8AM local).
            _tz_w = pos.get("tz") or _STATION_TZ.get(pos.get("station"), "America/New_York")
            if rm > float(cap) + 1.0 and _is_post_sunrise(_tz_w):
                return True
        # T-high (single-bound floor): rm below threshold → NO wins
        elif floor is not None and cap is None:
            if rm < float(floor) - 1.0:
                return True
        # T-low BUY_NO winner case requires post-sunrise; defer.
    elif action == "BUY_YES":
        # T-low: rm is monotonically decreasing — once it dips below cap it
        # cannot recover; YES is locked.
        if cap is not None and floor is None:
            if rm <= float(cap) - 1.0:
                return True
        # T-high: NO override — symmetric with `_check_obs_confirmed_alive`.
        # Removed 2026-04-28: rm above floor right now does not survive
        # overnight cooling within the same climate day. Without the override
        # the hard-stop will run normally; that's the conservative choice for
        # a position whose underlying low is not yet locked.
    return False


# 2026-05-04 night SHADOW deploy: OBS_CONFIRMED_LOSER exit detection.
# STRICTER than entry-side `_check_obs_confirmed_loser` because false
# positives at exit-time mean SELLING A WINNER (much worse than entry-time
# false positives which just mean missing a trade). Three safety layers:
#
# 1. Post-low-lock time gate (hour >= 8 local, _is_post_sunrise). For LOW-
#    temp brackets, the daily low locks before sunrise. After 8 AM local,
#    rm is essentially the day's final low.
# 2. 1°F CLI-rounding buffer matching the existing WINNER-override
#    convention (rm < floor - 1.0 = winner). For LOSER, the symmetric
#    boundary uses rm > floor + 1.0 in some cases — see per-case logic.
# 3. TZ-exception fail-safe: if we can't determine local time, return
#    False (don't sell). Conservative default.
#
# THIS FUNCTION IS CURRENTLY USED FOR SHADOW LOGGING ONLY. The
# `check_open_positions_for_exit` integration logs `OBS_CONFIRMED_LOSER
# would-fire` events but does not call `_execute_exit`. Stage-2 activation
# requires:
#   - 7-14 days of shadow data
#   - 0 false positives (no winner mistakenly flagged)
#   - estimated $ recovery > $30/wk to justify even small false-positive risk
def _check_position_obs_confirmed_loser_for_exit(pos: dict, rm: float) -> bool:
    """STRICT loser detection for exit-side use.

    Decision rules per (action, bracket-shape) — all require post-sunrise
    (hour >= 8 local) for the low-temp confirmation:

      BUY_NO + B-bracket [floor, cap]:
        rm >= floor + 1.0 AND rm <= cap (rm firmly in or below upper edge,
        with 1°F buffer below floor to absorb ASOS-vs-METAR -1°F noise).

      BUY_NO + T-cap (cap-only, "low ≤ X-1"):
        rm <= cap - 1.0 (CLI definitively rounds to X-1 or lower).

      BUY_NO + T-floor (floor-only, "low ≥ X"):
        rm > floor + 1.0 (1°F buffer above floor).

      BUY_YES + B-bracket [floor, cap]:
        rm < floor - 0.5 (rm only decreases, won't recover) — NO post-
          sunrise gate needed since the trajectory is monotone.
        OR rm > cap + 1.5 AND post-low-lock (low locked above bracket;
          1.5°F buffer = 1.0°F CLI safety + 0.5°F obs noise).

      BUY_YES + T-low (cap-only, "low ≤ X"):
        rm > cap + 0.5 AND post-low-lock (low has set above threshold).

      BUY_YES + T-high (floor-only, "low ≥ X"):
        rm < floor - 0.5 (rm only decreases, won't recover).

    Returns False on ANY exception (TZ lookup failure, missing fields).
    Better to miss a recovery opportunity than sell a winner.
    """
    try:
        floor = pos.get("floor")
        cap = pos.get("cap")
        action = pos.get("action")
        tz = pos.get("tz") or pos.get("entry_tz") or "America/New_York"

        # 2026-05-06: cli-align rm using METAR-precision when station+date
        # are present. CRITICAL exit-side path — false-positive losers here
        # mean SELLING A WINNER. The integer-°C round-down on madis_fsl2 can
        # make rm appear ~0.5°F colder than CLI; aligned rm uses awc/ldm
        # T-group precision matching what Kalshi will settle on.
        _station = pos.get("station")
        _date_str = pos.get("date_str")
        if USE_CLI_ALIGNED_RMIN and _station and _date_str:
            _aligned = _cli_aligned_rmin(rm, _station, _date_str)
            if _aligned is not None:
                rm = _aligned

        # Compute post-low-lock once. Used by most branches.
        try:
            post_lock = _is_post_sunrise(tz)
        except Exception:
            return False  # fail-safe

        if action == "BUY_NO":
            if floor is not None and cap is not None:
                # B-bracket: rm IN [floor + 1.0, cap]. The +1.0 buffer
                # mirrors the WINNER override's -1.0 buffer (rm < floor - 1.0
                # = winner), keeping a 2°F dead zone between WINNER and
                # LOSER for cases where CLI-vs-obs gap could flip the
                # outcome.
                if post_lock and (float(floor) + 1.0) <= rm <= float(cap):
                    return True
            elif cap is not None and floor is None:
                # T-cap: rm <= cap - 1.0. Means actual low is solidly
                # below the threshold; CLI will round to X-1 or lower.
                if post_lock and rm <= float(cap) - 1.0:
                    return True
            elif floor is not None and cap is None:
                # T-floor (BUY_NO bets low < X): rm > floor + 1.0.
                if post_lock and rm > float(floor) + 1.0:
                    return True
        elif action == "BUY_YES":
            if floor is not None and cap is not None:
                # B-bracket: low went below floor (won't recover, rm
                # monotone) OR low locked above cap+1.5 with post-lock.
                if rm < float(floor) - 0.5:
                    return True
                if post_lock and rm > float(cap) + 1.5:
                    return True
            elif cap is not None and floor is None:
                # T-low (YES bets low ≤ X): rm > cap + 0.5 means low has
                # set above threshold. Post-lock ensures it's final.
                if post_lock and rm > float(cap) + 0.5:
                    return True
            elif floor is not None and cap is None:
                # T-high (YES bets low ≥ X): rm < floor - 0.5 means low
                # has gone below threshold. rm monotone, no need for lock.
                if rm < float(floor) - 0.5:
                    return True
    except Exception:
        return False  # fail-safe: any unexpected error → don't sell
    return False


def _check_narrow_margin_tp(pos: dict, rm: Optional[float],
                             current_bid_c: Optional[int]) -> bool:
    """NARROW_MARGIN_TP gate: take profit on at-risk-zone BUY_NO B-bracket
    positions. Fires iff ALL of:
      - NARROW_MARGIN_TP_ENABLED (kill switch)
      - action == BUY_NO + B-bracket (both floor and cap)
      - station ∈ NARROW_MARGIN_TP_STATIONS (radiative-cooling-prone)
      - rm in (cap, cap + NARROW_MARGIN_TP_MARGIN_F]  — at-risk zone
      - bid in [NARROW_MARGIN_TP_MIN_BID_C, NARROW_MARGIN_TP_MAX_BID_C]
      - MTM ≥ NARROW_MARGIN_TP_MIN_MTM
    See NARROW_MARGIN_TP_* constant block for audit + rationale."""
    if not NARROW_MARGIN_TP_ENABLED:
        return False
    if pos.get("action") != "BUY_NO":
        return False
    floor = pos.get("floor")
    cap = pos.get("cap")
    if floor is None or cap is None:
        return False
    if pos.get("station") not in NARROW_MARGIN_TP_STATIONS:
        return False
    if rm is None or current_bid_c is None:
        return False
    try:
        margin = float(rm) - float(cap)
    except (TypeError, ValueError):
        return False
    if margin <= 0 or margin > NARROW_MARGIN_TP_MARGIN_F:
        return False
    bid = int(current_bid_c)
    if bid < NARROW_MARGIN_TP_MIN_BID_C or bid > NARROW_MARGIN_TP_MAX_BID_C:
        return False
    entry = float(pos.get("entry_price", 0) or 0)
    if entry <= 0:
        return False
    mtm = (bid / 100.0 - entry) / entry
    return mtm >= NARROW_MARGIN_TP_MIN_MTM


def _check_take_profit_15(pos: dict, current_bid_c: Optional[int],
                           now_local: Optional[datetime] = None) -> bool:
    """TAKE_PROFIT_15 gate: lock in BUY_NO gains when MTM/cost ≥ 15% AND
    local time ≥ 10:30 LST. See TAKE_PROFIT_15_* constant block for audit.

    Args:
      pos: open position dict (must have action/count/cost/entry_price/tz)
      current_bid_c: current bid on sell-side (no_bid for BUY_NO)
      now_local: tz-aware datetime in the position's local tz; if None,
                 computed from pos['tz'] (helps testing without freezegun)."""
    if not TAKE_PROFIT_15_ENABLED:
        return False
    if pos.get("action") != "BUY_NO":
        return False
    if current_bid_c is None:
        return False
    try:
        bid = int(current_bid_c)
    except (TypeError, ValueError):
        return False
    if bid <= 0:
        return False
    try:
        qty = int(pos.get("count", 0))
        cost = float(pos.get("cost", 0) or 0)
    except (TypeError, ValueError):
        return False
    if qty <= 0 or cost <= 0:
        return False
    mtm_usd = qty * (bid / 100.0) - cost
    if mtm_usd / cost < TAKE_PROFIT_15_MIN_MTM_PCT:
        return False
    if now_local is None:
        tz_name = pos.get("tz") or pos.get("entry_tz") or "America/New_York"
        try:
            now_local = datetime.now(ZoneInfo(tz_name))
        except Exception:
            return False
    h, m = now_local.hour, now_local.minute
    if h < TAKE_PROFIT_15_MIN_LOCAL_HOUR:
        return False
    if h == TAKE_PROFIT_15_MIN_LOCAL_HOUR and m < TAKE_PROFIT_15_MIN_LOCAL_MIN:
        return False
    return True


def _execute_exit(ticker: str, pos: dict, sell_side: str, sell_price_c: int,
                   reason: str, max_count: Optional[int] = None) -> bool:
    """Place a SELL order for an existing position at sell_price_c. Polls for
    fill, records exit in trades.jsonl with kind='exit', marks position
    settled in the dedupe map. Returns True iff we got any fill."""
    true_count = int(pos.get("count", 0))
    if true_count <= 0:
        return False
    if sell_price_c is None or sell_price_c <= 0:
        return False
    # 2026-05-26: max_count caps THIS order to a clip (paced 10am exit) without
    # touching the position's true size; settle logic below keys off true_count.
    order_count = min(true_count, int(max_count)) if max_count else true_count
    oid = place_kalshi_sell_order(ticker, sell_side, order_count, sell_price_c)
    if not oid:
        return False
    status, filled = wait_for_fill(oid, order_count, ORDER_FILL_TIMEOUT_SEC)
    if filled <= 0:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{oid}")
        except Exception as _cx:
            # noqa: should_log — exit cancel is best-effort; logging the
            # underlying failure makes ghost-order leaks investigable.
            log(f"  exit cancel failed for {oid} on {ticker}: {type(_cx).__name__}: {_cx}", "warn")
        log(f"  exit no-fill on {ticker} (status={status}); cancelled")
        return False
    if filled < order_count:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{oid}")
        except Exception as _cx:
            # noqa: should_log — exit partial-fill remainder cancel.
            log(f"  exit cancel-remainder failed for {oid} on {ticker}: {type(_cx).__name__}: {_cx}", "warn")
        log(f"  exit partial fill {filled}/{order_count} on {ticker}; cancelled remainder")
    sell_revenue = filled * (sell_price_c / 100.0)
    cost_basis = filled * float(pos.get("entry_price", 0))
    pnl = sell_revenue - cost_basis
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "exit",
        "market_ticker": ticker,
        "action": pos.get("action"),
        "exit_side": sell_side,
        "exit_price": sell_price_c / 100.0,
        "count": filled,
        "entry_price": float(pos.get("entry_price", 0)),
        "sell_revenue": sell_revenue,
        "cost_basis": cost_basis,
        "pnl": pnl,
        "reason": reason,
        "order_id": oid,
        "station": pos.get("station"),
        "date_str": pos.get("date_str"),
        "label": pos.get("label"),
    }
    _append_jsonl(_trades_file_today(), record)
    with _positions_lock:
        existing = _open_positions.get(ticker)
        if existing is not None:
            if filled < true_count:
                # Partial fill — DO NOT mark settled. The unsold contracts
                # are still ours on Kalshi and need to either be re-sold by
                # the next hard-stop check or settled by check_settlements
                # at climate-day end.
                # 2026-04-30: pre-fix bug — bot marked settled=True after a
                # partial-fill exit, orphaning AUS-26MAY01-T56's 79 unsold
                # contracts (entry $29.70 → bot reported pnl=-$2.86, missed
                # $20.54 of unrealized exposure that settles tomorrow).
                new_count = true_count - filled
                # Keep entry_price unchanged (cost basis per contract).
                # Reduce `count` so check_settlements computes settlement
                # against just the remaining contracts. Append-style accumulate
                # the partial-exit pnl in case multiple partials happen.
                existing["count"] = new_count
                existing["cost"] = new_count * float(existing.get("entry_price", 0))
                existing["_partial_exit_pnl"] = (
                    existing.get("_partial_exit_pnl", 0.0) + pnl)
                existing["_partial_exit_count"] = (
                    existing.get("_partial_exit_count", 0) + filled)
                existing["_partial_exit_last_ts"] = record["ts"]
                existing["_partial_exit_last_price"] = record["exit_price"]
                existing["_partial_exit_last_reason"] = reason
            else:
                # Full fill — exit complete, mark settled.
                existing.update({
                    "settled": True,
                    "exited_ts": record["ts"],
                    "exit_price": record["exit_price"],
                    "pnl": pnl,
                    "_exit_reason": reason,
                })
    _save_positions()
    if filled < true_count:
        log(f"  EXIT PARTIAL {ticker} ({reason}): {filled}/{true_count}x @ "
            f"{sell_price_c}c | partial pnl ${pnl:+.2f} | "
            f"{true_count - filled} contracts still open")
        discord_send(
            f"🟡 **PARTIAL EXIT** `{ticker}` {pos.get('action')} "
            f"{filled}/{true_count}x @ {sell_price_c}c ({reason}) — partial P&L "
            f"**${pnl:+.2f}**, {true_count - filled} still open"
        )
    else:
        log(f"  EXIT FILLED {ticker} ({reason}): {filled}x @ {sell_price_c}c | "
            f"pnl ${pnl:+.2f}")
        discord_send(
            f"🟡 **EXIT** `{ticker}` {pos.get('action')} {filled}x @ "
            f"{sell_price_c}c ({reason}) — P&L **${pnl:+.2f}**"
        )
    return True


def check_open_positions_for_exit(market_quotes: dict[str, dict]) -> int:
    """Mid-cycle exit check. For each open non-settled position, look up the
    current bid in market_quotes (caller passes a {ticker: market_dict} index
    from discover_markets). Triggers:

      1. d-1+ SKIP — climate day hasn't started for this station, no obs to
         verify adverse moves. Let σ-aware sizing be the only protection;
         hold to settlement. Tail markets at d-1 swing 30%+ on thin
         microstructure noise unrelated to actual probability changes.
         Trigger: AUS-26MAY01-T56 hard_stopped 11min after entry on bid
         drop, settled tomorrow on climate-day low (likely a winner).
      2. OBS_CONFIRMED_WINNER override → SKIP exit (hold guaranteed wins
         even if MTM looks awful — V2 lesson: thin-book price noise faked
         losses on confirmed winners).
      3. HARD STOP: MTM loss ≥ HARD_STOP_BRACKET_LOSS_PCT (B-bracket) or
         HARD_STOP_TAIL_LOSS_PCT (tail) → SELL at current bid.

    Returns count of exits executed."""
    n_exits = 0
    with _positions_lock:
        positions = dict(_open_positions)
    for ticker, pos in positions.items():
        if pos.get("settled"):
            continue
        mkt = market_quotes.get(ticker)
        if mkt is None:
            continue
        action = pos.get("action")
        entry_price = float(pos.get("entry_price", 0))
        if entry_price <= 0:
            continue
        # 2026-05-24 (Chris): TIME_EXIT_10AM — liquidate ALL open positions at
        # 10:00 station-local on their OWN climate day. Backtest 5/12-5/23
        # (n=48 open-at-10am): hold-to-settlement -15.0% ROI vs sell-at-10am-bid
        # +3.8% (+$223 over the window; better on 8/10 days). Min-bot positions
        # erode from 10am -> settlement, so cut them. Sells at current bid; if no
        # bid, falls through to existing logic (holds). The climate-day guard
        # prevents dumping d-1 positions (entered today for tomorrow) a day early.
        _tz10 = _STATION_TZ.get(pos.get("station"))
        if _tz10:
            _nl10 = datetime.now(ZoneInfo(_tz10))
            if pos.get("date_str") == _nl10.strftime("%Y-%m-%d") and _nl10.hour >= 10:
                # (A) keep confirmed winners — hold to settlement, don't 10am-sell.
                # Min-temp BUY_NO: running_min only falls, so once it's below the
                # bracket floor NO can't lose; selling at the ~99c bid just gives
                # up the last cent + adds execution risk. (2026-05-26)
                if ENABLE_PACED_EXIT:
                    _rm10 = pos.get("running_min")
                    if _rm10 is None:
                        _rm10 = get_running_min(pos.get("station"), pos.get("date_str"))
                    if _rm10 is not None:
                        try:
                            if _check_position_obs_winning(pos, float(_rm10)):
                                log(f"  TIME_EXIT_10AM HOLD-WINNER {ticker}: rm={_rm10} "
                                    f"confirms winner - holding to settlement (100c)")
                                continue
                        except Exception as _wx:
                            log(f"  TIME_EXIT_10AM winner-check raised "
                                f"{type(_wx).__name__}: {_wx}", "warn")
                _ss10 = "yes" if action == "BUY_YES" else ("no" if action == "BUY_NO" else None)
                _bid10 = mkt.get("yes_bid") if action == "BUY_YES" else mkt.get("no_bid")
                if _ss10 and _bid10 is not None and _bid10 > 0:
                    # (C) anti-chase floor — refuse to sell below ref_bid - TOL.
                    # Snapshot uncrushed arrival bid; skip cycles where current
                    # bid is below floor. Self-crushed bids recover (NOLA-5/28
                    # 89→33 walk after stable 89c). market_stop (≤1c) +
                    # shadow_obs_loser (≤25c+obs) still catch genuine declines.
                    # (2026-06-01)
                    if ENABLE_ANTICHASE_TIME_EXIT:
                        if "_exit_ref_bid_c" not in pos:
                            pos["_exit_ref_bid_c"] = int(_bid10)
                            _save_positions()
                        _floor = pos["_exit_ref_bid_c"] - ANTICHASE_EXIT_TOL_C
                        if int(_bid10) < _floor:
                            log(f"  TIME_EXIT_10AM ANTICHASE-SKIP {ticker}: "
                                f"bid={int(_bid10)}c < floor {_floor}c "
                                f"(ref {pos['_exit_ref_bid_c']}c) — hold this cycle")
                            continue
                    # (B) sell everything else PACED — a clip per cycle so we don't
                    # outrun the thin book and crush our own bid. (2026-05-26)
                    _clip = (min(int(pos.get("count", 0)), PACED_EXIT_CLIP_C)
                             if ENABLE_PACED_EXIT else None)
                    log(f"  TIME_EXIT_10AM {ticker}: local={_nl10:%H:%M} "
                        f"cd={pos.get('date_str')} sell {_ss10} @ {int(_bid10)}c"
                        + (f" (paced {_clip}/{pos.get('count')})" if _clip else ""))
                    if _execute_exit(ticker, pos, _ss10, int(_bid10),
                                     "time_exit_10am", max_count=_clip):
                        n_exits += 1
                    continue
        # No-obs skip: skip hard-stop entirely when no obs are available
        # for the position's climate day. Covers:
        #   - d-1+ trades (cd hasn't started — Austin entered today for
        #     tomorrow's market is the canonical case)
        #   - Early d-0 trades after cd starts but BEFORE first obs lands
        #     (the transition window where bid may still be depressed from
        #     overnight panic but no obs to verify direction)
        #   - obs-pipeline DB outages
        # Without obs, the hard-stop is firing on bid microstructure noise
        # uncorrelated with actual probability changes. Hold to settlement;
        # rely on σ-aware sizing as the only protection until obs available.
        # AUS-T56 was the trigger: hard_stopped 11min after entry on bid
        # drop, no obs to confirm any adverse signal.
        rm = pos.get("running_min")
        if rm is None:
            station = pos.get("station")
            date_str = pos.get("date_str")
            if station and date_str:
                rm = get_running_min(station, date_str)
        if rm is None:
            continue  # no obs → no hard-stop

        # BUY_YES T-high: skip hard-stop entirely. Per memory
        # `min_bot BUY_YES T-high obs_alive bypass removed 2026-04-28`, the
        # obs-winner override is INTENTIONALLY disabled for T-high BUY_YES
        # because rm-above-floor doesn't lock min-temp (evening cooling can
        # still drop the low below threshold). With override disabled,
        # hard-stop fires on bid noise even when rm is showing we're likely
        # winning — same disaster pattern that produced AUS-26MAY01-T56's
        # $26 loss on what was likely a settlement winner. Decision
        # 2026-04-30: hold T-high BUY_YES to settlement entirely. σ-aware
        # sizing shrinks the bet (~$5-10 typical), bounding max loss; the
        # asymmetric tail payoff makes settlement risk acceptable.
        floor_v = pos.get("floor")
        cap_v = pos.get("cap")
        if action == "BUY_YES" and floor_v is not None and cap_v is None:
            continue

        # Determine sell side and current bid.
        if action == "BUY_YES":
            sell_side = "yes"
            current_bid_c = mkt.get("yes_bid")
        elif action == "BUY_NO":
            sell_side = "no"
            current_bid_c = mkt.get("no_bid")
        else:
            continue
        if current_bid_c is None or current_bid_c <= 0:
            continue
        current_price = current_bid_c / 100.0
        loss_pct = (entry_price - current_price) / entry_price

        # 2026-05-20: TAKE_PROFIT_15 — lock in BUY_NO gains when MTM/cost
        # ≥ 15% AND local time ≥ 10:30 LST. Placed BEFORE obs_winner override
        # because late-evening-cool-flip patterns (SATX/HOU 5/19) have rm
        # clearly above cap at TP time but the bracket can still flip with
        # evening cooling — obs_winner would block the save. See
        # TAKE_PROFIT_15_* constant block for audit + rationale.
        try:
            if _check_take_profit_15(pos, current_bid_c):
                _bid = int(current_bid_c)
                _mtm_pct = ((_bid / 100.0 - entry_price) / entry_price) * 100
                _realize = (_bid / 100.0 - entry_price) * pos.get("count", 0)
                log(f"  TAKE_PROFIT_15 trigger {ticker}: bid={_bid}c "
                    f"entry={entry_price:.2f} mtm_pct={_mtm_pct:+.0f}% "
                    f"would-realize ${_realize:+.2f}")
                if _execute_exit(ticker, pos, sell_side, _bid, "take_profit_15"):
                    n_exits += 1
                continue
        except Exception as _tpx:
            log(f"  TAKE_PROFIT_15 check raised {type(_tpx).__name__}: {_tpx}", "warn")

        # OBS_CONFIRMED_WINNER override — never sell a guaranteed winner.
        if rm is not None:
            try:
                if _check_position_obs_winning(pos, float(rm)):
                    if loss_pct > 0.30:  # only log when override matters
                        log(f"  HOLD {ticker}: rm={rm} confirms winner; "
                            f"ignoring MTM loss {loss_pct:.0%}")
                    continue
            except Exception as _winx:
                # noqa: should_log — obs-winning override raised; without
                # logging, we'd silently drop into hard-stop logic and exit
                # a position the obs may already have confirmed as winner.
                log(f"  HOLD {ticker}: obs-winning check raised {type(_winx).__name__}: {_winx}; "
                    f"falling through to MTM stop", "warn")

        # 2026-05-13: NARROW_MARGIN_TP — take profit on at-risk-zone BUY_NO
        # B-bracket positions BEFORE late-day cooling can crash the bid.
        # Fires only on whitelist stations (KLAX/KMDW/KMIA/KSEA — those
        # with worst BUY_NO B-bracket WR over 14d). Placed AFTER winner
        # override (don't TP on confirmed winners) and BEFORE SHADOW
        # (NMTP catches the rm-just-above-cap zone, SHADOW catches rm-in-
        # bracket zone — non-overlapping rm ranges by construction).
        # See NARROW_MARGIN_TP_* constant block for audit + rationale.
        try:
            if _check_narrow_margin_tp(pos, rm, current_bid_c):
                log(f"  NMTP_EXIT trigger {ticker}: station={pos.get('station')} "
                    f"rm={rm} bracket=[{pos.get('floor')},{pos.get('cap')}] "
                    f"margin={(float(rm) - float(pos.get('cap'))):+.2f}°F "
                    f"bid={int(current_bid_c)}c entry={entry_price:.2f} "
                    f"mtm={(int(current_bid_c)/100.0 - entry_price)/entry_price*100:+.0f}% — execute "
                    f"(would-realize ${(int(current_bid_c)/100.0 - entry_price) * pos.get('count', 0):+.2f})")
                if _execute_exit(ticker, pos, sell_side, int(current_bid_c), "narrow_margin_tp"):
                    n_exits += 1
                continue
        except Exception as _nmtpx:
            log(f"  NMTP check raised {type(_nmtpx).__name__}: {_nmtpx}", "warn")

        # 2026-05-04 night SHADOW: OBS_CONFIRMED_LOSER detection.
        # 2026-05-13: Stage 2 activation, scoped to BUY_NO B-bracket only.
        # Fires actual exit when (a) obs confirm loser via
        # _check_position_obs_confirmed_loser_for_exit, (b) action is
        # BUY_NO with both floor and cap (the case the n=11 / 8d audit
        # covers), AND (c) bid <= SHADOW_EXIT_MAX_BID_C (market concurs).
        # All other action/bracket combos (BUY_YES, T-cap, T-floor) stay
        # Stage 1 log-only — no backtest evidence to ship Stage 2 there.
        # Above the bid gate we also fall back to log-only.
        if rm is not None:
            try:
                if _check_position_obs_confirmed_loser_for_exit(pos, float(rm)):
                    _is_buy_no_b = (
                        action == "BUY_NO"
                        and pos.get("floor") is not None
                        and pos.get("cap") is not None
                    )
                    if _is_buy_no_b and int(current_bid_c) <= SHADOW_EXIT_MAX_BID_C:
                        log(f"  SHADOW_EXIT trigger {ticker}: rm={rm} "
                            f"bracket=[{pos.get('floor')},{pos.get('cap')}] "
                            f"action={action} entry={entry_price:.2f} "
                            f"current_bid={int(current_bid_c)}c "
                            f"≤ {SHADOW_EXIT_MAX_BID_C}c — execute "
                            f"(would-recover ${(int(current_bid_c)/100.0 - entry_price) * pos.get('count', 0):+.2f} "
                            f"vs hold-to-loss ${-entry_price * pos.get('count', 0):+.2f})")
                        if _execute_exit(ticker, pos, sell_side, int(current_bid_c), "shadow_obs_loser"):
                            n_exits += 1
                        continue
                    else:
                        # Stage 1 log-only path: BUY_YES, T-* brackets, OR
                        # BUY_NO B-bracket with bid above the gate.
                        _gate_reason = ("not BUY_NO B-bracket"
                                        if not _is_buy_no_b
                                        else f"bid > {SHADOW_EXIT_MAX_BID_C}c")
                        log(f"  SHADOW OBS_CONFIRMED_LOSER {ticker}: "
                            f"rm={rm} bracket=[{pos.get('floor')},{pos.get('cap')}] "
                            f"action={action} entry={entry_price:.2f} "
                            f"current_bid={int(current_bid_c)}c "
                            f"would-recover ${(int(current_bid_c)/100.0 - entry_price) * pos.get('count', 0):+.2f} "
                            f"vs hold-to-loss ${-entry_price * pos.get('count', 0):+.2f} "
                            f"({_gate_reason} — log-only)", "warn")
            except Exception as _lex:
                # Conservative: any error in shadow logic is non-fatal.
                log(f"  SHADOW obs-loser check raised {type(_lex).__name__}: {_lex}", "warn")

        # 2026-05-04: very-loose value-dead market stop. Hard-stop (below) is
        # sentinel-disabled; this is a final safety net that fires only when
        # the position is essentially worthless. Bid ≤ 1c means market
        # consensus is ~99% the other side wins. The position can still
        # technically recover (especially min-temp BUY_NO where rm only
        # decreases), but the obs-winner override above already protects
        # genuine recovery cases — if obs still confirms a win at this
        # price, we hold; if not, we cut and free the capital.
        if int(current_bid_c) <= MARKET_STOP_BID_CEIL_C:
            log(f"  MARKET_STOP trigger {ticker}: bid={int(current_bid_c)}c "
                f"≤ {MARKET_STOP_BID_CEIL_C}c (value-dead, no obs-winner override)")
            if _execute_exit(ticker, pos, sell_side, int(current_bid_c), "market_stop"):
                n_exits += 1
            continue

        # Hard stop. Sentinel-disabled (HARD_STOP_*_LOSS_PCT = 999.0 since
        # 2026-05-04, see constant comment); the loss_pct >= 999 comparison
        # is impossible. Code retained for ease of re-enable.
        floor = pos.get("floor")
        cap = pos.get("cap")
        is_tail = (floor is None) != (cap is None)  # exactly one bound = tail
        loss_threshold = HARD_STOP_TAIL_LOSS_PCT if is_tail else HARD_STOP_BRACKET_LOSS_PCT
        if loss_pct >= loss_threshold:
            log(f"  HARD_STOP trigger {ticker}: entry {entry_price:.2f} → "
                f"mtm {current_price:.2f} (loss {loss_pct:.0%} ≥ {loss_threshold:.0%})")
            if _execute_exit(ticker, pos, sell_side, int(current_bid_c), "hard_stop"):
                n_exits += 1
    return n_exits


# ═══════════════════════════════════════════════════════════════════════
# SETTLEMENT
# ═══════════════════════════════════════════════════════════════════════

# Kalshi settlement fallback cache (5-min TTL). Populated lazily when we
# detect a stale open position with no obs-pipeline CLI. Closes the gap when
# obs-pipeline misses a CLI bulletin (8 stuck positions detected 2026-04-28
# audit: KBOS/KDFW Apr 27, plus 6 older — Kalshi had settled them all).
_kalshi_settlements_cache: dict[str, dict] = {}
_kalshi_settlements_cache_ts: float = 0.0
KALSHI_SETTLEMENTS_TTL_SEC = 300


def _refresh_kalshi_settlements_cache(force: bool = False) -> None:
    """Pull the most recent KXLOWT settlements from Kalshi and cache by ticker.
    5-min TTL, refreshed only when called by `check_settlements` for a stale
    position. Pulls the most-recent page (limit=200) — sufficient for any
    position the bot might still be holding given POSITION_TTL_DAYS=3."""
    global _kalshi_settlements_cache, _kalshi_settlements_cache_ts
    if not force and (time.time() - _kalshi_settlements_cache_ts) < KALSHI_SETTLEMENTS_TTL_SEC:
        return
    try:
        r = kalshi_get("/trade-api/v2/portfolio/settlements", {"limit": 200})
        new_cache: dict[str, dict] = {}
        for s in (r.get("settlements") or []):
            tk = s.get("ticker")
            if tk and tk.startswith("KXLOWT"):
                new_cache[tk] = s
        _kalshi_settlements_cache = new_cache
        _kalshi_settlements_cache_ts = time.time()
        log(f"  Kalshi settlements cache refreshed: {len(new_cache)} entries")
    except Exception as e:
        log(f"  Kalshi settlements cache refresh failed: {e}", "warn")


def _settle_from_kalshi(pos: dict) -> Optional[dict]:
    """Build a settlement record from a Kalshi `/portfolio/settlements` entry
    when obs-pipeline's CLI is missing. Returns the settlement dict or None
    if Kalshi hasn't settled either.

    `cli_low` is None on these records (we don't have the CLI value); a
    `source: kalshi` field marks them so calibration analysis can filter."""
    ticker = pos.get("market_ticker")
    if not ticker:
        return None
    ks = _kalshi_settlements_cache.get(ticker)
    if not ks:
        return None
    market_result = ks.get("market_result")
    if market_result not in ("yes", "no"):
        return None
    yes_wins = (market_result == "yes")
    action = pos.get("action")
    our_win = (yes_wins if action == "BUY_YES" else (not yes_wins))
    count = int(pos.get("count", 0))
    price = float(pos.get("entry_price", 0.0))
    cost = count * price
    revenue = count * 1.0 if our_win else 0.0
    pnl = revenue - cost
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "settlement",
        "market_ticker": ticker, "action": action,
        "entry_price": price, "count": count, "cost": cost,
        "cli_low": None, "floor": pos.get("floor"), "cap": pos.get("cap"),
        "in_bracket": yes_wins, "won": our_win,
        "revenue": revenue, "pnl": pnl,
        "model_prob": pos.get("model_prob"),
        "mu": pos.get("mu"), "sigma": pos.get("sigma"),
        "mu_source": pos.get("mu_source"),
        "running_min_at_entry": pos.get("running_min"),
        "station": pos.get("station"), "date_str": pos.get("date_str"),
        "label": pos.get("label"),
        # ─── Entry awareness (V2-parity, 2026-05-01) ───
        "entry_local_hour":           pos.get("entry_local_hour"),
        "entry_local_dow":            pos.get("entry_local_dow"),
        "entry_local_ts":             pos.get("entry_local_ts"),
        "entry_hours_to_sunrise":     pos.get("entry_hours_to_sunrise"),
        "entry_tz":                   pos.get("entry_tz"),
        "entry_yes_bid_cents":        pos.get("entry_yes_bid_cents"),
        "entry_yes_ask_cents":        pos.get("entry_yes_ask_cents"),
        "entry_no_bid_cents":         pos.get("entry_no_bid_cents"),
        "entry_no_ask_cents":         pos.get("entry_no_ask_cents"),
        "entry_volume":               pos.get("entry_volume"),
        "entry_spread_cents":         pos.get("entry_spread_cents"),
        "mu_nbp_at_entry":            pos.get("mu_nbp_at_entry"),
        "sigma_nbp_at_entry":         pos.get("sigma_nbp_at_entry"),
        "mu_nbm_om_at_entry":         pos.get("mu_nbm_om_at_entry"),
        "mu_hrrr_at_entry":           pos.get("mu_hrrr_at_entry"),
        "disagreement_at_entry":      pos.get("disagreement_at_entry"),
        "post_sunrise_lock_at_entry": pos.get("post_sunrise_lock_at_entry"),
        "is_today_at_entry":          pos.get("is_today_at_entry"),
        "yes_ask_frac_at_entry":      pos.get("yes_ask_frac_at_entry"),
        "no_ask_frac_at_entry":       pos.get("no_ask_frac_at_entry"),
        "edge":                       pos.get("edge"),
        "source": "kalshi",
        "kalshi_settled_time": ks.get("settled_time"),
    }


def check_settlements() -> int:
    """Walk open positions; for each, check if the settlement CLI low has
    been published. If so, compute P&L and archive.

    Two settlement paths:
      1. obs-pipeline CLI (preferred): `get_cli_low` returns the integer low
         from `cli_reports`. Bracket math computes in_bracket / won.
      2. Kalshi fallback: when obs-pipeline never ingested the CLI for this
         (station, climate-day) AND date_str < today, ask Kalshi if the market
         already settled (their `market_result` is authoritative regardless of
         our obs-pipeline coverage). Recovers stuck positions caused by gaps
         in NWS bulletin ingestion."""
    settled = 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    have_stale_unsettled = False
    with _positions_lock:
        positions = dict(_open_positions)
    for ticker, pos in positions.items():
        if pos.get("settled"):
            continue
        ds = pos.get("date_str") or ""
        if ds and ds < today:
            have_stale_unsettled = True
            break
    if have_stale_unsettled:
        _refresh_kalshi_settlements_cache()
    for ticker, pos in positions.items():
        # Skip positions already marked settled. dedupe-survives-settle (commit
        # bca506e) keeps them in _open_positions for re-entry blocking; without
        # this guard the loop re-processes them every cycle, double-writing
        # settlement records and spamming Discord.
        if pos.get("settled"):
            continue
        station = pos.get("station")
        date_str = pos.get("date_str")
        if not station or not date_str:
            continue
        cli_low = get_cli_low(station, date_str)
        # 2026-04-29 phantom-settlement fix: only treat the CLI as final when
        # issued AFTER climate_date_end_LST + CLI_FINAL_BUFFER_H. Otherwise it's
        # a partial intra-day reading (4 PM "VALID AS OF 0400 PM LOCAL TIME")
        # and the climate-day low can still drop overnight before midnight.
        # Falls through to the Kalshi /portfolio/settlements path which is
        # authoritative once the market actually finalizes.
        if cli_low is not None:
            tz_str = _STATION_TZ.get(station)
            if not _cli_is_final(station, date_str, tz_str):
                cli_low = None
        kalshi_settlement: Optional[dict] = None
        if cli_low is None:
            # Fallback: ask Kalshi if it already settled this market
            # (obs-pipeline missed the CLI bulletin OR the CLI we have is still
            # partial). Only fires when the climate day is past — fresh markets
            # still wait for the morning-after CLI.
            if date_str < today:
                kalshi_settlement = _settle_from_kalshi(pos)
            if kalshi_settlement is None:
                continue  # settlement not yet available from either source
        if kalshi_settlement is not None:
            settlement = kalshi_settlement
            in_bracket = settlement["in_bracket"]
            our_win = settlement["won"]
            pnl = settlement["pnl"]
            log_cli = "kalshi"
        else:
            # Determine outcome from CLI low
            floor = pos.get("floor")
            cap = pos.get("cap")
            # Defense-in-depth (2026-04-30): if both bounds are None we'd
            # silently default in_bracket=True, inverting BUY_NO/BUY_YES
            # settlement (Apr 27 phantom: CHI-T48 +$8.28, SEA-T42 +$3.32,
            # LV-T58 −$0.80). _reconcile_kalshi_positions already filters
            # such records at creation, but a stale positions.json from
            # before the reconcile fix could still hold one. Skip rather
            # than mis-settle.
            if floor is None and cap is None:
                log(f"  SKIP settle {ticker}: floor=cap=None — would invert "
                    f"settlement. Reconcile should have resolved tail bounds.", "warn")
                continue
            in_bracket = True
            if floor is not None and cli_low < floor:
                in_bracket = False
            if cap is not None and cli_low > cap:
                in_bracket = False
            action = pos.get("action")
            yes_wins = in_bracket
            our_win = (yes_wins if action == "BUY_YES" else (not yes_wins))
            count = int(pos.get("count", 0))
            price = float(pos.get("entry_price", 0.0))
            cost = count * price
            revenue = count * 1.0 if our_win else 0.0
            pnl = revenue - cost
            settlement = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "kind": "settlement",
                "market_ticker": ticker, "action": action,
                "entry_price": price, "count": count, "cost": cost,
                "cli_low": cli_low, "floor": floor, "cap": cap,
                "in_bracket": in_bracket, "won": our_win,
                "revenue": revenue, "pnl": pnl,
                "model_prob": pos.get("model_prob"),
                "mu": pos.get("mu"), "sigma": pos.get("sigma"),
                "mu_source": pos.get("mu_source"),
                "running_min_at_entry": pos.get("running_min"),
                "station": station, "date_str": date_str,
                "label": pos.get("label"),
                # ─── Entry awareness (V2-parity, 2026-05-01) ───
                "entry_local_hour":           pos.get("entry_local_hour"),
                "entry_local_dow":            pos.get("entry_local_dow"),
                "entry_local_ts":             pos.get("entry_local_ts"),
                "entry_hours_to_sunrise":     pos.get("entry_hours_to_sunrise"),
                "entry_tz":                   pos.get("entry_tz"),
                "entry_yes_bid_cents":        pos.get("entry_yes_bid_cents"),
                "entry_yes_ask_cents":        pos.get("entry_yes_ask_cents"),
                "entry_no_bid_cents":         pos.get("entry_no_bid_cents"),
                "entry_no_ask_cents":         pos.get("entry_no_ask_cents"),
                "entry_volume":               pos.get("entry_volume"),
                "entry_spread_cents":         pos.get("entry_spread_cents"),
                "mu_nbp_at_entry":            pos.get("mu_nbp_at_entry"),
                "sigma_nbp_at_entry":         pos.get("sigma_nbp_at_entry"),
                "mu_nbm_om_at_entry":         pos.get("mu_nbm_om_at_entry"),
                "mu_hrrr_at_entry":           pos.get("mu_hrrr_at_entry"),
                "disagreement_at_entry":      pos.get("disagreement_at_entry"),
                "post_sunrise_lock_at_entry": pos.get("post_sunrise_lock_at_entry"),
                "is_today_at_entry":          pos.get("is_today_at_entry"),
                "yes_ask_frac_at_entry":      pos.get("yes_ask_frac_at_entry"),
                "no_ask_frac_at_entry":       pos.get("no_ask_frac_at_entry"),
                "edge":                       pos.get("edge"),
                "source": "obs_pipeline",
            }
            log_cli = f"{cli_low}°F"
        _append_jsonl(SETTLEMENTS_FILE, settlement)
        # 2026-05-17 — record settled ticker so reconcile-on-restart skips
        # re-adding it. See _settled_tickers comment.
        _settled_tickers.add(ticker)
        # Don't pop — keep the record in _open_positions tagged as settled so
        # the per-ticker dedupe (in scan_cycle and execute_opportunity) still
        # blocks re-entry. Without this, the cascade bug (~50 V2-wallet orders
        # on 04:09–04:26 UTC, $21 lost) recurs whenever the local CLI lands
        # before Kalshi closes the market. POSITION_TTL_DAYS prunes on next
        # restart so this can't grow unbounded.
        with _positions_lock:
            existing = _open_positions.get(ticker)
            if existing is not None:
                existing.update({
                    "settled": True,
                    "settled_ts": settlement["ts"],
                    "cli_low": settlement.get("cli_low"),
                    "in_bracket": in_bracket,
                    "won": our_win,
                    "pnl": pnl,
                })
        settled += 1
        log(f"  SETTLED {ticker} | action={pos.get('action')} CLI_low={log_cli} "
            f"in={in_bracket} won={our_win} pnl=${pnl:+.2f}")
        notify_discord_settlement(ticker, pos.get("action"),
                                  settlement.get("cli_low") or 0,
                                  in_bracket, our_win, pnl)
    if settled:
        _save_positions()
    return settled


# ═══════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════

_shutdown = threading.Event()


def _sig_handler(sig, frame):
    log(f"received signal {sig}, shutting down")
    _shutdown.set()


def scan_cycle() -> dict:
    """One full scan: settlements, forecasts refresh (throttled),
    market discovery, opp find, log candidates, execute taken ones."""
    _reset_cycle_budget()  # zero per-cycle counter; rolls daily exposure at UTC midnight
    stats = {"settled": 0, "markets": 0, "candidates": 0, "opps": 0, "taken": 0}
    stats["settled"] = check_settlements()

    # New-low Discord alerts — poll running_min for all 20 stations and
    # emit on downward steps. Mirrors V1/V2 max-bot's "NEW HIGH" alerts.
    try:
        _check_new_low_alerts()
    except Exception as e:
        log(f"  new-low alert check failed: {e}", "warn")

    # 6-hour rolling summary of running_min across all 20 stations.
    _maybe_send_low_summary()

    # NBP refresh: HEAD-poll trigger (2026-04-30). Probes next-expected cycle
    # URL on S3 once per scan past cycle+70min; full GET only when HEAD=200.
    # Detects new NBP cycles within one scan interval (15–60s) of S3 publish
    # vs ~3.5h gap under the prior age>6h trigger.
    if _nbp_next_cycle_available():
        try:
            refresh_nbp_forecasts()
        except Exception as e:
            log(f"  NBP refresh failed: {e}", "warn")

    # NBM-OM refresh — dynamic TTL (30s general / 5s during HH:35-50 pub window).
    # 2026-05-03: aligned with V2's pattern (running since 2026-04-29). Catches
    # new NBM cycles within ~5s of Open-Meteo publishing them.
    try:
        need_refresh = False
        _nbm_om_ttl_now = _nbm_om_dynamic_ttl()
        with _nbm_om_cache_lock:
            for series in CITIES:
                entries = _nbm_om_cache.get(series, {})
                if not entries:
                    need_refresh = True
                    break
                newest = max((e.get("fetched", 0) for e in entries.values()), default=0)
                if time.time() - newest > _nbm_om_ttl_now:
                    need_refresh = True
                    break
        if need_refresh:
            refresh_nbm_om_forecasts()
    except Exception as e:
        log(f"  NBM-OM refresh failed: {e}", "warn")

    # HRRR refresh — dynamic TTL (60s general / 5s during HH:43-55 pub window).
    # Inside the pub window the loop will trigger a fresh fetch ~5s after the
    # previous one, catching new NCEP runs as soon as Open-Meteo has them.
    try:
        need_refresh = False
        _hrrr_ttl_now = _hrrr_dynamic_ttl()
        with _hrrr_cache_lock:
            for series in CITIES:
                entries = _hrrr_cache.get(series, {})
                if not entries:
                    need_refresh = True
                    break
                newest = max((e.get("fetched", 0) for e in entries.values()), default=0)
                if time.time() - newest > _hrrr_ttl_now:
                    need_refresh = True
                    break
        if need_refresh:
            refresh_hrrr_forecasts()
    except Exception as e:
        log(f"  HRRR refresh failed: {e}", "warn")

    try:
        markets = discover_markets()
    except Exception as e:
        log(f"  market discovery failed: {e}", "warn")
        return stats
    stats["markets"] = len(markets)
    # Mid-cycle exit check on existing open positions (hard stop + obs-winner
    # override). Runs BEFORE find_opportunities so freed-up budget can fund
    # new entries the same cycle.
    mkt_by_ticker = {m["market_ticker"]: m for m in markets if m.get("market_ticker")}
    # 2026-05-02 RICH POSITION TELEMETRY: capture per-cycle live state for
    # every open position before exit logic runs (so the exit decision and
    # the telemetry both see the same market+obs+forecast snapshot).
    # Pure additive — does not change any decision.
    try:
        _update_open_positions_telemetry(mkt_by_ticker)
    except Exception as e:
        log(f"  telemetry update failed: {type(e).__name__}: {e}", "warn")
    try:
        stats["exited"] = check_open_positions_for_exit(mkt_by_ticker)
    except Exception as e:
        log(f"  exit check failed: {e}", "warn")
        stats["exited"] = 0
    opps = find_opportunities(markets)
    stats["candidates"] = len(opps)
    for opp in opps:
        record_candidate(opp)
    # Execute the takeable ones. Note: edge floor is dynamic (OBS_ALIVE_MIN_EDGE
    # for obs-confirmed candidates, MIN_EDGE otherwise), so include any with
    # edge ≥ OBS_ALIVE_MIN_EDGE; execute_opportunity itself decides.
    taken = [o for o in opps if o.get("edge", 0) >= OBS_ALIVE_MIN_EDGE]
    stats["opps"] = sum(1 for o in opps if o.get("edge", 0) > 0)
    # 2026-05-01: outer dedup removed. The pre-fix `if ticker in _open_positions:
    # continue` blocked every ticker with a partial-fill record from reaching
    # execute_opportunity, which is where add-on eligibility is decided. With
    # the V2-style add-on path (commit 9ef1201), execute_opportunity owns the
    # full eligibility check (settled / exited / partial-exited / at-intended /
    # at-MAX_BET_USD); the outer dedup was the V1-era assumption that one
    # ticker = one entry attempt. Real-money result of leaving it: 0 add-ons
    # fired in 6h across 13 partial-fill positions — capital under-deployed.
    for opp in taken:
        if execute_opportunity(opp):
            stats["taken"] += 1
    return stats


def main() -> None:
    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)
    log("=" * 60)
    log(f"min_bot starting (WALLET={WALLET})")
    log(f"  cities: {len(CITIES)}")
    log(f"  obs DB: {OBS_DB_PATH}")
    log(f"  data dir: {DATA_DIR}")
    log(f"  caps: max_bet=${MAX_BET_USD:.2f} per_cycle={MAX_NEW_POSITIONS_PER_CYCLE} "
        f"daily_exposure=${DAILY_EXPOSURE_CAP_USD:.2f} min_edge={MIN_EDGE:.0%}")
    log("=" * 60)
    _load_kalshi_auth()
    _start_discord_worker()
    # Restore today's daily exposure from disk so DAILY_EXPOSURE_CAP_USD
    # survives bot restarts. Resets only when UTC date rolls over.
    global _today_date_utc, _today_exposure_usd
    _today_date_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _today_exposure_usd = _compute_today_exposure()
    log(f"  today's exposure (from trades.jsonl): ${_today_exposure_usd:.2f}/${DAILY_EXPOSURE_CAP_USD:.2f}")
    bal = get_kalshi_balance()
    if bal is None:
        log("  WARNING: balance fetch failed at startup — proceeding anyway", "warn")
    else:
        log(f"  Kalshi balance: ${bal:.2f}")
        if bal < BANKROLL_FLOOR_USD:
            raise RuntimeError(f"Balance ${bal:.2f} < floor ${BANKROLL_FLOOR_USD:.2f}; aborting")
    _load_positions()
    # Self-heal: pull live Kalshi positions and add any ghosts we lost track of.
    # Defends against a deploy cascade or crash that left holdings on Kalshi
    # without a corresponding record in positions.json.
    try:
        _reconcile_kalshi_positions()
    except Exception as e:
        log(f"  reconcile failed: {e}", "warn")
    # Belt-and-suspenders: Kalshi /portfolio/positions can lag fresh fills by
    # minutes. If positions.json was clobbered in the same window, that lag
    # let CHI-26APR25-T48 get bought twice on 04-25 (16 min apart, between
    # back-to-back deploy restarts). Trades.jsonl is our own append-only
    # log; reconciling from it closes the lag-window gap.
    try:
        _reconcile_from_trades_log()
    except Exception as e:
        log(f"  trade-log reconcile failed: {e}", "warn")
    _load_nbp_cache_from_disk()
    # Initial forecast fetches before first scan
    try:
        refresh_nbp_forecasts()
    except Exception as e:
        log(f"  initial NBP fetch failed: {e}", "warn")
    # Background NBP poller — HEAD-polls S3 every 5s during the publish
    # window so a new cycle lands in cache within ~5s of S3 publish instead
    # of waiting up to a full scan interval.
    _start_nbp_poller()
    try:
        refresh_nbm_om_forecasts()
    except Exception as e:
        log(f"  initial NBM-OM fetch failed: {e}", "warn")
    try:
        refresh_hrrr_forecasts()
    except Exception as e:
        log(f"  initial HRRR fetch failed: {e}", "warn")

    last_heartbeat = 0.0
    while not _shutdown.is_set():
        t0 = time.monotonic()
        try:
            stats = scan_cycle()
        except Exception as e:
            log(f"  scan_cycle error: {e}", "error")
            import traceback
            log(traceback.format_exc(), "error")
            stats = {}
        elapsed = time.monotonic() - t0
        log(f"cycle done: markets={stats.get('markets',0)} "
            f"cands={stats.get('candidates',0)} opps={stats.get('opps',0)} "
            f"taken={stats.get('taken',0)} settled={stats.get('settled',0)} "
            f"({elapsed:.1f}s)")

        if time.time() - last_heartbeat > LOG_HEARTBEAT_SEC:
            with _positions_lock:
                n_open = len(_open_positions)
            log(f"HEARTBEAT open_positions={n_open}")
            last_heartbeat = time.time()

        # Faster scan during pre-dawn window (any city could be near its low)
        # Simple: check if any of our cities is between 4-8 AM local.
        fast = False
        for series, meta in CITIES.items():
            h = datetime.now(ZoneInfo(meta["tz"])).hour
            if 4 <= h < 8:
                fast = True
                break
        interval = FAST_SCAN_INTERVAL_SEC if fast else SCAN_INTERVAL_SEC
        # Sleep but wake on shutdown
        _shutdown.wait(interval)

    log("min_bot exited cleanly")


if __name__ == "__main__":
    main()
