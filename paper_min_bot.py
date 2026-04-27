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
MAX_EDGE = 0.40                     # mirror V1 trust zone: edges above this almost always come
                                    # from model error (forecast vs market disagree wildly)
MIN_MODEL_PROB = 0.15               # skip model_prob < 15% (too unlikely to bet)
MAX_MODEL_PROB = 0.85               # skip model_prob > 85% (crowded / low payout)
MIN_ORDER_PRICE = 0.05              # don't bet contracts priced < 5¢
MAX_MODEL_PROB_MINUS_MARKET_FLOOR = 0.30  # sanity check on edge magnitude

# Hard safety gates (HIGH-impact: prevent the patterns that lost money on the
# 04:09 UTC live cycle and that V1/V2 had to fix in production).
MAX_DISAGREEMENT_F = 5.0            # skip if HRRR vs NBP / NBP vs NBM disagree > this
MAX_SPREAD_CENTS = 10               # skip if (yes_ask − yes_bid) > 10c on the active side
MAX_MU_VS_RM_DIFF_F = 5.0           # pre-sunrise sanity: skip if forecast μ disagrees with
                                    # observed running_min by more than this — model is wrong
MAX_OPEN_PER_EVENT = 1              # at most this many *open* positions per event_ticker.
                                    # Lifetime cap (counts against _open_positions, not just
                                    # this cycle). Prior per-cycle version let CHI-26APR25
                                    # accumulate 4 brackets across cycles 2026-04-25.
                                    # Correlated bets — if forecast is wrong, all lose.

# Kelly sizing
MAX_BET_USD = 5.00                  # $5 cap per entry ($1 → $3 (2026-04-26) → $5 (2026-04-27 PM
                                    # at Chris's request — open book of 18 sub-$1 positions made
                                    # the existing $3 ceiling feel small relative to the $1 floor).
KELLY_FRACTION = 0.25
MIN_BET_USD = 0.50
MIN_COST_USD = 1.00                 # cost floor: ceil(MIN_COST_USD / price) bumps `count` so
                                    # every fill deploys ≥ $1. Prior int-rounded Kelly produced
                                    # 96% sub-$1 fills on 2026-04-25/26 (avg $0.45). Capped by
                                    # MAX_BET_USD downstream so the floor can't blow the ceiling.

MIN_ABS_DISTANCE_F = 0.5            # BUY_NO only: skip if |mu − bracket_mid| < this many °F.
                                    # 1.0 → 1.5 (2026-04-27 AM) → reverted to 0.5 (2026-04-27 PM)
                                    # after Kalshi-truth audit on n=15: at 1.5°F we'd block 9
                                    # winners with `dist 0.5–1.5°F` (PHX-B65.5, LAX-B56.5,
                                    # SFO-B51.5, MIA-B69.5, CHI-B47.5 — all BUY_NO with mu *at the
                                    # bracket edge*, not inside). PHIL-B44.5 (0.1°F, mu *inside*
                                    # bracket, the only real loser) is still caught at 0.5°F.

# ─── Hard ceilings that gate execute_opportunity before placing the order
MAX_NEW_POSITIONS_PER_CYCLE = 3     # cycle scope (60s scan)
DAILY_EXPOSURE_CAP_USD = 60.00      # day scope (UTC midnight); $4 → $15 → $30 → $60 (2026-04-27,
                                    # paired with MIN_COST_USD floor — without the bump we'd hit
                                    # the cap in ~30 entries vs the recent 30–50/day rhythm)

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
# Four sub-checks on BUY_NO entries. Bypassed when _obs_confirmed_alive.
# V2 backtest: tightening these bands swung era P&L +$30 → +$74.
F2A_PROB_LO = 0.05                  # mp < this is a price-asymmetry trap (97c contracts, low WR)
F2A_PROB_HI = 0.30                  # mp ≥ this is calibration cliff (model says YES too likely)
F2A_SIGMA_MIN = 1.5                 # sigma < this is over-confident model (tight-σ zones lost in V2)
# F2A_DIST_MIN: V2 uses 0.5°F from NBM. NOT ported — min-bot audit (n=15) found
# `mu at bracket edge` is the BUY_NO winner pattern (cli flips OUTSIDE the bracket
# from there 60-100% of the time). MIN_ABS_DISTANCE_F (mu vs bracket MID, 0.5°F)
# already catches the dangerous "mu near bracket center / strictly inside" cases.

# ─── MSG multi-source consensus (V2 port, BUY_NO only) ────────────────────
# Count how many of {NBP, HRRR, NBM} forecasts predict YES wins. Block if
# consensus is too strong against us. Per-city tiers: WORST cities require
# unanimity (no source predicting YES). Bypassed when _obs_confirmed_alive.
MSG_MAX_CONSENSUS_DEFAULT = 2       # block if > this many sources predict YES
MSG_MAX_CONSENSUS_WORST = 0         # WORST cities: any source predicting YES blocks
MSG_WORST_CITIES = {                # cities with historical poor MIN calibration (mirror V2's WORST_7)
    "KXLOWTNYC", "KXLOWTSEA", "KXLOWTPHIL",
    "KXLOWTLV", "KXLOWTNOLA", "KXLOWTDEN",
}
MSG_MARGIN_F = 3.0                  # outlier source > this many °F into YES territory blocks

