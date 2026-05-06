# min_bot

Live trading bot for Kalshi low-temperature markets (`KXLOWT*`). Same 20
cities as V1/V2 but opposite settlement: daily minimum instead of maximum.

## Latest change (2026-05-06 morning) — Overfilter rollback: `MIN_MODEL_PROB` 0.15→0.05, `MAX_EDGE` 0.45→0.50, `COASTAL_TIGHT_FLOOR` disabled

A 5/4–5/6 audit (8 days of `bot_decisions.sqlite` rows + `trades_*.jsonl` candidates joined to `obs.sqlite running_min` for actual outcomes) found three gates were costing more than they were saving in the current regime. All three changed today.

**1. `MIN_MODEL_PROB` 0.15 → 0.05** — `MP_RANGE` blocked **8,022 candidates over 3 days**. Sample of 31 → ~28 would have **won**. The bot was killing its own deep-tail BUY_NO channel where the model's signal is strongest (mp 3–14% means model says 3–14% chance of being in bracket, and the model is right ~90% of the time). V2 uses 0.03; min_bot now at 0.05 for marginal conservatism. Note: F2A still blocks BUY_NO at mp < 0.05, so the deepest tail (mp 0–5%) remains gated — the change opens up the 5–15% mp band.

**2. `MAX_EDGE` 0.45 → 0.50** — At the 0.45 cap (since 4/29), 5/4–5/6 blocks were **21 winners / 9 losers** (70% would-win rate). The 4/29 rollback to 0.45 was based on an n=5 BUY_NO-bypass-loss observation; current regime has the model correctly identifying deep-tail BUY_NO winners at high apparent edge. V2 raised 0.45→0.50 on 5/3 (P3 of filter audit, +$418 era-wide, h:h 5:2) — this is a parity change. min_bot has the V2 downstream catchers (`MODEL_MARKET_DISAGREE` shipped 5/4, `OBS_CONFIRMED_LOSER` long-standing).

**3. `COASTAL_TIGHT_FLOOR` disabled via `_COASTAL_TIGHT_FLOOR_ENABLED = False`** — Original 5/3 backtest (n=9) showed h:h 6:0 (6 losses prevented). Live 5/4–5/6 result: 7 BUY_NO blocks, **6 won, 1 lost** — gate is regime-flipped. Cool-front shifted coastal lows out of brackets, breaking the marine-cold-bias premise. Predicate preserved (`COASTAL_TIGHT_FLOOR_STATIONS`, `COASTAL_TIGHT_FLOOR_MIN_GAP_F`) for fresh sliding-window re-enable. Re-evaluate ~2026-05-13.

**Why this matters:** entry rate had dropped from ~10/day (4/29–5/3) to 4–7/day (5/4–5/6) with PnL flipping from +$15–50/day realized to −$3/day. The combination of these three gates was suppressing the bot's profitable BUY_NO B-bracket channel (era-wide n=28, W=25, L=3, +$164). Estimated overfilter cost: $200–500/day in missed profit.

### Tests
- `tests/test_paper_min.py` — 384 OK, 1 skip (`test_low_mp_passes_when_nbp_consistent`: bypass path obsolete when `MIN_MODEL_PROB == F2A_PROB_LO`)
- `tests/test_model_market_disagree.py` — `TestCoastalTightFloorBothCallSites` `setUp/tearDown` now force-enables `_COASTAL_TIGHT_FLOOR_ENABLED` to keep validating wiring while flag is `False` in production
- 5 pre-existing ladder-test failures (`test_ladder_chase_20260503`, `test_ladder_no_fill_continue_20260504`) are unchanged and unrelated

Backups: `paper_min_bot.py.bak.pre_overfilter_20260506-065313`, `tests/test_paper_min.py.bak.pre_overfilter_20260506-065313`, `README.md.bak.pre_overfilter_20260506-065313`.

Re-evaluate ~2026-05-20.

## Previous change (2026-05-04 late afternoon) — `PER_SERIES_D{0,1}_PRIMARY` re-calibrated on 14d audit

