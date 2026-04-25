# paper-min-bot

Trading bot for Kalshi low-temperature markets (`KXLOWT*`). Same 20 cities as
V1/V2 but opposite settlement: daily minimum instead of maximum.

**Live since 2026-04-25 on the V1 Kalshi wallet** (key from `~/.env`
`KALSHI_KEY_ID`, PEM `~/kalshi_key.pem` — same wallet V1's max bot uses).
`PAPER_MODE=False`. Conservative live caps: $1 per entry, 3 entries per cycle,
$20 daily exposure, 20% min edge. Flip back with `PAPER_MODE=True`.

## Architecture

- **obs-pipeline dependency**: reads `running_min` from obs-pipeline's SQLite
  DB for real-time overnight-low tracking (added 2026-04-24 alongside existing
  `running_max`).
- **Forecasts**: NBP (NBM Probabilistic text bulletins on AWS S3, `TNNMN`/`TNNSD`
  for min-temp mu + sigma). Fallback to NBM via Open-Meteo daily
  `temperature_2m_min`.
- **Model**: truncated Gaussian bounded above by `running_min` (the final daily
  min cannot exceed the lowest observed temperature so far). Post-sunrise, sigma
  collapses to 0.5°F residual ASOS-vs-CLI noise — the min is essentially fixed
  once the sun's been up for an hour.
- **Settlement**: reads CLI daily low from obs-pipeline's `cli_reports.low_f`
  field (same source V1/V2 use for CLI high).

## Data flow

```
obs-pipeline                   paper-min-bot
─────────────                  ──────────────
ingests ASOS/MADIS/AWC
  → running_min table       →  get_running_min(station, date)
  → observations           →  get_latest_obs(station)
  → cli_reports.low_f       →  check_settlements()

AWS S3 NBM Probabilistic     →  refresh_nbp_forecasts()
Open-Meteo NBM daily min     →  refresh_nbm_om_forecasts()

Kalshi REST  /markets         → discover_markets() (per-series, parallel)
Kalshi WS    orderbook_delta  → kalshi_ws.get_bbo() (sub-100ms BBO overlay)
```

## Kalshi quote source (2026-04-24)

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

## Data dir

`/home/ubuntu/paper_min_bot/data/`:
- `paper_trades.jsonl` — every candidate + entry (full context for calibration)
- `paper_positions.json` — currently open hypothetical positions
- `paper_settlements.jsonl` — settled outcomes + P&L
- `paper_stats.json` — aggregates
- `paper_nbp_cache.json` — persisted NBP forecasts across restarts

## Running

```bash
# systemd
sudo systemctl enable paper-min-bot.service
sudo systemctl start paper-min-bot.service
sudo systemctl status paper-min-bot.service
sudo journalctl -u paper-min-bot -f

# manual (development)
python3.12 /home/ubuntu/paper_min_bot/paper_min_bot.py
```

## Live mode (current)

Live executor placed at the limit-buy ask, fill-aware via `kalshi_ws`, with
hard caps gating every entry. Order placement mirrors V2's `place_order`
without maker-remainder / paused-cooldown logic.

**Caps** (in `paper_min_bot.py`):
- `MAX_BET_USD = 1.00` — Kelly-sized but capped per entry.
- `MAX_NEW_POSITIONS_PER_CYCLE = 3` — first cycle of a fresh restart took 55
  paper entries; the cycle cap stops a runaway.
- `DAILY_EXPOSURE_CAP_USD = 20.00` — sum of fill costs since UTC midnight.
- `MIN_EDGE_LIVE = 0.20` — bumped from 0.15 to skip the marginal 78c BUY_NO
  entries the model overproduces.
- `BANKROLL_FLOOR_USD = 5.00` — startup refuses to run if portfolio balance
  drops below this.

**Wallet selection.** `WALLET = "v1"` (default) loads `~/.env` `KALSHI_KEY_ID`
and `~/kalshi_key.pem`. Set `WALLET = "v2"` for the obs-pipeline-bot's
secondary account.

**Skip-if-resolved guard (2026-04-25).** `find_opportunities` skips any
market whose `(station, climate-day)` pair already has a CLI low recorded
in obs-pipeline. Prevents the post-CLI / pre-Kalshi-close window where the
market is still tradeable but the answer is decided. Also skips any market
whose climate day is already strictly in the past for the city's TZ.

**+1°F obs-vs-CLI buffer (2026-04-25).** `running_min` (5-min ASOS samples)
and Kalshi's settlement CLI (2-min averages, integer-rounded) can differ by
~1°F. Mirrors the V1/V2 max-bot fix. In `calc_bracket_probability_min`:
running_min is treated as `running_min + 1.0` for the truncation upper
bound and the "bracket impossible" guard, and the post-sunrise lock uses
σ=1.0°F (was 0.5°F).

**Order flow.**
1. `discover_markets()` returns quoted markets (REST) with WS BBO overlay.
2. `find_opportunities()` filters; `execute_opportunity()` dedupes per ticker
   and checks the cycle/daily caps.
3. `place_kalshi_order()` POSTs a limit buy at the ask price.
4. `wait_for_fill()` polls the `kalshi_ws` fill cache for up to 5 seconds,
   falls back to REST `/portfolio/orders/{id}`.
5. Partial / no-fill: cancel any resting remainder. Record actual `filled`
   count to `paper_trades.jsonl`.

**Kill switch:** `sudo systemctl stop paper-min-bot`. Open positions stay
on Kalshi and self-settle from the `cli_reports` low.

**Flipping back to paper:** set `PAPER_MODE = True` and restart.

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
  (so fresh restarts correctly initialize today's running_min from the
  observations table instead of whatever obs arrives first post-restart)
- Discord alerts (route paper bot alerts to a separate channel)
- Web dashboard (calibration curve, P&L by city, rolling Brier score)