# ─── Hard stop on existing positions (V2 port, mid-cycle exit) ────────────
HARD_STOP_BRACKET_LOSS_PCT = 0.80   # exit if MTM loss ≥ 80% on B-brackets
HARD_STOP_TAIL_LOSS_PCT = 0.70      # exit if MTM loss ≥ 70% on tails (lottery payoff)
ORDER_FILL_TIMEOUT_SEC = 5.0        # wait this long for fill, then cancel
BANKROLL_FLOOR_USD = 5.00           # refuse new orders if portfolio cash < this

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
    pass

POSITIONS_FILE = DATA_DIR / "positions.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"            # every candidate + decision
SETTLEMENTS_FILE = DATA_DIR / "settlements.jsonl"
STATS_FILE = DATA_DIR / "stats.json"
NBP_CACHE_FILE = DATA_DIR / "nbp_cache.json"
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
            pass


def _atomic_write_json(path: Path, data: Any) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, default=str)
    os.replace(tmp, path)


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
        except Exception:
            pass
        finally:
            try:
                _discord_queue.task_done()
            except Exception:
                pass


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
    except Exception:
        pass
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
    Ports V2's 409-trading_paused + 400-insufficient_balance cooldown handling
    (V2 H-2 fix 2026-04-16; without it V1 logged 11k retries in one day)."""
    if _in_paused_cooldown(ticker):
        return None
    if _in_insufficient_balance_cooldown():
        return None
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
        emsg = str(e).lower()
        if "409" in str(e) or "trading is paused" in emsg or "trading_is_paused" in emsg:
            _set_paused_cooldown(ticker)
        elif "insufficient_balance" in emsg or "insufficient balance" in emsg:
            _set_insufficient_balance_cooldown()
        log(f"  ORDER FAILED {ticker} {side} {count}@{price_cents}c: {e}", "error")
        return None


def place_kalshi_sell_order(ticker: str, side: str, count: int,
                             price_cents: int) -> Optional[str]:
    """Place a limit SELL at price_cents. Used by hard-stop exits.
    `side` is the side we currently HOLD (e.g. 'no' if we hold BUY_NO).
    Returns order_id or None on failure."""
    if _in_paused_cooldown(ticker):
        return None
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
        emsg = str(e).lower()
        if "409" in str(e) or "trading is paused" in emsg or "trading_is_paused" in emsg:
            _set_paused_cooldown(ticker)
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
                pass
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

NBP_CYCLES = ["00", "06", "12", "18"]  # bulletins available every 6h
NBP_MAX_AGE_SEC = 8 * 3600             # 8h — one cycle past

def _load_nbp_cache_from_disk() -> None:
    global _nbp_cache, _nbp_cache_ts
    try:
        if NBP_CACHE_FILE.exists():
            with open(NBP_CACHE_FILE) as f:
                data = json.load(f)
            with _nbp_cache_lock:
                _nbp_cache = data.get("cache", {})
                _nbp_cache_ts = float(data.get("ts", 0.0))
            log(f"  NBP cache loaded: {sum(len(v) for v in _nbp_cache.values())} entries")
    except Exception as e:
        log(f"  NBP cache load failed (non-critical): {e}", "warn")


def _save_nbp_cache_to_disk() -> None:
    try:
        with _nbp_cache_lock:
            snap = {"cache": dict(_nbp_cache), "ts": _nbp_cache_ts}
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


def _nbp_fetch_latest_bulletin() -> Optional[tuple[str, int, str]]:
    """Fetch the most recent NBP bulletin text from AWS S3.
    Returns (text, cycle_hour, bulletin_date_YYYYMMDD) or None.

    Uses blend_nbptx (longer-range bulletin with TXNMN/TXNSD). NBM Probabilistic
    cycles run 01/07/13/19 UTC; 06z/18z are short-range and omit TXNMN.
    """
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y%m%d")
    yesterday = (now - timedelta(days=1)).strftime("%Y%m%d")
    cycle_order = [
        (today, "19"), (today, "13"), (today, "07"), (today, "01"),
        (yesterday, "19"), (yesterday, "13"), (yesterday, "07"),
    ]
    for d, h in cycle_order:
        url = ("https://noaa-nbm-grib2-pds.s3.amazonaws.com/"
               f"blend.{d}/{h}/text/blend_nbptx.t{h}z")
        try:
            r = httpx.get(url, timeout=30.0)
            if r.status_code != 200:
                continue
            data = r.text
            # Sanity check: TXNMN present (present on 01/07/13/19 cycles).
            if "TXNMN" not in data[:500000]:
                continue
            if len(data) < 1000000:
                continue
            log(f"  NBP: fetched blend.{d}/{h} ({len(data)//1024}KB)")
            return data, int(h), d
        except Exception:
            continue
    return None


def refresh_nbp_forecasts() -> None:
    """Fetch NBP bulletin + parse + update cache. Idempotent; call periodically."""
    global _nbp_cache, _nbp_cache_ts
    fetched = _nbp_fetch_latest_bulletin()
    if not fetched:
        log("  NBP: fetch failed — keeping stale cache", "warn")
        return
    text, cycle_hour, bulletin_date = fetched
    parsed = _nbp_parse_bulletin(text, cycle_hour, bulletin_date)
    with _nbp_cache_lock:
        for st, dates in parsed.items():
            _nbp_cache.setdefault(st, {}).update(dates)
        _nbp_cache_ts = time.time()
    _save_nbp_cache_to_disk()
    log(f"  NBP: parsed {len(parsed)} stations")


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
NBM_OM_TTL_SEC = 3600      # refresh hourly

_hrrr_cache: dict[str, dict] = {}
_hrrr_cache_lock = threading.Lock()
HRRR_TTL_SEC = 600         # refresh every 10 min; HRRR updates hourly


def _fetch_open_meteo_daily_min(model: str, cache: dict, cache_lock: threading.Lock,
                                  label: str) -> None:
    """Generic Open-Meteo daily min fetcher. Shared by NBM + HRRR."""
    for series, meta in CITIES.items():
        try:
            r = httpx.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": meta["lat"], "longitude": meta["lon"],
                    "models": model,
                    "daily": "temperature_2m_min",
                    "temperature_unit": "fahrenheit",
                    "timezone": meta["tz"],
                    "forecast_days": 3,
                },
                timeout=10.0,
            )
            r.raise_for_status()
            data = r.json()
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            mins = daily.get("temperature_2m_min", [])
            by_date = {}
            for d, m in zip(dates, mins):
                if m is not None:
                    by_date[d] = {"min_f": float(m), "fetched": time.time()}
            with cache_lock:
                cache[series] = by_date
        except Exception as e:
            log(f"  {label} fetch {series}: {e}", "warn")


def refresh_nbm_om_forecasts() -> None:
    """Fetch NBM daily min via Open-Meteo for all 20 cities."""
    _fetch_open_meteo_daily_min("best_match", _nbm_om_cache, _nbm_om_cache_lock, "NBM-OM")


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
    (customer-api.open-meteo.com) using `models=hrrr_conus`. Computes daily
    min from hourly trajectory. Free endpoint does NOT support HRRR.

    HRRR is high-res (3km), updates hourly, ~48h horizon — best available
    nowcast for upcoming overnight lows.
    """
    global _HRRR_DISABLED
    if _HRRR_DISABLED:
        return
    _load_open_meteo_key()
    if not _OPEN_METEO_API_KEY:
        _HRRR_DISABLED = True
        log("  HRRR disabled: OPEN_METEO_API_KEY not in .env", "warn")
        return

    paid_url = "https://customer-api.open-meteo.com/v1/forecast"
    fetched_count = 0
    for series, meta in CITIES.items():
        try:
            r = httpx.get(
                paid_url,
                params={
                    "latitude": meta["lat"], "longitude": meta["lon"],
                    "models": "ncep_hrrr_conus", "hourly": "temperature_2m",
                    "temperature_unit": "fahrenheit",
                    "timezone": meta["tz"],
                    "forecast_days": 3,
                    "apikey": _OPEN_METEO_API_KEY,
                },
                timeout=10.0,
            )
            if r.status_code == 401 or r.status_code == 403:
                _HRRR_DISABLED = True
                log(f"  HRRR disabled: paid endpoint auth failed ({r.status_code})", "warn")
                return
            r.raise_for_status()
            data = r.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])
            temps = hourly.get("temperature_2m", [])
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
                        d: {"min_f": v, "fetched": time.time()}
                        for d, v in by_date.items()
                    }
                fetched_count += 1
        except Exception as e:
            log(f"  HRRR fetch {series}: {e}", "warn")
    if fetched_count:
        log(f"  HRRR: fetched {fetched_count}/{len(CITIES)} cities (paid Open-Meteo)")


