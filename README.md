# min_bot

Live trading bot for Kalshi low-temperature markets (`KXLOWT*`). Same 20
cities as V1/V2 but opposite settlement: daily minimum instead of maximum.

**Live since 2026-04-25 on the V1 Kalshi wallet** (key from `~/.env`
`KALSHI_KEY_ID`, PEM `~/kalshi_key.pem` — same wallet V1's max bot uses).
Conservative caps: $1 per entry, 3 entries per cycle, $4 daily exposure,
20% min edge.

> File / dir / service names still carry the `paper-min-bot` prefix from the
> initial paper-trading scaffold. Keeping them avoids touching the systemd
> unit + git remote. Internally everything is live-only.

## Architecture

- **obs-pipeline dependency**: reads `running_min` from obs-pipeline's SQLite
  DB for real-time overnight-low tracking (added 2026-04-24 alongside existing
  `running_max`).
- **Forecasts**: NBP (NBM Probabilistic text bulletins on AWS S3, `TNNMN`/`TNNSD`
  for min-temp mu + sigma). Fallback to NBM via Open-Meteo daily
  `temperature_2m_min`. HRRR via paid Open-Meteo on day-0.
- **Model**: truncated Gaussian bounded above by `running_min + 1.0°F` (the
  +1°F is the ASOS-vs-CLI buffer; CLI rounds to integer and our 5-min obs
  may be 0.5°F off). Post-sunrise, sigma collapses to 1.0°F residual noise.
- **Settlement**: reads CLI daily low from obs-pipeline's `cli_reports.low_f`
  field (same source V1/V2 use for CLI high).

## Data flow

```
obs-pipeline                    min_bot
─────────────                   ──────────────
ingests ASOS/MADIS/AWC
  → running_min table        →  get_running_min(station, date)
  → observations             →  get_latest_obs(station)
  → cli_reports.low_f        →  check_settlements()

AWS S3 NBM Probabilistic      →  refresh_nbp_forecasts()
Open-Meteo NBM daily min      →  refresh_nbm_om_forecasts()
Open-Meteo HRRR (paid)        →  refresh_hrrr_forecasts()

Kalshi REST  /markets         →  discover_markets() (per-series, parallel)
Kalshi WS    orderbook_delta  →  kalshi_ws.get_bbo() (sub-100ms BBO overlay)
Kalshi REST  /portfolio/...   →  place_kalshi_order, wait_for_fill, cancel
Discord webhook → channel     →  notify_discord_entry / _settlement
```

## Kalshi quote source

Discovery uses `/trade-api/v2/markets?series_ticker=X&status=open`, parsed via
`yes_bid_dollars` / `yes_ask_dollars` (the `/events?with_nested_markets` path
returns those fields as `None` and is unsuitable for trading). Per-cycle cost
is 20 calls, parallelized 5-at-a-time (~500 ms wall). Pattern ported from
`obs-pipeline-bot/kalshi_weather_bot_v2.py`.

`kalshi_ws.py` (also ported from V2) maintains a WebSocket subscription to
`orderbook_delta` for every discovered ticker. `_overlay_ws_bbo()` overrides
the REST snapshot with cached BBO when fresh (≤10 s), dropping quote staleness
from one cycle (~60 s) to ~50 ms. WS BBO is for read-side only — order
placement still routes through REST. Disable with `USE_KALSHI_WS = False`.

## Order flow

1. `discover_markets()` returns quoted markets (REST) with WS BBO overlay.
2. `find_opportunities()` filters per-market: skips if obs-pipeline already
   has CLI for `(station, climate-day)`, or `date_str < today` for the city's
   TZ. Computes `mu`, `sigma`, `running_min`, `model_prob`, `edge`.
3. `execute_opportunity()` runs the safety gate stack (below), dedupes per
   ticker, checks cycle / daily / per-event budgets, sizes via Kelly.
4. `place_kalshi_order()` POSTs a limit buy at the ask price.
5. `wait_for_fill()` polls the `kalshi_ws` fill cache for up to 5 s, falls
   back to REST `/portfolio/orders/{id}` (authoritative).
6. Partial / no-fill: cancel any resting remainder. Record actual `filled`
   count to `data/trades.jsonl` and emit a Discord notification.
7. `check_settlements()` walks open positions; on CLI publish, computes P&L
   and emits a Discord settlement notification.

## Caps and gates

All in `paper_min_bot.py`:

| Constant | Value | Why |
|---|---|---|
| `MAX_BET_USD` | `$10.00` | Kelly-sized but capped per entry. $1 (live launch) → $3 (2026-04-26) → $5 (2026-04-27 PM) → $10 (2026-04-27 evening, after 3-of-3 winning settlements demonstrated the post-V2-port edge). $59 bankroll supports ~6 concurrent $10 bets without breaching per-event correlation. |
| `MIN_COST_USD` | `$1.00` | Cost floor: `count = max(count, ceil(MIN_COST_USD/price))` after Kelly + MIN_BET_USD floor + int rounding. Without this, `int(bet_usd/price)` rounded `count` down such that 96% of fills landed under $1 (avg $0.45 on 2026-04-25/26). The cap clamp at MAX_BET_USD keeps the floor from blowing the ceiling on cheap contracts. Added 2026-04-27. |
| `MAX_NEW_POSITIONS_PER_CYCLE` | `3` | First test cycle took 55 paper entries; cap stops runaway |
| `MAX_OPEN_PER_EVENT` | `1` | Lifetime cap (counts open positions, not per-cycle). Once one bracket on an event is open, all other brackets on that event are blocked until it settles. CHI-26APR25 stacked 4 brackets across cycles 2026-04-25 under the prior per-cycle rule. |
| `DAILY_EXPOSURE_CAP_USD` | `$60.00` | $4 → $15 (2026-04-25) → $30 (2026-04-26) → $60 (2026-04-27, paired with MIN_COST_USD floor) |
| `BANKROLL_FLOOR_USD` | `$5.00` | Startup refuses to run if balance below floor |
| `MIN_EDGE` | `0.20` | Take only edges ≥ 20% |
| `MAX_EDGE` | `0.42` | Skip edges > 42% (V1 trust-zone — model error). Bumped 0.40 → 0.42 (2026-04-27 evening) as a cautious half-step toward V2's NWS-PRIMARY-era 0.45. |
| **PRICE_ZONE** | `yes_bid 30-40c` | **BUY_NO only** (V2 port). Skip when market YES bid ∈ [30c, 40c] — market is uncertain (NO costs 60-70c) and our model with high BUY_NO confidence usually disagrees with the market for the wrong reasons. V2 backtest: 50% WR / −$99 / n=50. Bypassed by `_obs_confirmed_alive`. |
| **H_2.0** | `disagreement 2°F` | **BUY_NO d-1+ only** (V2-inspired). Skip when pairwise forecast disagreement (NBP/HRRR/NBM max diff) > 2°F on day-1+ markets where we have no obs to break ties. Tighter than `MAX_DISAGREEMENT_F=5.0`. Bypassed by `_obs_confirmed_alive`. |
| `MIN_MODEL_PROB` / `MAX_MODEL_PROB` | `0.15` / `0.85` | Skip wildly unlikely or near-certain |
| **Directional consistency** | `mp 0.40 / 0.60` | BUY_NO requires `mp ≤ 0.40`; BUY_YES requires `mp ≥ 0.60`. Don't bet against your own model. Tightened 2026-04-27 from 0.50 → 0.40/0.60 to drop coin-flip-zone entries (CHI-T41 mp=34% and NYC-T44 mp=42% were both BUY_YES losers admitted at the old 0.50 threshold). The edge formula assumes a calibrated model; with the +1.24°F NBP-cool bias, edge alone misleads when action and direction disagree. |
| `MIN_ABS_DISTANCE_F` | `0.5°F` | **BUY_NO only** — skip when `\|mu − bracket_mid\| < 0.5°F` (mu inside or near bracket center). 1.0 → 1.5 (2026-04-27 AM) → reverted to 0.5 (2026-04-27 PM) after Kalshi-truth audit on n=15: at 1.5°F we'd have blocked 9/9 winners with dist 0.5–1.5°F (BUY_NO with mu *at the bracket edge*, not inside). PHIL-B44.5 (0.1°F, mu inside) is still caught at 0.5°F. BUY_YES intentionally not gated. |
| **F2A asymmetry gate** | mp / sigma | **BUY_NO only** (V2 port). Block if `mp < 0.05` (price-asymmetry trap), `mp ≥ 0.30` (calibration cliff), or `sigma < 1.5°F` (over-confident model). V2's distance-from-edge sub-check NOT ported — min-bot audit found at-edge mu is the BUY_NO sweet spot. Bypassed when `_obs_confirmed_alive`. |
| **MSG multi-source consensus** | per-city tier | **BUY_NO only** (V2 port). Count how many of {NBP, HRRR, NBM} predict YES. Default cities allow up to 2 disagreeing; WORST cities (NYC/SEA/PHIL/LV/NOLA/DEN) require unanimity. Outlier > 3°F into YES territory blocks separately. Bypassed when `_obs_confirmed_alive`. |
| `_obs_confirmed_alive` | rm vs bracket | **Bypass** (V2 port). When `running_min` has decisively settled the bracket (e.g. rm < floor − 3°F for BUY_NO B-bracket; rm ≤ cap − 1°F for BUY_YES T-low), skip all forecast-based gates, lower edge floor to 5%, multiply Kelly by 1.5×. Mirror of V2's `_obs_confirmed_dead` for max-bot. |
| `_obs_confirmed_loser` | rm vs bracket | **Pre-empt** (mirror of `_obs_confirmed_alive`). When `running_min` has already moved into the YES territory of our action (e.g. BUY_NO T-high with rm > floor; BUY_YES B-bracket with rm < floor), skip the entry. The hard-stop catches these post-entry but the round-trip is costly: LAX-T54 lost $3.44 in 18 min on 2026-04-27 12:44 entry — rm was already 57.2°F vs floor 54.5°F when bot bought BUY_NO based on HRRR's stale mu=53.1°F forecast. |
| **Kelly anchor** | bankroll | (V2 port). Pre-fix anchored on MAX_BET_USD (sized as if bankroll=$5 — every fill hit the $1 floor). Post-fix: `bet_usd = kelly × bankroll`, capped at MAX_BET_USD. With $21 bankroll + 25% Kelly + 25% edge + 50c price, sizes $2.62 instead of $0.625. Bankroll cached, refreshed every BANKROLL_REFRESH_SEC. |
| **Hard stop** | mid-cycle exit | (V2 port). Sells at current bid if MTM loss ≥ 80% (B-bracket) or 70% (tail). Override: skip exit if `_obs_confirmed_winner` (rm confirms our side wins) — never sell guaranteed wins on price noise. New `check_open_positions_for_exit` runs each cycle, sees market quotes from `discover_markets`. |
| `MAX_DISAGREEMENT_F` | `5.0°F` | Skip if HRRR vs NBP / NBP vs NBM diverge > this |
| `MAX_SPREAD_CENTS` | `10c` | Skip if ask − bid > 10c on buying side |
| `MAX_MU_VS_RM_DIFF_F` | `5.0°F` | Pre-sunrise: forecast μ vs observed rm sanity gate |
| `POSITION_TTL_DAYS` | `3` | Drop positions on load whose climate day is older than this |
| `ORDER_FILL_TIMEOUT_SEC` | `5.0` | Wait this long for fill, then cancel |

## Cooldowns (V2 H-2 fix port)

- 409 `trading_is_paused` from Kalshi → 60-second per-ticker cooldown.
- 400 `insufficient_balance` → 5-minute account-wide cooldown.

V1 logged 11k retries in one day before V2 added these.

## Wallet selection

`WALLET = "v1"` (default) loads `~/.env` `KALSHI_KEY_ID` and `~/kalshi_key.pem`.
Set `WALLET = "v2"` for the obs-pipeline-bot's secondary account
(`obs-pipeline-bot/kalshi_key_v2_account2.pem`).

## Discord notifications

- Channel `1497464077608550570` receives `ENTRY` and `SETTLED` messages.
- Bot token comes from `DISCORD_BOT_TOKEN` in `~/.env` (same token V1/V2 use;
  must already be a member of the channel).
- One bounded queue + one worker thread; non-blocking; drops on overflow.

## Data dir

`/home/ubuntu/paper_min_bot/data/`:
- `trades.jsonl` — every candidate + entry (full context for calibration)
- `positions.json` — currently open positions
- `settlements.jsonl` — settled outcomes + P&L
- `nbp_cache.json` — persisted NBP forecasts across restarts

## Running

```bash
sudo systemctl start paper-min-bot.service
sudo systemctl status paper-min-bot.service
sudo journalctl -u paper-min-bot -f

# manual (development)
python3.12 /home/ubuntu/paper_min_bot/paper_min_bot.py
```

## Kill switch

`sudo systemctl stop paper-min-bot`. Open positions stay on Kalshi and
self-settle from the `cli_reports` low.

## Startup self-heal

On boot, the bot runs three reconciliation passes in order so it never
double-buys after a deploy or crash:

1. `_load_positions()` — reads `positions.json` (TTL filter).
2. `_reconcile_kalshi_positions()` — pulls `/portfolio/positions` and adds
   any KXLOWT* holding the bot doesn't track (the original "ghost
   position" defense). For T-tail tickers (where `parse_market_bracket`
   alone leaves `floor=cap=None`), additionally fetches `/markets/{ticker}`
   and parses `yes_sub_title` ("X° or above" → T-high `floor=X-0.5`,
   "X° or below" → T-low `cap=X+0.5`). Without this, recovered T-tails
   default `in_bracket=True` at settlement and silently invert the `won`
   flag — confirmed inversions on CHI-T48, SEA-T42, LV-T58, MIN-T45 in
   the 2026-04-27 audit.