Recalibrated both per-city forecast-source override dicts based on a fresh 14d audit (`/tmp/inv2_audit_v2.py` on VPS — joined min_bot's candidate-log per-source forecasts with `obs.sqlite running_min` for actuals; n=4-6 settled cells per (city, days_out)).

**`PER_SERIES_D0_PRIMARY` — 4 cells removed** where NBP became materially worse than HRRR over the 14d window:

| Series | NBP MAE | HRRR MAE | Gap | Action |
|---|---:|---:|---:|---|
| `KXLOWTNYC` | 1.97 | **0.88** | +1.09°F | REMOVED → HRRR default |
| `KXLOWTDC` | 2.03 | **1.66** | +0.37°F | REMOVED (marginal) → HRRR default |
| `KXLOWTMIA` | 1.92 | **0.53** | +1.39°F | REMOVED → HRRR default (biggest flip) |
| `KXLOWTPHX` | 2.40 | **1.52** | +0.88°F | REMOVED → HRRR default |

Surviving NBP-better cohort (gap −0.34 to −0.99°F): `KXLOWTBOS`, `KXLOWTLAS`, `KXLOWTLAX`, `KXLOWTPHIL`, `KXLOWTSEA`, `KXLOWTSFO`. **Total D0 overrides: 10 → 6.**

The MIA flip is the headline. The 2026-05-01 deploy added KXLOWTMIA → NBP based on a single MIA-26MAY01 hard_stop where NBP would have been right; 14d aggregate now shows the opposite — NBP runs warm-biased on KMIA at d-0 (bias +1.92°F) while HRRR is essentially unbiased (-0.37°F). Today's MIA-26MAY04 hard_stop where NBP=74°F vs actual rm=71.6°F (HRRR=72°F was 0.4°F off) is consistent with the new audit, not the old one.

**`PER_SERIES_D1_PRIMARY` — 3 cells added** for gulf-coast cities where HRRR is materially better at d-1+:

| Series | NBP d-1 MAE | HRRR d-1 MAE | Gap | Action |
|---|---:|---:|---:|---|
| `KXLOWTHOU` | 4.35 | **1.58** | +2.77°F | ADDED → HRRR |
| `KXLOWTNOLA` | 4.05 | **0.55** | +3.50°F | ADDED → HRRR (biggest gap) |
| `KXLOWTSATX` | 5.10 | **1.85** | +3.25°F | ADDED → HRRR |

NBP runs 4-5°F warm on these gulf-coast cities at d-1 vs actual rm. HRRR is 2-7× more accurate. **Total D1 overrides: 2 → 5.**

**Why the 2026-04-29 audit got these wrong:** the original audit used n~31k cycle samples per cell over a 6-7 day window of intra-day forecast cycle data. The 14d audit uses fewer points but each one is the LATEST pre-settle forecast vs the actual settled rm. The 14d view is a more direct measure of "what μ would I have used at trade time vs what actually happened." Different question, different answer; this view is the one that matters for trade sizing.

**Tests updated:**
- `TestPerCityD0PrimarySource` rewritten: 4 new `_REMOVED_` tests pin NYC/DC/MIA/PHX out of the dict; count assertion bumped 10 → 6.
- `TestPerCityD1PrimarySource` extended: 3 new HOU/NOLA/SATX assertions; count assertion 2 → 5.
- Disjoint-dicts invariant test still passes.

Full suite: **376 passed** (was 374, +2 net new tests). Zero regressions.

**Forward audit:** re-run `/tmp/inv2_audit_v2.py` ~2026-05-18 (≥21d cumulative window). If any cell flips again, this is a live distribution that needs continual recalibration — at which point a per-station rolling-bias-correction layer becomes more attractive than a static dict.

**No behavior change to logic** — only the routing dicts moved. σ-aware Kelly, COASTAL_TIGHT_FLOOR (verified neutral, NOT bumped), MARKET_STOP, ladder, hard-stop disable all unchanged.

**2026-05-05 follow-up — MIA added to D1 HRRR-PRIMARY (post-loss audit):**

KMIA-26MAY04-B71.5 BUY_NO −$8.91 loss (NBP=74°F vs actual rm=71°F) prompted a 5-day MIA d-1 audit. Per-source error stats:

| | NBP | HRRR | NBM_OM |
|---|---:|---:|---:|
| MAE | 2.12°F | 2.10°F | **1.64°F** |
| bias | **+2.12°F warm** | −1.78°F cold | −1.32°F cold |
| warm errors >+1°F | **4/5 days** | 0/5 | 0/5 |

MAE is essentially tied between NBP and HRRR — the case is bias DIRECTION, not magnitude. NBP error is warm-skewed and asymmetric:

- NBP errs:  `0.00, +0.40, +1.80, +5.40, +3.00`  → 4/5 warm, 2 ≥ +3°F
- HRRR errs: `−3.00, −3.70, −3.00, +0.70, +0.10`  → 0/5 warm > +1°F

For BUY_NO trades, only WARM-side forecast error is the failure mode (overstated μ → false confidence min won't be in bracket → actual settles into bracket). HRRR's cold bias does not produce losing BUY_NO entries; its trades simply look less attractive. KMIA-26MAY04-B71.5 specifically: NBP=74.0, HRRR=71.1, actual=71.0 → HRRR-PRIMARY would have placed μ IN bracket → no BUY_NO generated. Adding KMIA matches the gulf-coast cohort pattern. **Total D1 overrides: 5 → 6.**

`COASTAL_TIGHT_FLOOR` (wired correctly via `d4b3e5d` today) is the safety-net gate for coastal BUY_NO; this per-series cohort change is the upstream prevention.

Full suite: **574 passed / 5 fail** (the 5 fails are pre-existing ladder tests unrelated to forecast routing).

---

## Previous change (2026-05-04 afternoon) — ladder bumped to 5 + extended to BUY_YES + value-dead MARKET_STOP

Three companion changes addressing thin-orderbook fill problems and adding a final safety net after the morning's hard-stop disable.

**1. `LADDER_MAX_RETRIES`: 3 → 5.** PHX/AUS/SEA orderbooks were too thin for 3 walks to clear meaningful intended counts. Real audit on today's open positions: PHX-MAY04-B64.5 filled 1/34 (3%), SEA-MAY04-B53.5 filled 10/62 (16%), AUS-MAY04-B55.5 filled 1/9 (11%). Five walks gives the bot more headroom before edge drops below `MIN_EDGE` and ladder gives up.

**2. Ladder extended from BUY_NO-only to BUY_YES.** AUS-MAY04 BUY_YES showed the same 89% orphan rate as BUY_NO cases, but the 2026-05-03 ladder deploy was BUY_NO-only. Now action-aware: BUY_NO chases `no_ask_dollars` against `MAX_BET_USD` ($30) cap with edge `1 − mp_yes − price`; BUY_YES chases `yes_ask_dollars` against `MAX_BET_BUY_YES_USD` ($5) cap with edge `mp_yes − price`. Add-ons still iterate via the existing scan loop, unchanged.

**3. New `MARKET_STOP_BID_CEIL_C = 1`.** Hard-stop is sentinel-disabled; this is the final safety net. When the position's bid (no_bid for BUY_NO, yes_bid for BUY_YES) is ≤ 1¢, market consensus is ~99% the other side wins — the position is essentially worthless. Exits with `reason="market_stop"`. The obs-winner override still fires FIRST and blocks this exit on positions where rm confirms a recovery path (e.g., BUY_NO with rm < floor − 1°F), so genuine recovery cases ride to settlement.

```python
LADDER_MAX_RETRIES = 5
MARKET_STOP_BID_CEIL_C = 1   # exit if current bid ≤ 1¢ AND no obs-winner override
```

**Why MARKET_STOP at 1¢ specifically:** anything wider competes with the philosophy of the morning's hard-stop disable (let positions ride to settlement). 1¢ is the bid floor on Kalshi — a position trading there has essentially no upside ceiling that justifies tying up capital, and the obs-winner override means real recovery cases never see this gate. Re-evaluate threshold after 14d forward audit; widen if survivor-bias data shows positions recovering from 1¢, tighten if 1¢ exits are wrong too often.

**Tests added: 9** in two new classes — `TestMarketStopValueDead` (5: fires at 1c BUY_NO, fires at 1c BUY_YES, no fire at 2c, obs-winner override blocks, constant pin) + `TestLadderConfig` (4: retries=5, action gate accepts both, action-specific ask field, action-specific cap). 5 pre-existing hard-stop tests already updated this morning. Full suite: **374 passed** (was 365, +9 new). Zero regressions.

**Live verification post-deploy:** bot restarted, cycling clean. Forward audit hook: position records will show `_filled_count` vs `_intended_count` per ticker — compare to before-fix orphan rate; expect material improvement on PHX/AUS/SEA-class thin books.

---

## Previous change (2026-05-04 morning) — MTM hard-stop DISABLED (sentinel)

`HARD_STOP_BRACKET_LOSS_PCT` and `HARD_STOP_TAIL_LOSS_PCT` set to 999.0 (sentinel disable, mirrors V1's `SESSION_DRAWDOWN_LIMIT = -9999` pattern from 2026-05-03). Positions now hold to settlement regardless of mid-day MTM.

**Trigger:** `KXLOWTMIA-26MAY04-B71.5` BUY_NO hard-stopped today at $0.04 with rm=71.6 (PNL −$8.91). Per Chris's call, the position would have won if held; the MTM-cut surrendered a trade the obs evolution could still rescue. Pattern: when min-temp BUY_NO has rm crossing into the bracket, market price collapses → hard-stop fires → bot exits → but `running_min` only decreases (it's a daily min), so the trade can still win if rm continues dropping below floor. The hard-stop short-circuits that recovery path.

**What still works:**
- `_check_obs_confirmed_loser` (entry-side blocker for `rm > cap + 1`) unchanged
- `_check_position_obs_winning` override unchanged (positions where obs confirms a guaranteed win still hold without ambiguity)
- Trailing TP / settlement / time-based exits unchanged
- σ-aware Kelly sizing unchanged — bounds the per-trade downside

**Behavior with sentinels:** `loss_pct >= 999.0` is impossible (loss_pct ≤ 1.0 since price ∈ [0, 1]), so the hard-stop branch in `check_open_positions_for_exit` becomes dead code without removing the surrounding logic. Re-enable: revert both constants to their prior values (0.80 / 0.70).

**Tests updated:** 5 prior tests that asserted "hard-stop fires on X" flipped to "hold to settlement"; new `test_hard_stop_constants_are_sentinel_values` pins the disable. Full min_bot suite: **365 passed** (was 364, +1 sentinel test). Zero regressions.

**Forward audit:** track P&L over a 14-day forward window. If the disable produces net-negative results vs prior 80%/70% MTM cut on the same trade pool, re-enable with a tightened threshold (e.g., 95%). Comparison data: pre-disable hard_stop trades + their settlement outcomes are in `data/trades_*.jsonl` with `reason=hard_stop`.

---

## Previous change (2026-05-04 early morning) — NBP staleness alert is now cycle-aware

`_nbp_staleness_alert` had a flat `age_h > 3.0` trigger that fired halfway through every normal 6h NBP inter-cycle gap. NBP publishes 01/07/13/19 UTC with ~70 min publish latency, so between cycles the cache is correctly 4-7h old; the old threshold produced one false-positive Discord ping every ~6h regardless of whether the HEAD-poll was actually missing publications.

**Fix:** new `_nbp_alert_overdue(cycle_dt, age_h)` helper. Alert fires only when `now > cycle_dt + 6h + NBP_PUBLISH_LATENCY_MIN + 30min grace` — i.e., when the next cycle is past its expected landing time (~7h40m post the cached cycle's nominal hour). Falls back to plain `age_h > 6.0` when `cycle_dt` is unknown (cold start). The `NBP_HARD_STALE_SEC = 8h` block remains as the final safety net.

**Why now:** repeated `:warning: STALE CACHE FALLBACK NBP — cache age=4.0h ...` Discord pings on 2026-05-04 between the 01z fetch (02:10 UTC) and the next-expected 07z cycle (~08:10 UTC). Confirmed not a real failure: `https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20260504/07/text/blend_nbptx.t07z` returned 404 at the time of the alert — NOAA simply hadn't published yet.

**Tests added: 7** in `TestNbpStaleAlertCycleAware` (`tests/test_paper_min.py`) covering normal-gap quiet behavior, exact-landing-boundary behavior, alert-when-overdue, cold-start fallback, and the `NBP_PUBLISH_LATENCY_MIN` constant being respected. Full suite: 364 paper_min tests + 18 om_dynamic_ttl tests pass; zero regressions.

**No behavior change to trading.** σ inflation logic for stale NBP is unchanged (still ramps `+5%/h after 1h, capped at +30% at 7h`). Only the Discord alert threshold moved.

---

## Previous change (2026-05-03 late evening) — persist `_last_rm_seen` across restarts

`_check_new_low_alerts()` polls obs-pipeline's `running_min` once per scan and emits a Discord `❄️ NEW LOW` when any of the 20 stations' rm steps down. State was an in-memory dict only, so any restart wiped the baseline. The first cycle after a restart silently established a fresh baseline at whatever rm was current — including any corrupt upstream value. (The 2026-05-03 obs-pipeline `PK WND` false-match wrote 27.14°F to KMDW running_min; without persistence, a restart in that window would have absorbed 27.14 as the new baseline and never alerted on the regression.)

**Fix:** `_last_rm_seen` persists to `data/last_rm_seen.json` (atomic temp-file replace) on every state mutation in `_check_new_low_alerts`. Loaded at module import via `_load_last_rm_seen()`; per-entry parse failures are skipped so one malformed row can't drop the rest. Soft cache — missing or corrupt file falls back to empty dict, never crashes the bot.



### Tests

6 new tests in `TestLastRmSeenPersistence` (`tests/test_paper_min.py`):

- `test_load_returns_empty_when_file_missing` — fresh deploy
- `test_save_then_load_roundtrip` — round-trip preservation of `(cd, rm)` tuples
- `test_load_tolerates_corrupt_json` — truncated JSON returns `{}`, no crash
- `test_load_skips_invalid_entries` — mixed valid/invalid file keeps the valid rows
- `test_check_new_low_alerts_persists_baseline_on_first_sighting` — first-cycle write
- `test_restart_simulation_persisted_baseline_blocks_silent_corruption` — explicit guard against the obs-pipeline `PK WND` regression class: pre-restart 44.6°F → restart → injected corrupt 27.14°F → alert MUST fire (regression of in-memory-only behavior would silently absorb 27.14 as the new baseline)

The existing `TestNewLowDiscordAlerts.setUp` was extended to mock `_save_last_rm_seen` so the existing 6 tests don't write a stale cache file into the production data dir during test runs.

Live verification post-deploy: `data/last_rm_seen.json` written on the first scan cycle, all 20 stations populated. Includes the corrected KMDW=44.6 / KPHX=77.0 / KOKC=46.0 baselines from the obs-pipeline running_min repair. `358 tests passing` (357 `test_paper_min.py` + 18 `test_om_dynamic_ttl.py` — full deselected count omitted).


## Latest change (2026-05-02 late afternoon) — disable mp-range bypass on coastal stations

Replays of every historical bypass-fired entry (n=23, 19 settled) showed the `_nbp_consistent_with_recent_cli` mp-range bypass is **net negative overall** (−$51.42, 10W:9L) and the loss concentrates entirely on coastal/marine-layer stations:

| Cohort | W:L | Net PnL | Win rate |
|---|---|---|---|
| **Coastal** (KLAX, KSFO, KSEA, KMIA, KHOU, KMSY, KNYC, KPHL, KBOS) | 3 : 6 | **−$91.87** | 33% |
| Inland (everything else) | 7 : 3 | **+$40.45** | 70% |

Per-station scoreboard:

```
0W / 2L  -$53.16  KLAX   ← worst offender; 2 consecutive B58.5 hard-stops
0W / 1L  -$29.44  KHOU
0W / 1L  -$24.50  KSFO
0W / 1L  -$ 7.68  KATL
0W / 1L  -$ 1.38  KMIA
0W / 1L  -$ 1.06  KNYC
1W / 2L  +$ 0.18  KDEN  (basically flat)
1W / 0L  +$ 0.39  KSEA
2W / 0L  +$15.64  KPHX
2W / 0L  +$16.76  KDFW
2W / 0L  +$17.28  KPHL
... (all-winner inland tail)
```

**Mechanism**: the bypass condition `NBP μ within ±2°F of last-7d CLI range` is meaningless on stations whose CLI range spans 8-12°F. The buffer expands the consistency window to 12-16°F — virtually any forecast lands inside it, turning the gate into a no-op rubber stamp. Inland stations with tight 4-6°F ranges retain a meaningful test.

**Fix**: new `COASTAL_NO_MPBYPASS_STATIONS` constant (the 9 stations above). `_nbp_consistent_with_recent_cli` returns `False` early when station ∈ that set; inland stations keep the bypass as-is. The original 2026-04-29 backtest justification (3/3 historical wins) was an n=3 sample that didn't extrapolate.

Counterfactual on the 19-trade sample: removing bypass on coastal would skip the 9 coastal trades (avoid −$91.87 in losses, give up +$17.67 in wins) → **+$74 net improvement** without affecting inland's +$40.

Tests: 4 new (`test_coastal_constant_membership`, `test_coastal_station_bypass_disabled_KLAX`, `test_coastal_station_bypass_disabled_full_set`, `test_inland_station_bypass_still_active`); 320/323 pass on python3.12 (3 remaining are pre-existing unrelated bankroll/sigma_wider failures).

See `memory/project_min_bot_mp_range_bypass_coastal_skip_20260502.md`.

## Earlier 2026-05-02 afternoon — close BUY_YES B-bracket "low locked above" gap

Added a new branch in `_check_obs_confirmed_loser` for **BUY_YES + B-bracket + rm > cap + 1.0 + past local low-lock**. Previously the function only checked `rm < floor` for that combo; the symmetric `rm > cap` case slipped through.

**Why:** PHIL-26MAY02-B49.5 BUY_YES this morning (μ=49, bracket [49,50]) entered at 6:45 AM EDT with rm=51.8 — low had already locked 1.8°F above cap=50, so BUY_YES was structurally guaranteed to lose. The bot didn't block because the obs-loser check for BUY_YES B-bracket only handled rm-below-floor. The $5 cap (shipped earlier today) limited the damage but the underlying bug let the trade through.

**The fix:**
```python
elif action == "BUY_YES":
    if floor is not None and cap is not None:
        if rm_f < float(floor):
            return True
        # NEW: low locked above bracket → YES guaranteed loss
        tz = opp_or_pos.get("tz", "America/New_York")
        try:
            _now_local = datetime.now(ZoneInfo(tz))
            _past_low_lock = _now_local.hour >= 6
        except Exception:
            _past_low_lock = False
        if rm_f > float(cap) + 1.0 and _past_low_lock:
            return True
```

**Threshold rationale:**
- **`rm > cap + 1.0`**: CLI integer rounding requires continuous rm ≥ cap+0.5 to round to cap+1 (definitively outside bracket); +1.0 adds half a degree for ASOS-vs-METAR obs noise. PHIL: rm=51.8 vs cap+1=51 → blocks.
- **`hour >= 6`**: lows typically lock 30-60 min before sunrise (predawn radiative cooling). 6 AM is past low-lock across all US cities/seasons. Tighter than the existing `_is_post_sunrise` (hour ≥ 8 blanket) which would have missed PHIL at 6:45 AM EDT. False-positive cost is bounded by the $5 BUY_YES cap.

**Tests:** 15 new in `tests/test_buy_yes_b_bracket_obs_loser.py` — covers PHIL exact pattern at 6:45 AM and 7:17 AM EDT, hour-cutoff sweep (3/5/6 AM), buffer boundary (cap+0.4 / cap+1.0 / cap+1.1), regression guards (rm-below-floor still fires, BUY_NO unaffected, T-high/T-low branches unchanged, rm=None handled).

Suite: **404 passing**, 3 unrelated pre-existing failures.

Daemon restart required.

---

## 2026-05-02 mid-day — extend $5 BUY_YES cap to ALL BUY_YES (was tail-only)

`MAX_BET_BUY_YES_TAIL_USD` renamed to `MAX_BET_BUY_YES_USD` and the cap now applies to **all BUY_YES entries**, including B-brackets. Previously only T-high / T-low were capped at $5; B-bracket BUY_YES used `MAX_BET_USD = $30`.

**Why:** the bracket-math fix shipped earlier today made inside-bracket BUY_YES trades viable for the first time. PHIL-26MAY02-B49.5 fired this morning at μ=49 (NBP-d0, σ=1.0), bracket [49,50] — corrected mp = 62%, edge = +42%. Kelly wanted ~$30 worth (≈150 contracts at 20¢); only got 2 contracts ($0.38 total) due to no liquidity, but the next time a similar setup hits a liquid market the cap is the only thing standing between a small bet and a $30 position on a forecast that's 2-3°F off the actual locked low.

**The asymmetry:** BUY_YES wins are bounded by `(1 − price) × count` — for a 20¢ entry, max payout is 80¢/contract. BUY_YES losses are the full position cost. Capping bets at $5 limits per-trade max loss to ~$5 across all bracket types.

**Code change:**
```python
# Before (tail-only):
_bet_is_tail = (floor is not None) ^ (cap is not None)
if _bet_action == "BUY_YES" and _bet_is_tail:
    _effective_max_bet = MAX_BET_BUY_YES_TAIL_USD

# After (all BUY_YES):
if _bet_action == "BUY_YES":
    _effective_max_bet = MAX_BET_BUY_YES_USD
```

Tests: `TestYesTailMaxBetCap` updated — `test_buy_yes_b_bracket_uses_full_max_bet` was an explicit assertion of the OLD behavior (B-bracket NOT capped); flipped to `test_buy_yes_b_bracket_capped_at_5_dollars` to enforce the NEW cap. `test_constant_is_5_dollars` now checks the renamed constant + asserts the old name is gone (`hasattr` check). Suite: 389 pass, 3 unrelated pre-existing failures.

Daemon restart required.

---

## 2026-05-02 — BRACKET MATH FIX (V1/V2 port from 2026-04-22)

Ported the 2026-04-22 bracket-math fix from V1 + V2 (already shipped there months ago). `calc_bracket_probability_min` now widens B-bracket integration limits by ±0.5°F to match Kalshi's integer-CLI settlement.

**The math:** Kalshi settles on integer °F CLI. A B-bracket [floor, cap] wins YES when CLI ∈ {floor, …, cap}, which is continuous low ∈ [floor−0.5, cap+0.5]. The previous formula computed P(continuous ∈ [floor, cap]) — too narrow by 1°F (0.5°F on each side).

**V1/V2 evidence:** Brier-score backtest on n=398 settled max-temp brackets showed 0.250 → 0.228 with this fix. min_bot was missing the port.

**Why it was load-bearing:** at boundary forecasts (μ within 1°F of bracket edge with σ ≈ 2-3°F), the old formula produced mp ≈ 10-15%, which bypassed `MIN_MODEL_PROB` via the NBP-CLI consistency bypass and then sailed through `DIRECTIONAL_BUY_NO_MAX_MP=0.20` (because old mp was way below 20%). With corrected mp ≈ 22-30% in those same cases, the `DIRECTIONAL` gate catches them directly — bypass mechanism never invoked.

**Impact on the 5 catastrophic May 1 losses + 4 in-sample winners** (verified against fresh code):

| ticker | μ | σ | bracket | new mp | DIRECTIONAL gate | actual outcome |
|---|---|---|---|---|---|---|
| LAX-MAY01 | 57.0 | 3.00 | [58,59] | **23.1%** | **BLOCK** | $-29.16 hard-stop saved |
| SFO-MAY01 | 53.0 | 2.40 | [54,55] | **26.9%** | **BLOCK** | $-24.50 hard-stop saved |
| ATL-APR29 | 61.3 | 2.50 | [63,64] | **21.5%** | **BLOCK** | $-7.68 saved |
| NYC-APR29 | 50.0 | 2.50 | [51,52] | **26.2%** | **BLOCK** | $-1.06 saved |
| DEN-APR30 | 37.6 | 2.50 | [38,39] | **29.2%** | **BLOCK** | $-0.63 saved |
| PHX-APR29 | 60.4 | 3.72 | [62,63] | 18.1% | allow | $+10.20 preserved |
| AUS-APR30 | 66.0 | 3.30 | [64,65] | 21.5% | BLOCK | $+1.36 forfeit (lucky bet — forecast direction wrong) |
| MIN-MAY01 | 37.0 | 3.06 | [37,38] | 25.3% | BLOCK | $+14.19 forfeit (μ INSIDE bracket — never should have fired) |
| DEN-MAY01 | 32.0 | 6.00 | [33,34] | 12.8% | allow | $+2.97 preserved |

In-sample lift on the 22-trade BUY_NO pool: **+$47.48** (saves $63.03, forfeits $15.55).

**T-tails are unaffected** — `parse_market_bracket` already pre-buffers them (`cap=val−0.5` for T-low, `floor=val+0.5` for T-high). The fix detects B-brackets via `floor is not None and cap is not None` and skips the buffer for half-integer T-tail bounds (avoids double-buffering).

**Blast radius:** entry-side only. `calc_bracket_probability_min` is called in exactly one place (`find_opportunities`), and the result is stored as `opp["model_prob"]` — never recomputed. Hard-stop, settlement, OBS_CONFIRMED logic uses `floor`/`cap`/`running_min` directly and is unaffected.

Tests: `tests/test_bracket_math_fix.py` (18) — covers all 5 historical losses, B-bracket buffer monotonicity, T-tail no-double-buffer guards, regression guards. Three pre-existing tests in `test_paper_min.py` (`test_gaussian_without_truncation`, `test_post_sunrise_lock_collapses_to_rm`, `test_post_sunrise_sigma_widened_to_1F`) had hard-coded mp values from the old math; updated to reflect the new (correct) values.

**To activate:** daemon must be restarted — Python doesn't hot-reload `.py`. Until restart, entries continue to use the buggy mp.

---

## Earlier 2026-05-01 late night — `primary_outlier_diff_at_entry` shadow log

Added one diagnostic field to every entry log record. **No behavior change.** The bot still trades exactly as before — this is forward-data collection for a possible `SKIP_PRIMARY_OUTLIER` filter at ~2.0°F.

**The field:**

| Field | Source | Purpose |
|---|---|---|
| `primary_outlier_diff_at_entry` | `_compute_primary_outlier_diff(opp)` | `\|mu_primary − mean(other available source mus)\|`, in °F. High = bot's chosen forecast disagrees with the cluster. |

**Why this was added:** Two of today's three big losses (HOU 26APR30-B68.5 = $-29.44, MIA 26MAY01-B71.5 = $-23.82) shared a "primary source 2-3°F off the cluster" signature. The Apr 29 - May 1 backtest (n=30 BUY_NO entries) showed:

| ticker | primary | others | diff |
|---|---|---|---|
| HOU-B68.5 | HRRR=69.4 | NBP=74.0, NBM=69.5 | **2.35°F** → loss |
| MIA-B71.5 | NBP=72.0 | HRRR=69.6, NBM=69.6 | **2.40°F** → loss |
| PHX-APR29-B62.5 | NBP=64.0 | HRRR=60.4, NBM=60.4 | **3.60°F** → +$10.20 win (false positive) |
| SFO-MAY01-B54.5 | NBP=53.0 | HRRR=51.3, NBM=52.1 | 1.30°F (not caught) |

A 2.0°F threshold catches HOU + MIA but forfeits PHX. Helps:hurts is **1:1 settled** with one structural false-positive class (sources disagree but both directions independently support BUY_NO). Insufficient to deploy as a gate — only 2 settled saves to fit against. Shadow-log first, re-evaluate at ~60 BUY_NO trades (~mid-May 2026).

**Reference scripts** (kept in `/tmp/` on EC2 for the next backtest run):
- `/tmp/conf_kelly_backtest.py` — earlier confidence-Kelly framework (rejected: −$11 lift)
- `/tmp/primary_outlier_backtest.py` — this filter's threshold sweep

Tests: `tests/test_primary_outlier_diff.py` (15) — 3 source-grep + 12 unit tests covering each `mu_source` label and edge cases (missing sources, unknown source, rounding). Full suite: **352 passed (337 baseline + 15 new), no regressions**.

**To activate:** the daemon must be restarted — Python doesn't hot-reload `.py` edits. Until restart, entries are still being written without the field.

---

## Earlier 2026-05-01 — entry-awareness fields on settlements

Added V2-parity entry-time context to every trade + settlement record so
filter audits (catching-knife, late-day, per-station bias) can run against
min_bot's settled pool. Without these fields, the V2 mirror filters cannot
be backtested — the per-trade context is missing.

**New fields** (additive only — every existing field still present):

| Field | Source | Purpose |
|---|---|---|
| `entry_local_hour` | computed at entry from station tz | local hour 0-23 (catching-knife filter requires this) |
| `entry_local_dow` | computed | local day-of-week (Mon/Tue/...) |
| `entry_local_ts` | computed | full local ISO timestamp (grep-friendly) |
| `entry_hours_to_sunrise` | `_hours_to_sunrise(tz, lat, lon)` | hours to next sunrise — analog of V2's `peak_hour` for min temps |
| `entry_tz` | from opp dict | station timezone string |
| `entry_yes_bid_cents` / `_ask_cents` | from market quote at entry | raw market bid/ask (for catching-knife: market's view at entry) |
| `entry_no_bid_cents` / `_ask_cents` | from market quote | NO side raw quotes |
| `entry_spread_cents` | computed: yes_ask − yes_bid | liquidity proxy |
| `entry_volume` | from market quote | 24h volume at entry |
| `mu_nbp_at_entry`, `sigma_nbp_at_entry` | from forecast cluster | NBP forecast detail (the d-1+ default source) |
| `mu_nbm_om_at_entry`, `mu_hrrr_at_entry` | from forecast cluster | other ensemble members at entry |
| `disagreement_at_entry` | from opp | max pairwise disagreement (forecast cluster spread) |
| `post_sunrise_lock_at_entry` | from opp | whether sigma was collapsed at entry |
| `is_today_at_entry` | from opp | d-0 vs d-1 marker |
| `yes_ask_frac_at_entry` / `no_ask_frac_at_entry` | from opp | edge breakdown components |
| `edge` | from opp | bot's computed edge (was already in trade_record, now also in settlement) |

**Why this was needed:** the V2 catching-knife audit on min_bot's settled pool produced a "0 fires" or "wrong direction" verdict because we couldn't compute `local_hour - peak_hour` proximity — min_bot's settlements lacked the time-of-day field. Adding these closes the gap so future filter proposals can be validated against real settled outcomes.

**Backward compatibility:** all existing fields remain. Backtest tools and downstream readers continue to work unchanged. New fields populate on entries written 2026-05-01 evening onwards; older settlements have these as `null` / `None`.

Tests: `tests/test_entry_awareness.py` (7) — verify trade_record + both settlement paths (kalshi-fallback + obs-pipeline-CLI) include all 20 new fields, that `ZoneInfo` is used to compute local_hour, and that no original fields were removed. Full min_bot suite: **337 passed (330 baseline + 7 new), no regressions**.

---


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
- **NBP refresh — HEAD-poll trigger + background poller** (2026-04-30).
  NBP cycles run 01/07/13/19 UTC; S3 publish typically 75–90 min after
  cycle nominal time. A dedicated `nbp-poller` daemon thread HEAD-probes
  the next-expected cycle URL on a 5s tick during the active publish
  window (cycle+60 to +240min), 60s tick otherwise. Full GET (33MB) fires
  only on HEAD 200, behind a non-blocking `_nbp_refresh_lock` so the
  poller and the scan loop can't double-fetch. Detection-to-usable
  latency ≈ **5–8s** after S3 publish (vs ~3.5h under the prior age>6h
  trigger; vs ~30s if we relied on the scan-loop probe alone). Cache
  pointer (`_nbp_cache_cycle_dt`) is disk-persisted so restarts don't
  reset the schedule. Hard-stale safety net (>8h) covers stuck pointer /
  S3 outage. The scan-loop also calls `_nbp_next_cycle_available()` as a
  belt-and-suspenders fallback in case the daemon thread dies.
- **Model**: truncated Gaussian bounded above by `running_min + 1.0°F` (the
  +1°F is the ASOS-vs-CLI buffer; CLI rounds to integer and our 5-min obs
  may be 0.5°F off). σ inflation pipeline (in order, all multiplicative):
  disagreement-based (1×→1.5× as NBP/HRRR/NBM diverge 2°F→5°F) → NBP
  staleness (1×→1.30× as NBP cache ages 1h→7h, d-1+ only; **Discord alert
  raised from >1h to >3h on 2026-04-30** — HEAD-poll refresh now lands a
  fresh cycle sub-2h after publish, so >3h implies a real fetch failure
  worth pinging) → per-station
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
3. `execute_opportunity()` runs the safety gate stack (below), checks
   per-ticker eligibility (NEW: add-on path), checks cycle / daily /
   per-event budgets, sizes via Kelly.
4. `place_kalshi_order()` POSTs a limit buy at the ask price.
5. `wait_for_fill()` polls the `kalshi_ws` fill cache for up to 5 s, falls
   back to REST `/portfolio/orders/{id}` (authoritative).
6. Partial / no-fill: cancel any resting remainder. Record actual `filled`
   count to today's `data/trades_YYYY-MM-DD.jsonl` and emit a Discord
   notification. The position is left **addon-eligible** — the gap
   `_intended_count − count` will be retried on subsequent scans.
7. `check_settlements()` walks open positions; on CLI publish, computes P&L
   and emits a Discord settlement notification.

### Add-on path (2026-04-30 PM)

V2-style. Once a position partial-fills, the bot retries on each subsequent
scan to fill the remaining gap up to the original Kelly-determined intended
count, capped by remaining `MAX_BET_USD` budget. The full gate stack runs
again on each retry — if model edge has eroded the add-on is suppressed.

State on each open position:
- `_intended_count` — Kelly target captured at first entry.
- `_filled_count` — cumulative filled count (= `count` field).
- `_n_orders` — incremented per addon (1 = first entry only).
- `entry_price` — weighted average across all fills (used by hard-stop loss%).

Add-on is **blocked** when any of:
- position settled, or `exited_ts` set (full hard-stop fill), or
- `_partial_exit_count > 0` (don't average back into a stopped position), or
- already at `_intended_count` (full target hit), or
- already at `MAX_BET_USD` cost cap.

Per-event cap and cycle-new-cap are bypassed for add-ons (the existing
position already counts; an add-on doesn't open a new slot). Daily $ cap
still applies — add-on dollars come from the same wallet.

Each add-on writes a separate `entry`-kind record to `trades.jsonl` with
`_is_addon=True`, while the in-memory position dict accumulates cumulative
state. trades.jsonl remains a per-order audit log; the position dict
represents current holdings.

Trigger: pre-fix, `wait_for_fill` partial-fill (`filled < count`) cancelled
the remainder and called the position done — capital under-deployed when
liquidity is thin. AUS-26MAY01-T56 fired this pattern repeatedly (single-c
fills against a 90-contract intent).

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
| `MAX_EDGE` | `0.50` | Skip edges > 50%. Evolution: 0.40 → 0.42 (4/27) → 0.55 (4/28) → 0.45 (4/29) → **0.50 (5/6)**. 5/6 raise driven by 21:9 winner:loser blocked-rate at 0.45 cap. No bypass — `MAX_EDGE` itself is the firewall. |
| ~~PRICE_ZONE~~ | ~~yes_bid 30-40c~~ | **REMOVED 2026-04-29** after gate-audit on min_bot historical candidates: 2/2 PRICE_ZONE-blocked cases were winners (NYC-26APR25-B42.5 BUY_NO @66¢ → +$0.34/c; ATL-26APR27-B59.5 BUY_NO @67¢ → +$0.33/c). V2's max-temp finding (50% WR / −$99 / n=50) does not appear to hold for min-temp markets. Sample is small (n=2) but 100% winners and gate-cost is non-trivial; re-enable from git history if a future audit flips the signal. |
| **H_2.0** | `disagreement 2°F` | **BUY_NO d-1+ only** (V2-inspired). Skip when pairwise forecast disagreement (NBP/HRRR/NBM max diff) > 2°F on day-1+ markets where we have no obs to break ties. Tighter than `MAX_DISAGREEMENT_F=5.0`. Bypassed by `_obs_confirmed_alive`. |
| `MIN_MODEL_PROB` / `MAX_MODEL_PROB` | `0.05` / `0.85` | Skip wildly unlikely or near-certain. Lowered 0.15→0.05 on **2026-05-06** after audit found 8,022 blocks / 3 days, ~90% would-win rate — bot was killing its own deep-tail BUY_NO channel. **Bypassed by `_nbp_consistent_with_recent_cli`** when forecast μ is within ±2°F of the station's last-7-day CLI low range; bypass is now near-moot for the lower side because `F2A_PROB_LO=0.05` blocks at the same mp threshold. |
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

- Channel `1497464077608550570` receives `ENTRY`, `SETTLED`, and `❄️ NEW LOW`
  messages.
- Bot token comes from `DISCORD_BOT_TOKEN` in `~/.env` (same token V1/V2 use;
  must already be a member of the channel).
- One bounded queue + one worker thread; non-blocking; drops on overflow.
- **NEW LOW alerts** (2026-04-30): on each scan, `_check_new_low_alerts()`
  polls running_min for all 20 stations and emits `❄️ NEW LOW <city> (<icao>):
  X°F → Y°F (Δ −Z°F)` whenever rm drops by ≥ `NEW_LOW_THRESHOLD_F` (0.1°F)
  vs the last seen value. State is per-station; cd rollover resets the
  baseline so the first obs of a new climate day doesn't false-fire. Mirrors
  V1/V2 max-bot's `NEW HIGH` pattern. Detection latency = scan interval (60s
  normal / 15s pre-dawn) + obs-pipeline lag (~9 min p50 from `madis_fsl2`).

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