def get_nbm_om_min(series: str, date_str: str) -> Optional[float]:
    with _nbm_om_cache_lock:
        entry = _nbm_om_cache.get(series, {}).get(date_str)
    if not entry:
        return None
    if time.time() - entry.get("fetched", 0) > NBM_OM_TTL_SEC * 2:
        return None  # stale
    return float(entry["min_f"])


def get_hrrr_min(series: str, date_str: str) -> Optional[float]:
    with _hrrr_cache_lock:
        entry = _hrrr_cache.get(series, {}).get(date_str)
    if not entry:
        return None
    if time.time() - entry.get("fetched", 0) > HRRR_TTL_SEC * 3:
        return None
    return float(entry["min_f"])


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

    # Post-sunrise: collapse forecast to the observed running_min with tight
    # residual noise. Skip the truncation conditioning below — the Gaussian
    # centered on rm already captures the remaining uncertainty (ASOS vs CLI
    # sampling / rounding differences). Sigma 1.0°F (was 0.5) gives us a
    # ±1°F obs-vs-CLI window — a 5-min ASOS reading and the CLI 2-min low can
    # disagree by that much, even post-sunrise.
    if post_sunrise_lock and running_min is not None:
        mu = running_min
        sigma = 1.0
        lo_z = ((floor - mu) / sigma) if floor is not None else None
        hi_z = ((cap - mu) / sigma) if cap is not None else None
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
        if floor is not None and floor > rm_buffered:
            return 0.0  # bracket entirely above rm even after +1°F buffer — impossible
        effective_cap = cap if (cap is None or rm_buffered >= cap) else rm_buffered
        norm_z = (rm_buffered - mu) / sigma
        norm_denom = _gauss_cdf(norm_z)
        if norm_denom <= 0:
            return 0.0
        lo_z = ((floor - mu) / sigma) if floor is not None else None
        hi_z = ((effective_cap - mu) / sigma) if effective_cap is not None else norm_z
        p_lo = _gauss_cdf(lo_z) if lo_z is not None else 0.0
        p_hi = _gauss_cdf(hi_z)
        prob = (p_hi - p_lo) / norm_denom
        return max(0.0, min(1.0, prob))

    # No running_min constraint — unconditional Gaussian.
    lo_z = ((floor - mu) / sigma) if floor is not None else None
    hi_z = ((cap - mu) / sigma) if cap is not None else None
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
            pass
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
            pass
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
    """Return Kalshi bankroll, refreshed every BANKROLL_REFRESH_SEC. On fetch
    failure, falls back to the cached value (or MAX_BET_USD on cold start —
    preserves pre-fix sizing as a safety net)."""
    global _cached_bankroll, _bankroll_cache_ts
    now = time.time()
    if now - _bankroll_cache_ts > BANKROLL_REFRESH_SEC:
        b = get_kalshi_balance()
        if b is not None:
            _cached_bankroll = b
            _bankroll_cache_ts = now
    return _cached_bankroll if _cached_bankroll > 0 else MAX_BET_USD


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
      BUY_YES + T-high (floor=X−0.5, YES if cli ≥ X): rm ≥ floor + 1.0 AND post-sunrise
        → low has bottomed in YES territory → YES wins
      BUY_YES + B-bracket: defer (would need post-sunrise + rm in bracket;
        rare and complex; not a typical sweet-spot anyway)"""
    rm = opp_or_pos.get("running_min")
    if rm is None:
        return False
    floor = opp_or_pos.get("floor")
    cap = opp_or_pos.get("cap")
    action = opp_or_pos.get("action")
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
        # T-low: low has confirmed dip into YES territory
        if cap is not None and floor is None:
            if rm_f <= float(cap) - 1.0:
                return True
        # T-high: low rose to YES territory AND post-sunrise so it won't drop
        elif floor is not None and cap is None:
            tz = opp_or_pos.get("tz", "America/New_York")
            if rm_f >= float(floor) + 1.0 and _is_post_sunrise(tz):
                return True
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
      BUY_YES + B-bracket: rm < floor → low went below bracket; daily low
        ≤ rm < floor → YES (low in bracket) lost
      BUY_YES + T-high: rm < floor → low went below threshold; YES lost
      BUY_YES + T-low: rm > cap AND post-sunrise → low never reached
        threshold and won't drop further; YES (low ≤ X) lost"""
    rm = opp_or_pos.get("running_min")
    if rm is None:
        return False
    floor = opp_or_pos.get("floor")
    cap = opp_or_pos.get("cap")
    action = opp_or_pos.get("action")
    rm_f = float(rm)

    if action == "BUY_NO":
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
    triggers a separate block. Bypassed by caller when _obs_confirmed_alive."""
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
        #     But HRRR has no sigma, so pair it with NBP's sigma if we have it,
        #     otherwise use a conservative default.
        #   - day-1+ (future): NBP first (has sigma), else NBM-OM.
        if is_today and hrrr is not None:
            mu = hrrr
            sigma = nbp["sigma"] if nbp else 2.5
            mu_source = "hrrr"
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

# ─── Per-cycle / per-day budget tracking ────────────────────────────────
_cycle_budget_lock = threading.Lock()
_cycle_new_count = 0                          # reset each cycle
_today_exposure_usd = 0.0                     # cost of today's entries
_today_date_utc: str = ""                     # tracks UTC midnight rollover

# Per-ticker cooldowns ported from V2 (H-2 fix 2026-04-16): if Kalshi 409s a
# market with "trading_is_paused", or 400s the account with insufficient_balance,
# stop hammering it for a window. Without this V1 had an 11k retry storm.
_cooldown_lock = threading.Lock()
_paused_tickers: dict[str, float] = {}        # ticker → unix-time when cooldown ends
_insufficient_balance_until: float = 0.0      # account-wide; 0 = no cooldown
PAUSED_COOLDOWN_SEC = 60.0
INSUFFICIENT_BALANCE_COOLDOWN_SEC = 300.0     # 5 min, since the bankroll is small


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
    """Count currently-open (non-settled) positions on a given event."""
    if not event_ticker:
        return 0
    with _positions_lock:
        return sum(
            1 for tk, pos in _open_positions.items()
            if not pos.get("settled") and tk.rsplit("-", 1)[0] == event_ticker
        )


def _budget_can_take(cost_usd: float, event_ticker: str = "") -> tuple[bool, str]:
    """Check live-mode caps. Returns (ok, reason_if_blocked).

    Per-event cap counts against `_open_positions` (lifetime, not per-cycle),
    so once any bracket on an event is open we won't add another bracket on
    the same event until that one settles. Correlated-bet protection."""
    if event_ticker:
        open_n = _open_count_for_event(event_ticker)
        if open_n >= MAX_OPEN_PER_EVENT:
            return False, f"event_cap({event_ticker} open={open_n})"
    with _cycle_budget_lock:
        if _cycle_new_count >= MAX_NEW_POSITIONS_PER_CYCLE:
            return False, f"cycle_cap({_cycle_new_count}/{MAX_NEW_POSITIONS_PER_CYCLE})"
        if _today_exposure_usd + cost_usd > DAILY_EXPOSURE_CAP_USD:
            return False, f"daily_cap(${_today_exposure_usd:.2f}+${cost_usd:.2f}>${DAILY_EXPOSURE_CAP_USD:.2f})"
    return True, ""


def _budget_record(cost_usd: float, event_ticker: str = "") -> None:
    global _cycle_new_count, _today_exposure_usd
    with _cycle_budget_lock:
        _cycle_new_count += 1
        _today_exposure_usd += cost_usd


def _in_paused_cooldown(ticker: str) -> bool:
    with _cooldown_lock:
        until = _paused_tickers.get(ticker, 0.0)
    return time.time() < until


def _set_paused_cooldown(ticker: str) -> None:
    with _cooldown_lock:
        _paused_tickers[ticker] = time.time() + PAUSED_COOLDOWN_SEC


def _in_insufficient_balance_cooldown() -> bool:
    return time.time() < _insufficient_balance_until


def _set_insufficient_balance_cooldown() -> None:
    global _insufficient_balance_until
    _insufficient_balance_until = time.time() + INSUFFICIENT_BALANCE_COOLDOWN_SEC
    log(f"  insufficient_balance cooldown set for {INSUFFICIENT_BALANCE_COOLDOWN_SEC:.0f}s", "warn")


def _compute_today_exposure() -> float:
    """Sum of cost field over all 'entry' records in TRADES_FILE whose UTC
    date matches today. Survives bot restarts: the daily cap is enforced
    against actual on-disk spend, not just in-process state. Without this,
    every restart resets _today_exposure_usd to $0 and the bot can spend
    the full DAILY_EXPOSURE_CAP_USD again."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    if not TRADES_FILE.exists():
        return total
    try:
        with open(TRADES_FILE) as f:
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
        log(f"  today-exposure compute failed: {e}", "warn")
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

    Reads today's `kind=entry` records from TRADES_FILE and adds any whose
    market_ticker is missing from `_open_positions` *and* not already in
    SETTLEMENTS_FILE. Worst-case false-positive (manually-closed-then-not-
    re-entered) is benign — it just blocks a redundant entry until next
    cycle's reconcile catches up. The bug it prevents (CHI T48 double-entry
    2026-04-25) is much worse: real money on correlated dupes.
    """
    if not TRADES_FILE.exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

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
    try:
        with open(TRADES_FILE) as f:
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
        log(f"  trade-log reconcile read failed: {e}", "warn")
        return 0

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


def _load_positions() -> None:
    """Load positions.json, dropping any whose climate day is more than
    POSITION_TTL_DAYS in the past. Defends against orphaned positions that
    never settle (data error) accumulating indefinitely."""
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
        log(f"  loaded {len(_open_positions)} positions (dropped {dropped} > {POSITION_TTL_DAYS}d old)")
    except Exception as e:
        log(f"  positions load failed: {e}", "warn")


def _save_positions() -> None:
    with _positions_lock:
        snap = dict(_open_positions)
    _atomic_write_json(POSITIONS_FILE, snap)


def record_candidate(opp: dict) -> None:
    """Record a candidate opportunity for calibration analysis. Every
    generated opp goes here — not just taken ones — so we can back-test
    the model's probability calibration against settled outcomes.

    2026-04-25 fix: opp has its own `kind` field (bracket/tail_low/tail_high)
    which was silently overwriting our `kind: candidate` discriminator via
    dict-spread. Rename to bracket_kind so the candidate/entry filter works."""
    fields = ("event_ticker", "market_ticker", "station", "series", "date_str",
              "label", "floor", "cap",
              "yes_bid", "yes_ask", "no_bid", "no_ask",
              "volume", "mu", "sigma", "mu_source",
              "mu_nbp", "sigma_nbp", "mu_nbm_om", "mu_hrrr", "disagreement",
              "running_min",
              "post_sunrise_lock", "is_today", "model_prob",
              "yes_ask_frac", "no_ask_frac",
              "action", "edge", "entry_price")
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "candidate",
        "bracket_kind": opp.get("kind"),
        **{k: opp.get(k) for k in fields},
    }
    _append_jsonl(TRADES_FILE, record)


def execute_opportunity(opp: dict) -> bool:
    """Enforce caps, place a real Kalshi limit-buy at the ask, wait up to
    ORDER_FILL_TIMEOUT_SEC for fill via WS cache, cancel any unfilled
    remainder, record actual fill_count + cost. Returns True on a real
    entry, False if any gate blocked or no fill landed."""
    if opp.get("action") is None or opp.get("entry_price") is None:
        return False
    edge = float(opp["edge"])
    ticker = opp["market_ticker"]

    # Per-ticker dedupe — never double up on the same market.
    with _positions_lock:
        if ticker in _open_positions:
            return False

    # _obs_confirmed_alive: rm has decisively settled the bracket in our favor.
    # When True, bypass forecast-based gates (directional, abs_dist, F2A, MSG,
    # disagreement, mu-vs-rm, mp range) and lower the edge floor. Only the
    # spread filter and budget gates still apply.
    obs_alive = _check_obs_confirmed_alive(opp)
    edge_floor = OBS_ALIVE_MIN_EDGE if obs_alive else MIN_EDGE
    if edge < edge_floor:
        return False

    # Kelly sizing — anchor on bankroll (V2 fix; pre-fix anchored on MAX_BET_USD,
    # under-sizing every trade by ~4× when bankroll > MAX_BET_USD). Kelly boost
    # via SIGNAL_KELLY_MULT when obs_confirmed_alive (V2 _SIGNAL_KELLY_MULT port).
    price = float(opp["entry_price"])
    kelly = KELLY_FRACTION * edge / max(1 - price, 0.01)
    if obs_alive:
        kelly *= SIGNAL_KELLY_MULT
    bankroll = _get_bankroll_cached()
    bet_usd = min(MAX_BET_USD, max(MIN_BET_USD, kelly * bankroll))
    count = max(1, int(bet_usd / price))
    count = max(count, math.ceil(MIN_COST_USD / price))
    count = min(count, max(1, int(MAX_BET_USD / price)))
    intended_cost = count * price
    action = opp["action"]
    mp = float(opp.get("model_prob", 0.0))

    if obs_alive:
        log(f"  OBS_CONFIRMED_ALIVE {ticker}: bypassing forecast gates "
            f"(rm={opp.get('running_min')}, action={action}); kelly×{SIGNAL_KELLY_MULT}")
    else:
        # Pre-empt entries where rm has already moved into losing territory.
        # The hard-stop catches these post-entry, but the round-trip is costly
        # (LAX-T54 round-trip 2026-04-27 lost $3.44 in 18 min).
        if _check_obs_confirmed_loser(opp):
            log(f"  skip {ticker}: OBS_CONFIRMED_LOSER — rm={opp.get('running_min')} "
                f"already in YES territory (floor={opp.get('floor')}, cap={opp.get('cap')})")
            return False
        # Forecast-based gates — each prevents a class of model error.
        # Order: cheapest-to-evaluate first.
        if edge > MAX_EDGE:
            log(f"  skip {ticker}: edge {edge:.1%} > MAX_EDGE {MAX_EDGE:.0%} (model likely wrong)")
            return False
        if mp < MIN_MODEL_PROB or mp > MAX_MODEL_PROB:
            log(f"  skip {ticker}: model_prob {mp:.0%} outside [{MIN_MODEL_PROB:.0%},{MAX_MODEL_PROB:.0%}]")
            return False
        # Directional consistency: never bet against our own model.
        if action == "BUY_NO" and mp > 0.40:
            log(f"  skip {ticker}: BUY_NO but model_prob {mp:.0%} > 40% (action vs model disagree)")
            return False
        if action == "BUY_YES" and mp < 0.60:
            log(f"  skip {ticker}: BUY_YES but model_prob {mp:.0%} < 60% (action vs model disagree)")
            return False
        # ABS DISTANCE GATE (BUY_NO only). mu close to bracket midpoint = coin flip.
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
            if bracket_mid is not None:
                mu_val = float(opp.get("mu", 0.0))
                abs_dist = abs(mu_val - bracket_mid)
                if abs_dist < MIN_ABS_DISTANCE_F:
                    log(f"  skip {ticker}: ABS DISTANCE GATE — mu={mu_val:.1f}°F only "
                        f"{abs_dist:.1f}°F from bracket mid={bracket_mid:.1f}°F (min {MIN_ABS_DISTANCE_F:.1f}°F)")
                    return False
        # F2A asymmetry gate (V2 port, BUY_NO only).
        f2a_block = _check_f2a_gate(opp)
        if f2a_block:
            log(f"  skip {ticker}: {f2a_block}")
            return False
        # MSG multi-source consensus gate (V2 port, BUY_NO only).
        msg_block = _check_msg_gate(opp)
        if msg_block:
            log(f"  skip {ticker}: {msg_block}")
            return False
        disagreement = float(opp.get("disagreement", 0.0))
        if disagreement > MAX_DISAGREEMENT_F:
            log(f"  skip {ticker}: forecast disagreement {disagreement:.1f}°F > {MAX_DISAGREEMENT_F:.1f}°F")
            return False
        # Mu-vs-running_min sanity: pre-sunrise, forecast μ vs observed lowest > 5°F = wrong.
        rm = opp.get("running_min")
        if rm is not None and not opp.get("post_sunrise_lock"):
            mu_check = float(opp.get("mu", 0.0))
            if abs(mu_check - float(rm)) > MAX_MU_VS_RM_DIFF_F:
                log(f"  skip {ticker}: μ={mu_check:.1f} vs rm={float(rm):.1f} diff > {MAX_MU_VS_RM_DIFF_F:.1f}°F")
                return False
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
        log(f"  skip {ticker}: spread {spread}c > {MAX_SPREAD_CENTS}c")
        return False
    # Per-cycle / daily / per-event budget.
    ok, reason = _budget_can_take(intended_cost, opp.get("event_ticker", ""))
    if not ok:
        log(f"  skip {ticker}: {reason}")
        return False
    price_cents = int(round(price * 100))
    order_id = place_kalshi_order(ticker, side, count, price_cents)
    if not order_id:
        return False
    status, filled = wait_for_fill(order_id, count, ORDER_FILL_TIMEOUT_SEC)
    if filled <= 0:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{order_id}")
        except Exception:
            pass
        log(f"  no fill on {ticker} (status={status}); cancelled")
        return False
    if filled < count:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{order_id}")
        except Exception:
            pass
        log(f"  partial fill {filled}/{count} on {ticker}; cancelled remainder")
    actual_cost = filled * price
    _budget_record(actual_cost, opp.get("event_ticker", ""))
    record = {
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
    }
    _append_jsonl(TRADES_FILE, record)
    with _positions_lock:
        _open_positions[ticker] = record
    _save_positions()
    log(f"  ENTRY {opp['action']} {filled}x @ {price_cents}c on {ticker} "
        f"| edge={edge:.1%} mp={opp['model_prob']:.0%} mu={opp['mu']:.1f}°F "
        f"σ={opp['sigma']:.1f}°F rm={opp['running_min']} "
        f"| day=${_today_exposure_usd:.2f}/${DAILY_EXPOSURE_CAP_USD:.2f}")
    notify_discord_entry(record, opp)
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
        # T-high (single-bound floor): rm below threshold → NO wins
        elif floor is not None and cap is None:
            if rm < float(floor) - 1.0:
                return True
        # T-low BUY_NO winner case requires post-sunrise; defer.
    elif action == "BUY_YES":
        # T-low: rm has dipped to YES threshold
        if cap is not None and floor is None:
            if rm <= float(cap) - 1.0:
                return True
        # T-high: rm above threshold AND post-sunrise (low won't drop further)
        elif floor is not None and cap is None:
            tz = pos.get("tz", "America/New_York")
            if rm >= float(floor) + 1.0 and _is_post_sunrise(tz):
                return True
    return False


def _execute_exit(ticker: str, pos: dict, sell_side: str, sell_price_c: int,
                   reason: str) -> bool:
    """Place a SELL order for an existing position at sell_price_c. Polls for
    fill, records exit in trades.jsonl with kind='exit', marks position
    settled in the dedupe map. Returns True iff we got any fill."""
    count = int(pos.get("count", 0))
    if count <= 0:
        return False
    if sell_price_c is None or sell_price_c <= 0:
        return False
    oid = place_kalshi_sell_order(ticker, sell_side, count, sell_price_c)
    if not oid:
        return False
    status, filled = wait_for_fill(oid, count, ORDER_FILL_TIMEOUT_SEC)
    if filled <= 0:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{oid}")
        except Exception:
            pass
        log(f"  exit no-fill on {ticker} (status={status}); cancelled")
        return False
    if filled < count:
        try:
            kalshi_delete(f"/trade-api/v2/portfolio/orders/{oid}")
        except Exception:
            pass
        log(f"  exit partial fill {filled}/{count} on {ticker}; cancelled remainder")
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
    _append_jsonl(TRADES_FILE, record)
    with _positions_lock:
        existing = _open_positions.get(ticker)
        if existing is not None:
            existing.update({
                "settled": True,
                "exited_ts": record["ts"],
                "exit_price": record["exit_price"],
                "pnl": pnl,
                "_exit_reason": reason,
            })
    _save_positions()
    log(f"  EXIT FILLED {ticker} ({reason}): {filled}x @ {sell_price_c}c | "
        f"pnl ${pnl:+.2f}")
    discord_send(
        f"🟡 **EXIT** `{ticker}` {pos.get('action')} {filled}x @ {sell_price_c}c "
        f"({reason}) — P&L **${pnl:+.2f}**"
    )
    return True


def check_open_positions_for_exit(market_quotes: dict[str, dict]) -> int:
    """Mid-cycle exit check. For each open non-settled position, look up the
    current bid in market_quotes (caller passes a {ticker: market_dict} index
    from discover_markets). Triggers:

      1. OBS_CONFIRMED_WINNER override → SKIP exit (hold guaranteed wins
         even if MTM looks awful — V2 lesson: thin-book price noise faked
         losses on confirmed winners).
      2. HARD STOP: MTM loss ≥ HARD_STOP_BRACKET_LOSS_PCT (B-bracket) or
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

        # OBS_CONFIRMED_WINNER override — never sell a guaranteed winner.
        rm = pos.get("running_min")
        # If pos was reconciled from Kalshi without rm context, fetch current rm.
        if rm is None:
            station = pos.get("station")
            date_str = pos.get("date_str")
            if station and date_str:
                rm = get_running_min(station, date_str)
        if rm is not None:
            try:
                if _check_position_obs_winning(pos, float(rm)):
                    if loss_pct > 0.30:  # only log when override matters
                        log(f"  HOLD {ticker}: rm={rm} confirms winner; "
                            f"ignoring MTM loss {loss_pct:.0%}")
                    continue
            except Exception:
                pass

        # Hard stop.
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