3. `_reconcile_from_trades_log()` — reads today's `kind=entry` records
   from `trades.jsonl` and adds any whose ticker is missing from
   `_open_positions` *and* not already in `settlements.jsonl`. Closes
   the Kalshi `/portfolio/positions` API-lag window. Without this,
   KXLOWTCHI-26APR25-T48 was bought twice on 2026-04-25 (16 min apart,
   between back-to-back deploy restarts) because positions.json was
   clobbered AND Kalshi hadn't propagated the fill yet.

## Files touched by the obs-pipeline extension (2026-04-24)

To support this bot, obs-pipeline gained parallel min-tracking alongside its
existing max-tracking:

- `pipeline/storage/db.py`: new `running_min` / `running_min_local` tables +
  `upsert_running_min()` (lowest-wins) + `_OBS_RUNNING_MIN_SOURCES` allow-list
- `pipeline/storage/snapshot.py`: `StationSnapshot` gained `min_f_nws`,
  `min_f_local`, `min_obs_time`, `min_source`, `cli_low`, `dsm_low` fields
  (defaults to None — backward-compatible with V2)
- `pipeline/sources/{awc_metar,ldm_files,madis_netcdf,nws_obs}.py`: each call
  to `upsert_running_max` is now paired with `upsert_running_min` of the same
  obs; snapshot updates now include min fields.
- `tests/test_running_min.py`: 10 unit tests covering lowest-wins, source
  allow-list, parallel-to-max regression, snapshot field preservation.

V2 is unaffected — still reads `max_f_nws`/`max_f_local` from `/snapshot`,
ignoring the new min fields.

## Not yet implemented (backlog)

- HRRR overnight nowcast blending (HRRR 0-18h min trajectory)
- NWS gridpoint minTemperature as tertiary mu source
- `seed_running_min_from_observations()` wired into obs-pipeline startup
- client_order_id-based idempotency on retry
- Time-to-settlement-aware sigma collapse (smooth interpolation, not just
  the binary `_is_post_sunrise` flip)
- Web dashboard (calibration curve, P&L by city, rolling Brier score)
