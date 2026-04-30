# min_bot

Live trading bot for Kalshi low-temperature markets (`KXLOWT*`). Same 20
cities as V1/V2 but opposite settlement: daily minimum instead of maximum.

**Live since 2026-04-25 on the V1 Kalshi wallet** (key from `~/.env`
`KALSHI_KEY_ID`, PEM `~/kalshi_key.pem` — same wallet V1's max bot uses).
Current caps (2026-04-29 evening): **$30 per entry, 3 entries per cycle,
daily exposure unlimited (bankroll itself is the binding constraint), 20%
min edge.** Bumped
iteratively from launch's `$1 / 3 / $4 / 20%` as the bot's track record
built. Full evolution in the constants table below.

> File / dir / service names still carry the `paper-min-bot` prefix from the
> initial paper-trading scaffold. Keeping them avoids touching the systemd
> unit + git remote. Internally everything is live-only.

## Architecture

- **obs-pipeline dependency**: reads `running_min` from obs-pipeline's SQLite
  DB for real-time overnight-low tracking (added 2026-04-24 alongside existing
  `running_max`).
- **Forecasts**: NBP (NBM Probabilistic text bulletins on AWS S3, `TNNMN`/`TNNSD`
  for min-temp μ + σ) is the default mu source for d-1+. NBM via Open-Meteo
  daily `temperature_2m_min` is the fallback. HRRR via paid Open-Meteo is
  the d-0 primary source for all cities, AND the d-1+ primary for CHI / OKC
  via `PER_SERIES_D1_PRIMARY` (source-MAE audit 2026-04-29: HRRR beat NBP
  5× / 1.6× respectively at those stations).
- **Model**: truncated Gaussian bounded above by `running_min + 1.0°F` (the
  +1°F is the ASOS-vs-CLI buffer; CLI rounds to integer and our 5-min obs
  may be 0.5°F off). σ inflation pipeline (in order, all multiplicative):
  disagreement-based (1×→1.5× as NBP/HRRR/NBM diverge 2°F→5°F) → NBP
  staleness (1×→1.30× as NBP cache ages 1h→7h, d-1+ only) → per-station
  multiplier (`PER_SERIES_SIGMA_MULT`: KLAX=2.5, KPHX=2.0, KDEN=1.5,
  KLAS=1.5 — the 4 stations with HRRR MAE > 4°F per source_audit).
  *Post-sunrise σ collapse was DISABLED 2026-04-25*: the heuristic was too
  tight — Apr 25 V1 positions had market prices nowhere near 99/1 at
  mid-morning, proving the daily low can still drop later in the climate
  day. Keep full σ all day; the running_min+1°F truncation is the safety
  net (low can only go down).
- **Settlement**: reads CLI daily low from obs-pipeline's `cli_reports.low_f`
  field (same source V1/V2 use for CLI high). **`_cli_is_final` gate** — only
  treat the most recent CLI as authoritative when its `issued_time` is at
  least `CLI_FINAL_BUFFER_H = 6 h` past climate-day-end LST. Below that
  threshold the CLI is treated as a partial intra-day reading (4 PM
  "VALID AS OF 0400 PM LOCAL TIME") and we wait. Added 2026-04-29 after a
  phantom-settle bug: bot logged `KXLOWTSATX-26APR29-T73 WIN +$5.22` from a
  4 PM CDT partial CLI showing low=77; market priced 89% NO because evening
  cooling drove the actual climate-day low into the 60s. **Kalshi fallback**
  when obs-pipeline missed a CLI bulletin OR the CLI is still partial: if
  `cli_low is None` (or gated to None) for a position whose `date_str <
  today`, query `/portfolio/settlements` and use Kalshi's `market_result` to
  settle locally. Recovers stuck positions caused by NWS bulletin ingestion
  gaps (settlement record gets `source: "kalshi"` and `cli_low: null` so
  calibration analysis can filter).
- **Logging**: skip-log lines for the same `(market_ticker, msg)` pair are
  debounced — first occurrence logs, repeats are suppressed for 30 min, then
  re-logged once for visibility. Pre-fix the log was 83% repeated skip lines
  (top offender: 2108 firings of one ticker in 24h). Real events
  (`ENTRY`/`EXIT`/`SETTLED`/`OBS_CONFIRMED_ALIVE`) always log.

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
   count to today's `data/trades_YYYY-MM-DD.jsonl` and emit a Discord
   notification.
7. `check_settlements()` walks open positions; on CLI publish, computes P&L
   and emits a Discord settlement notification.

## Caps and gates

All in `paper_min_bot.py`:

| Constant | Value | Why |
|---|---|---|
| `MAX_BET_USD` | `$30.00` | Kelly-sized but capped per entry. $1 (live launch) → $3 (2026-04-26) → $5 (2026-04-27 PM) → $10 (2026-04-27 evening) → $15 (2026-04-28) → $20 (2026-04-28 night, paired with $200 bankroll add to ~$279) → **$30 (2026-04-29 evening, per Chris)**. Kelly @ 25% on 25% edge × 50c price wants ~$35/bet on $279 bankroll; $30 cap finally lets full Kelly run on the highest-conviction trades. Per-position blast radius still bounded (~10% of bankroll). |
| `MIN_COST_USD` | `$1.00` | Cost floor: `count = max(count, ceil(MIN_COST_USD/price))` after Kelly + MIN_BET_USD floor + int rounding. Without this, `int(bet_usd/price)` rounded `count` down such that 96% of fills landed under $1 (avg $0.45 on 2026-04-25/26). The cap clamp at MAX_BET_USD keeps the floor from blowing the ceiling on cheap contracts. Added 2026-04-27. |
| `MAX_NEW_POSITIONS_PER_CYCLE` | `3` | First test cycle took 55 paper entries; cap stops runaway |
| `MAX_OPEN_PER_EVENT` | `1` | Lifetime cap (counts open positions, not per-cycle). Once one bracket on an event is open, all other brackets on that event are blocked until it settles. CHI-26APR25 stacked 4 brackets across cycles 2026-04-25 under the prior per-cycle rule. |
| `DAILY_EXPOSURE_CAP_USD` | `$10000.00` (sentinel) | $4 → $15 (2026-04-25) → $30 (2026-04-26) → $60 (2026-04-27) → $120 (2026-04-28 night) → **effectively unlimited (2026-04-29 evening, per Chris)**. With ~$279 bankroll, $20/bet, 3 entries/cycle, and `BANKROLL_FLOOR_USD=$5`, the bankroll itself is the binding constraint — the bot stops placing orders once balance drops below $5. The $10,000 sentinel is unreachable at current scale; revisit only if bankroll grows past $5k. |
| `BANKROLL_FLOOR_USD` | `$5.00` | Startup refuses to run if balance below floor |
| `MIN_EDGE` | `0.20` | Take only edges ≥ 20% |
| `MAX_EDGE` | `0.45` | Skip edges > 45%. Evolution: 0.40 → 0.42 (2026-04-27) → 0.55 with NBP-CLI bypass (2026-04-28 night) → **0.45 with bypass rolled back** (2026-04-29 early). Backtest on n=8 historical bypass-passers: 5/5 BUY_NO MAX_EDGE-bypass cases LOST (μ at-or-near bracket boundary — honest forecasts still landing in the wrong bracket). High apparent edge IS a real model-error signal even when NBP aligns with recent CLI. **No bypass.** |
| ~~PRICE_ZONE~~ | ~~yes_bid 30-40c~~ | **REMOVED 2026-04-29** after gate-audit on min_bot historical candidates: 2/2 PRICE_ZONE-blocked cases were winners (NYC-26APR25-B42.5 BUY_NO @66¢ → +$0.34/c; ATL-26APR27-B59.5 BUY_NO @67¢ → +$0.33/c). V2's max-temp finding (50% WR / −$99 / n=50) does not appear to hold for min-temp markets. Sample is small (n=2) but 100% winners and gate-cost is non-trivial; re-enable from git history if a future audit flips the signal. |
| **H_2.0** | `disagreement 2°F` | **BUY_NO d-1+ only** (V2-inspired). Skip when pairwise forecast disagreement (NBP/HRRR/NBM max diff) > 2°F on day-1+ markets where we have no obs to break ties. Tighter than `MAX_DISAGREEMENT_F=5.0`. Bypassed by `_obs_confirmed_alive`. |
| `MIN_MODEL_PROB` / `MAX_MODEL_PROB` | `0.15` / `0.85` | Skip wildly unlikely or near-certain. **Bypassed by `_nbp_consistent_with_recent_cli`** when forecast μ is within ±2°F of the station's last-7-day CLI low range. Backtest 2026-04-29: 3/3 historical bypass cases (cheap BUY_NO with mp 3-11%, μ clearly outside bracket, NBP consistent) all won — the gate was blocking legit "very confident NO" trades. Bypass kept for this gate only; same bypass on MAX_EDGE was rolled back same day after 5/5 losses. |
| **Directional consistency** | `mp 0.20 / 0.60` | BUY_NO requires `mp ≤ 0.20`; BUY_YES requires `mp ≥ 0.60`. Don't bet against your own model. **Evolution**: 0.50 (initial) → 0.40/0.60 (2026-04-27, CHI-T41 mp=34% / NYC-T44 mp=42% BUY_YES losers triggered) → **BUY_NO 0.40 → 0.20** (2026-04-29 night, V2 port). 2026-04-29 audit on deduped n=42 BUY_NO pool: trades with mp ∈ (0.20, 0.40] had 12L:1W, lift +$5.16 over no-filter; every mp bucket above 0.20 is net negative. April 29 forward-test: of 13 entries, only LAX-30-B56.5 would have been newly blocked (currently coin-flip; small + impact). BUY_YES side kept at 0.60 — symmetric tightening to 0.80 not yet validated on min_bot. mp_range_bypass (NBP-CLI consistency) applies to MIN/MAX_MODEL_PROB only, NOT to directional consistency. |
| `MIN_ABS_DISTANCE_F` | `0.5°F` | **BUY_NO only** — skip when `\|mu − bracket_mid\| < 0.5°F` (mu inside or near bracket center). 1.0 → 1.5 (2026-04-27 AM) → reverted to 0.5 (2026-04-27 PM) after Kalshi-truth audit on n=15: at 1.5°F we'd have blocked 9/9 winners with dist 0.5–1.5°F (BUY_NO with mu *at the bracket edge*, not inside). PHIL-B44.5 (0.1°F, mu inside) is still caught at 0.5°F. BUY_YES intentionally not gated. |
| **F2A asymmetry gate** | mp / sigma | **BUY_NO only** (V2 port). Block if `mp < 0.05` (price-asymmetry trap), `mp ≥ 0.30` (calibration cliff), or `sigma < 1.5°F` (over-confident model). V2's distance-from-edge sub-check NOT ported — min-bot audit found at-edge mu is the BUY_NO sweet spot. Bypassed when `_obs_confirmed_alive`. |
| **MSG multi-source consensus** | per-city tier | **BUY_NO only** (V2 port). Count how many of {NBP, HRRR, NBM} predict YES. Default cities allow up to 2 disagreeing; WORST cities (NYC/SEA/PHIL/LV/NOLA/DEN) require unanimity. Outlier > 3°F into YES territory blocks separately. Bypassed when `_obs_confirmed_alive`. |
| **LAX_NO_THIGH** | `series == KXLOWTLAX` | **BUY_NO T-high block, KLAX-only** (2026-04-29). LAX BUY_NO T-high lost 0/3 historical (μ=53–56 vs cli 58–60); NBP runs 2.5–4°F cool on KLAX so betting "low won't reach X" is structurally the wrong direction at any threshold inside KLAX's observed 54-60°F range. KLAX BUY_NO B-bracket is unaffected (n=5 wr=60% there). Re-enable from `BUY_NO_T_HIGH_BLOCK_SERIES = set()` if a future audit shows the regime changed. |
| **PER_SERIES_SIGMA_MULT** | per-station σ multiplier | 4 stations with HRRR MAE > 4°F per source_audit n=336: **KLAX=2.5** (HRRR MAE 5.9°F, n=12), **KPHX=2.0** (HRRR MAE 5.67°F, n=24), **KDEN=1.5** (HRRR MAE 4.47°F, n=18), **KLAS=1.5** (HRRR MAE 4.48°F, n=24). Multiplier ≈ empirical σ (≈ MAE × √(π/2)) divided by typical bot σ at that station. KLAX initially 1.5× on 2026-04-29; bumped to 2.5× same evening after audit on today's losing LAX-T54 BUY_YES revealed the 1.5× was still too tight at HRRR's 5.9°F MAE. Applied to all μ sources at the listed stations (over-widens NBP at KPHX/KLAS where NBP is narrow — conservative, won't cause losses). Borderline cities (ATL/NYC/DC/BOS, HRRR MAE 2.6-4.4°F) deferred until full empirical-σ-table backtest is built (see backlog). Defaults to 1.0 for cities not in the dict. |
| **PER_SERIES_D1_PRIMARY** | per-station d-1+ source override | KCHI / KOKC → "hrrr" (2026-04-29). d-1+ markets default to NBP μ; when a city is mapped here HRRR is used instead (NBP σ retained, with staleness inflation). Source-MAE audit 2026-04-29 (n=336): CHI HRRR 0.43°F vs NBP 2.33°F (5×, n=18); OKC HRRR 2.22°F vs NBP 3.50°F (1.6×, n=24). All other cities have NBP equal-or-better; they stay on NBP. |
| **PER_SERIES_D0_PRIMARY** | per-station d-0 source override | NYC / DC / BOS → "nbp" (2026-04-29 night). d-0 default is HRRR (freshest nowcast); these 3 stations override to NBP after candidate-log audit (n=46-58k cycle samples) showed HRRR runs systematically cool: NYC NBP 1.54°F vs HRRR 3.30°F (bias −3.30°F); DC 1.83 vs 2.61 (bias −1.65°F); BOS 1.17 vs 2.57 (bias −2.52°F). Today's NYC-B51.5 settled loss (HRRR μ=49.2 vs cli=52) was the trigger. ATL excluded (HRRR 1.48 vs NBP 1.61, HRRR slightly better); PHIL excluded (gap 0.30°F, marginal). Source tag `nbp_d0_override`; subject to NBP staleness σ-inflation since d-0 NBP can be 4-6h stale on late-evening entries. |
| `_obs_confirmed_alive` | rm vs bracket | **Bypass** (V2 port). When `running_min` has decisively settled the bracket (rm < floor − 3°F for BUY_NO B-bracket / T-high; rm ≤ cap − 1°F for BUY_YES T-low), skip all forecast-based gates, lower edge floor to 5%, multiply Kelly by 1.5×. **BUY_YES T-high case removed 2026-04-28** after KOKC-26APR28-T56 phantom (rm=60.08 at 16:04Z bypassed all gates; NBP next-day forecast 45°F → 21°F cold-front cooling expected before climate-day end). Unlike max-bot's V2 analog, the daily MIN can drop again in evening / pre-LST-midnight hours — `running_min` above floor right now is not a stable claim until the climate day ends, by which point the OBS WINNER LOCK has already pulled the market. |
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
- `trades_YYYY-MM-DD.jsonl` — every candidate + entry + exit, **date-rotated
  per UTC date** (added 2026-04-29 — V2 follows the same convention).
  Candidate records carry `blocked_by` + `block_reason` shadow fields:
  `null` means the candidate would have been entered, otherwise the gate
  name (`MIN_EDGE`, `MAX_EDGE`, `MP_RANGE`, `DIRECTIONAL_BUY_NO`,
  `DIRECTIONAL_BUY_YES`, `ABS_DIST`, `F2A`, `MSG`, `H_2_0`,
  `MAX_DISAGREEMENT`, `MU_VS_RM`, `SPREAD`, `OBS_CONFIRMED_LOSER`,
  `NO_ACTION`). Populated by `_evaluate_gates(opp)` at candidate-record
  time. Old `trades.jsonl` is kept as the historical archive for
  pre-rotation entries; runtime readers (`_compute_today_exposure`,
  `_reconcile_from_trades_log`) check both files for today's entries.
- `positions.json` — currently open positions
- `settlements.jsonl` — settled outcomes + P&L (NOT date-rotated; small
  enough to stay flat)
- `nbp_cache.json` — persisted NBP forecasts across restarts

## Tools

`/home/ubuntu/paper_min_bot/tools/`:
- `gate_audit.py` — back-test current gate stack against historical
  candidates. Reads the `blocked_by` field directly when present, falls
  back to recomputing via `paper_min_bot._evaluate_gates(opp)` for
  pre-shadow-logging records. Resolves outcomes via local `settlements.jsonl`,
  Kalshi `/portfolio/settlements`, and (with `--include-active`) Kalshi
  `/markets/{ticker}` endpoint. Argparse: `--gate <NAME>`, `--action`,
  `--days N`, `--include-active`, `--list-gates`.
- `source_audit.py` — per-source forecast accuracy vs actual CLI low.
  For every historical candidate that has settled, compares NBP / HRRR /
  NBM-OM / bot's chosen μ / blended (NBP+HRRR mean, triple-mean) against
  the actual CLI low. Reports MAE, bias, RMSE overall, by day-offset, and
  per city. Use when validating any model-source change (e.g. NWS
  integration, per-city primary-source override). Argparse: `--days N`,
  `--series KXLOWTXXX`, `--day-offset {-1,0,1,2,3}`.

`/home/ubuntu/paper_min_bot/MIN_BOT_BACKTEST_PLAYBOOK.md` — read this
**before proposing any filter / threshold change**. Codifies the 5-point
validation bar (n ≥ 20, lift ≥ +$30, robust lift ≥ +$15 LOO-1,
helps:hurts ≥ 4:2, articulable mechanism), audit workflow, and common
mistakes. V2 has a sister playbook at `~/obs-pipeline-bot/BACKTEST_PLAYBOOK.md`
with different thresholds (max-temp markets have different volume / variance
characteristics).

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
   from both today's `trades_YYYY-MM-DD.jsonl` and the legacy `trades.jsonl`
   archive (date-rotation deployed 2026-04-29; legacy file holds
   pre-rotation entries) and adds any whose ticker is missing from
   `_open_positions` *and* not already in `settlements.jsonl`. Closes the
   Kalshi `/portfolio/positions` API-lag window. Without this,
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

## Backlog

### Hold pending validation (post-2026-04-29 deploys)

These are decisions or watch-items that need fresh data from today's deploys
to settle. Re-run `tools/gate_audit.py` and `tools/source_audit.py` after
the Apr 28 + Apr 29 climate days settle (≈ 48-72h from 2026-04-29 06:00Z),
then revisit each.

- **Validate LAX BUY_NO T-high block** (commit `d39f23b`). Was deployed at
  n=3 wr=0% — below the n≥20 playbook bar but justified by the
  CLI-median + bias mechanism. After 48-72h, count `LAX_NO_THIGH` firings
  in `blocked_by`. If the gate is firing and the candidates it blocks would
  have been losers (resolve via `tools/gate_audit.py --gate LAX_NO_THIGH`),
  keep. If it's blocking winners, revert.
- **Validate CHI + OKC HRRR-primary on d-1+** (commit `b751940`). CHI
  deployed at n=18 (under n≥20 bar) on a 5× MAE gap; OKC at n=24. Re-run
  `tools/source_audit.py --series KXLOWTCHI` and `--series KXLOWTOKC` once
  fresh d-1+ candidates have settled. If MAE gap holds, consider
  expanding to NYC (HRRR 2.63°F vs NBP 3.33°F, n=18) and PHIL (HRRR 1.30°F
  vs NBP 5.00°F, n=6 currently — wait for n≥20).
- **Validate PRICE_ZONE removal** (commit `9dff496`). Removed at n=2 wr=0%.
  Watch for new entries with `yes_bid ∈ [30, 40]c` BUY_NO and check their
  outcomes. If the removal continues to recover winners, the call was right.
  If new BUY_NO at 65-67¢ in this band start losing, the V2 finding may
  apply on min after all.
- **MAX_DISAGREEMENT BUY_YES gate**: 1/1 historical winner blocked
  (KATL-T60 BUY_YES, +$0.63/c). Sample tiny but pattern is interesting —
  the gate fires on BUY_NO d-1+ via H_2.0 already; for BUY_YES it's only
  the broader 5°F MAX_DISAGREEMENT. Could be worth a `_nbp_consistent_with_recent_cli`-
  style bypass for BUY_YES specifically. Wait for n≥3 more BUY_YES
  candidates blocked by MAX_DISAGREEMENT before deciding.
- **F2A BUY_NO blocked-winner watch**: 1/1 historical winner
  (KMIN-26APR27-B46.5, +$0.33/c). Tiny sample. F2A is a V2 port; behavior
  on min-temp B-brackets may differ. Watch the count of F2A blocks via
  `tools/gate_audit.py --gate F2A`.
- **LAX threshold T-high BUY_YES audit**: bot has a real weakness at LAX
  53/54/55 boundary cluster — at-threshold trades with σ=1.4 produce
  overconfident BUY_YES that loses on rounding (LAX-T53 currently losing,
  LAX-T54 toss-up). The σ×1.5 mult deployed today partially addresses this,
  but a tighter rule (e.g., block BUY_YES T-high at LAX when |μ − floor|
  < σ × some factor) may be needed. Audit after ≥5 more LAX BUY_YES
  T-high settles.
- **Bracket-math integer-rounding correction (Phase 0a)**: V2's
  `CDF(cap+0.5) − CDF(floor−0.5)` formula. Min-bot currently uses raw
  `[floor, cap]` endpoints, under-stating B-bracket probability.
  Mathematically correct fix but ripples into every gate's mp threshold
  (which were calibrated against the buggy formula). Defer until paired
  with a re-calibration backtest of all the mp-threshold gates.

### Not yet implemented (features)

- **Per-station σ multiplier expansion**: source-MAE data hints at
  candidates beyond KLAX (OKC has same −3.5°F NBP cool-bias pattern; DC,
  DEN, NYC each have distinct +2 to +3°F warm bias). Each only has n=12-24
  trades right now. Defer per-city σ-mults until the playbook's n≥20 +
  consistent bias signal is established.
- **NWS gridpoint forecast integration**: V2's primary source. Researched
  2026-04-29; held because the source-MAE backtest showed blending hurts
  on min markets (NBP+HRRR mean MAE 2.18°F vs NBP alone 2.11°F; triple-mean
  2.41°F). Worth revisiting only if a single-source NWS test (NWS alone vs
  the current NBP / HRRR per-city split) shows a clean MAE win at the
  worst-MAE stations (LAX, MIN, DEN, PHIL).
- **HRRR overnight nowcast blending** (HRRR 0-18h min trajectory). Could
  pull HRRR's hourly forecast and find the min over the next 18h instead
  of just the daily-min field. Useful for d-0 evening entries when the
  morning pre-dawn low hasn't happened yet.
- **`seed_running_min_from_observations()`** wired into obs-pipeline
  startup. Currently rm only accumulates from new obs after bot restart;
  if it crashes mid-climate-day we lose the day's running min until the
  next obs comes in.
- **client_order_id-based idempotency on retry**. Lower priority — Kalshi's
  `order_id` already provides this; but client_order_id would let the bot
  recover from a half-acknowledged POST without a duplicate fill.
- **Web dashboard** (calibration curve, P&L by city, rolling Brier score).
  V2 has one at `~/.openclaw/workspace-main/sniper-us-poll/weather_dashboard.py`;
  porting the pattern to min would help with at-a-glance monitoring.