def check_settlements() -> int:
    """Walk open positions; for each, check if the settlement CLI low has
    been published. If so, compute P&L and archive."""
    settled = 0
    with _positions_lock:
        positions = dict(_open_positions)
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
        if cli_low is None:
            continue  # settlement not yet available
        # Determine outcome
        floor = pos.get("floor")
        cap = pos.get("cap")
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
        }
        _append_jsonl(SETTLEMENTS_FILE, settlement)
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
                    "cli_low": cli_low,
                    "in_bracket": in_bracket,
                    "won": our_win,
                    "pnl": pnl,
                })
        settled += 1
        log(f"  SETTLED {ticker} | action={action} CLI_low={cli_low}°F "
            f"in={in_bracket} won={our_win} pnl=${pnl:+.2f}")
        notify_discord_settlement(ticker, action, cli_low, in_bracket, our_win, pnl)
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

    # Forecast refresh (throttled by internal TTL)
    global _nbp_cache_ts
    with _nbp_cache_lock:
        nbp_age = time.time() - _nbp_cache_ts
    if nbp_age > 6 * 3600:
        try:
            refresh_nbp_forecasts()
        except Exception as e:
            log(f"  NBP refresh failed: {e}", "warn")

    # NBM-OM refresh (check cached per-series freshness)
    try:
        # Simple: refresh once per cycle if any entry is older than 1h
        need_refresh = False
        with _nbm_om_cache_lock:
            for series in CITIES:
                entries = _nbm_om_cache.get(series, {})
                if not entries:
                    need_refresh = True
                    break
                newest = max((e.get("fetched", 0) for e in entries.values()), default=0)
                if time.time() - newest > NBM_OM_TTL_SEC:
                    need_refresh = True
                    break
        if need_refresh:
            refresh_nbm_om_forecasts()
    except Exception as e:
        log(f"  NBM-OM refresh failed: {e}", "warn")

    # HRRR refresh — more frequent (10-min TTL) since HRRR updates hourly
    try:
        need_refresh = False
        with _hrrr_cache_lock:
            for series in CITIES:
                entries = _hrrr_cache.get(series, {})
                if not entries:
                    need_refresh = True
                    break
                newest = max((e.get("fetched", 0) for e in entries.values()), default=0)
                if time.time() - newest > HRRR_TTL_SEC:
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
    for opp in taken:
        ticker = opp["market_ticker"]
        with _positions_lock:
            if ticker in _open_positions:
                continue
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
