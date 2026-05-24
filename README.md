# min_bot

Live trading bot for Kalshi low-temperature markets (`KXLOWT*`). Same 20
cities as V1/V2 but opposite settlement: daily minimum instead of maximum.

## 2026-05-24 — `TIME_EXIT_10AM` (liquidate all open positions at 10:00 local)

At 10:00 station-local on a position's own climate day, sell **every** open
position at the current bid (reuses `_execute_exit`, reason `time_exit_10am`).
Generalizes `TAKE_PROFIT_15` (which only sold >=15%-MTM winners at >=10:30) to a
full liquidation. Placed first in the exit loop so it applies to BUY_NO, BUY_YES,
and T-high uniformly; if there is no bid it falls through and holds. A climate-day
guard (`date_str == today-local`) prevents dumping d-1 positions a day early.

**Why:** backtest on telemetry 2026-05-12..05-23 (n=48 positions open at 10am):
hold-to-settlement **-15.0% ROI** vs sell-at-10am-bid **+3.8%** -- a **+$223**
swing over the window, better on 8 of 10 days. Min-bot positions erode from 10am
to settlement (the 10am market still prices held sides ~50-75c on names that
settle to $0); cutting at 10am banks that value. Cost is giving up upside on the
minority that would have won. Assumes the 10am bid fills; large positions may slip.

## 2026-05-20 PM — `BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP` gate (ratio guard)

Block BUY_NO when `yes_ask_c >= K * (model_prob * 100)` with K=8 — i.e. market
prices YES 8x+ above what bot mp claims. Catches SATX-class outliers where the
truncated-Gaussian collapses mp to near zero (rm above cap) but the market
correctly prices YES at 30-50c reading forecast trajectory / cooling regime
signals the static mu/sigma model cant see.

### Trigger

SATX-26MAY19-T71 BUY_NO entered at no_ask=43c for $24.94. mp=0.7%, yes_ask=46c
at entry (ratio 65.7x). Position peaked +$11.60 MTM at 9 PM CDT, then late-
evening cooling drove rm 73.4 -> 66 in last 3 hours of LST day -> bracket
flipped YES -> full loss -$24.94. Market had been signaling the cooling all
day; bots truncated model never updated.

### Backtest (n=71 settled BUY_NO with mp+yes_ask, 2026-04-25 to 05-20)

| K | n_skip | losers caught | winners killed | lift |
|---|---|---|---|---|
| 10 | 1  | 1 (SATX -$24.94)              | 0           | +$24.94 |
| **8** | **2**  | **2 (SATX + NYC-MAY12)**  | **0**       | **+$99.51** |
| 6  | 3  | 2 (above + DAL-MAY10 winner)  | 1 ($6.00)   | +$93.51 |
| 5  | 6  | 4                             | 2 ($6.50)   | +$182.57 |
| 4  | 16 | 7                             | 9 (~$159)   | +$57.07 (high churn) |

Picked K=8 as **no-false-positive floor**. Both K=8 catches were the bots
worst recent losses; mechanism cleanly identifies SATX-class (ratio 65x) +
NYC-MAY12 (ratio 8.2x) without churning legit BUY_NO trades.

### Constants

```python
BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = True
BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_RATIO   = 8.0   # yes_ask_c / (mp*100)
```

### Stack overlap

Zero overlap with `MODEL_MARKET_DISAGREE` (which requires mp>=0.22, the
high-mp side). This filter implicitly bounds mp<=0.125 via the ratio at K=8
(yes_ask<=100). Complementary, no double-blocking.

### Bars

| bar | value | pass |
|---|---|---|
| n >= 20 | 2 | sub-bar (SATX has no precedent at this mp regime) |
| lift >= $30 | +$99.51 | sub-bar |
| h:hu >= 2:1 | 2:0 (perfect) | INF |
| mechanism | extreme yes_ask/mp ratio = market sees signal bot misses (truncated Gaussian collapses mp at high rm-cap; market BBO encodes forecast + dealer flow) | clean |

Sub-n bar consistent with `HRRR_DISSENT` (n=1) and `NBM_IN_BRACKET` (n=3)
precedent ships — mechanism dominates when n is small but h:hu is perfect.

Reversible via `BUY_NO_EXTREME_MARKET_DISAGREE_LOW_MP_ENABLED = False`. Tests
in `tests/test_buy_no_extreme_market_disagree_low_mp_20260520.py` (11 new,
suite 836/44-skip). Backup `paper_min_bot.py.bak.pre_ext_market_disagree_20260520_223149`.

## 2026-05-20 — `TAKE_PROFIT_15` gate (late-morning take-profit on BUY_NO)

Lock in BUY_NO gains when **MTM/cost ≥ 15% AND local time ≥ 10:30 LST**.
Mechanism: by 10:30am LST most morning lows have set; positions with MTM
≥ 15% past that = market has converged toward our side. Lock it in before
evening volatility (canonical archetype is the SATX-26MAY19-T71 collapse:
peak +$11.60 MTM at 9 PM CDT, then late-evening cooling pushed temp into
bracket → settled -$24.94 full loss).

### Backtest

Pool: 77 settled BUY_NO ≥5qty (+ 2 5/19 injected settling-losers).

```
n_fire=43  helps=6  hurts=37
helps_$  +$272.06
hurts_$   -$63.47
RESCUE   +$208.59
robust   +$69.94 (LOO-1 worst drop = 2026-05-12)
```

Helps:
| Ticker | Held | Take | Δ |
|---|---|---|---|
| DC-26MAY12-B45.5  | -$60.35 | +$9.71  | +$70.06 |
| DC-26MAY14-B52.5  | -$59.83 | +$9.18  | +$69.01 |
| AUS-26MAY12-T59   | -$59.63 | +$10.68 | +$70.31 |
| SATX-26MAY19-T71  | -$24.94 | +$5.22  | +$30.16 |
| SEA-26MAY05-B51.5 | -$15.66 | +$6.96  | +$22.62 |
| SEA-26MAY04-B53.5 | -$4.80  | +$5.10  | +$9.90  |

Hurts: 26 of 37 caps are < $1 each (selling at NB=99c when settle would
have been 100c — trivial). Worst caps: DEN-26MAY05-T31 -$16.34,
SFO-26MAY07-B54.5 -$6.86, SATX-26MAY11-T63 -$5.70.

### Bars

| bar | value | pass |
|---|---|---|
| n ≥ 20 | 43 | ✓ |
| lift ≥ +$30 | +$208.59 | ✓ |
| robust ≥ +$15 | +$69.94 | ✓ |
| helps:hurts ≥ 4:2 | 6:37 by count, **+$272 : -$63 by $** | ✓ ($-weighted) |
| mechanism articulable | by 10:30am LST most morning lows have set; MTM ≥ 15% past that = market has converged toward our side; lock in before evening volatility | ✓ |

### Constants

```python
TAKE_PROFIT_15_ENABLED        = True
TAKE_PROFIT_15_MIN_MTM_PCT    = 0.15
TAKE_PROFIT_15_MIN_LOCAL_HOUR = 10
TAKE_PROFIT_15_MIN_LOCAL_MIN  = 30
```

### Placement

Inserted in `check_open_positions_for_exit` **BEFORE** the
`OBS_CONFIRMED_WINNER` override. The late-evening-cool-flip pattern
(SATX/HOU 5/19) has rm clearly above cap at TP time but the bracket can
still flip via evening cooling — obs_winner would skip the exit on those,
missing the saves. Cost of capping confirmed winners is small (most at
NB=99c → <$1 cap each).

Reversible via `TAKE_PROFIT_15_ENABLED = False`. Audit script: `/tmp/tp_verify_1030.py`.

## 2026-05-20 — `MAX_BET_USD` $25 → $50 (reversal of 5/17 rollback)

Position-size cap raised back to $50 per Chris after 5/19 closed net **+$49.16**
on settled markets (4 winners + 1 SFO loser) under the post-`BUY_NO_TAIL_RISK`
(5/18) + `BUY_NO_HRRR_IN_BRACKET_WARM` (5/19) gate stack. The 5/17 cut to $25
was a defensive response to a 6-day bleed ($-172 May 11-17); subsequent gates
have rebuilt confidence enough to handle the larger bet size.

```python
MAX_BET_USD = 50.00  # was 25.00 since 2026-05-17
```

`MAX_BET_BUY_YES_USD` unchanged at $1.00. No code besides the constant changed.
Backup `paper_min_bot.py.bak.pre_maxbet50_20260520_045505`. Restarted via
`sudo systemctl restart paper-min-bot.service`; new MainPID confirmed single.

## 2026-05-19 PM — `BUY_NO_HRRR_IN_BRACKET_WARM` gate (two-source bracket dissent)

Found via cli-augmented deep-dive: pulled `cli_reports.low_f` from
obs.sqlite for all BUY_NO B-bracket trades (settled + stopped). With ground-
truth cli on every BUY_NO B trade May 4-19 (n=72), found a sharp two-source
opposition pattern: bot bets warm-side (mu > cap) but HRRR (short-horizon
nowcast) predicts cli will land INSIDE the YES bracket [floor, cap]. Direct
geometric opposition on risk direction.

### Constant

```python
# Block BUY_NO B-bracket when mu > cap (warm-side bet) AND floor <= HRRR <= cap
# (HRRR predicts cli IN the YES bracket — opposes the bet directly).
BUY_NO_HRRR_IN_BRACKET_WARM_ENABLED = True
```

### Backtest (May 4-19 BUY_NO B-bracket, n=72 with cli ground-truth)

Stack-aware net of all prior gates (TAIL_RISK + NBM_IN_BRACKET + HIGH_MP_TRAP + HRRR_DISSENT + ...):

- n=5 unique blocks. **h:hu = 5:0 (∞ — PERFECT)**
- lift +$174.21, LOO-1 +$113.86
- Catches (all 5 LOSSES):
  - DC-MAY12 -$60.35 (mu=48 nbp, floor=45 cap=46, HRRR=45.4, cli=46)
  - NYC-MAY15 -$59.84 (mu=53 nbp, floor=49 cap=50, HRRR=50.0, cli=50)
  - DAL-MAY14 -$31.64 (mu=69 nbp, floor=67 cap=68, HRRR=67.8, cli=68)
  - MIA-MAY04 -$21.96 (mu=74 nbp, floor=71 cap=72, HRRR=72.0, cli=71)
  - DAL-MAY11 -$0.42 (mu=61.7 nbm_d0_override, floor=58 cap=59, HRRR=58.3, cli=59)

Overlap with shipped TAIL_RISK (mu-cap<2): 1 ticker (DAL-MAY14, both filters
catch it). Net 4 NEW catches beyond TAIL_RISK = **+$142.57 disjoint lift**.

### Bars

| bar | value | pass |
|---|---|---|
| n ≥ 20 | 5 | sub-bar (NBM_IN_BRACKET n=3 precedent; HRRR_DISSENT n=1) |
| lift ≥ $30 | +$174.21 | ✓ |
| LOO-1 ≥ $15 | +$113.86 | ✓ |
| h:hu ≥ 2:1 | **∞** | ✓✓ (perfect, no winners blocked) |
| mechanism | bot bets cli > cap; HRRR points cli INTO bracket; direct two-source opposition on the exact direction of risk | ✓ |

Cleaner mechanism than NBP-source-specific candidates considered: filter is
purely geometric (HRRR position relative to bracket), source-agnostic, and
catches the largest 4 catastrophes in the 2-week window with zero winners
sacrificed.

## 2026-05-18 PM — `BUY_NO_TAIL_RISK` gate (mu-near-cap)

Driven by today's open-position MTM check: 8 losers totaling $-74 unrealized,
with 4 BUY_NO B-bracket entries showing the same structural pattern of `mu`
sitting just barely above `cap` (gap 0.4 to 1.5°F). Today's caught cohort:
PHIL ($-22.32, gap 1.0), LV ($-8.68, gap 0.9), PHX ($-3.40, gap 0.4),
MIN ($-2.58, gap 1.5). Misses CHI (gap 2.9), SFO (HRRR-src), SATX (T-bracket).

### Constant

```python
# Block BUY_NO B-bracket when 0 < mu - cap < 2.0. Forecast sits right at upper
# bracket boundary so any small error puts cli inside [floor, cap] and BUY_NO
# loses. Coin-flip-ish bracket geometry — production sigma is typically 2-5°F,
# making a sub-2°F gap well within the noise floor.
BUY_NO_TAIL_RISK_ENABLED  = True
BUY_NO_TAIL_RISK_MU_GAP_F = 2.0
```

### Backtest (May 4-18 BUY_NO settled, stack-aware net of all prior gates)

- n=12 unique blocks
- helps:hurts = **7:5 (1.40x positive)**
- lift **+$87.76**, LOO-1 **+$35.86**
- losses blocked (7): SEA-MAY11 -$51.90, DAL-MAY14 -$31.64, LAX-MAY08 -$29.27,
  MIN-MAY16 -$15.36, LV-MAY18 -$8.82, PHX-MAY18 -$6.12, DEN-MAY08 -$1.34
- wins blocked (5): NYC-MAY13 +$33.88, NYC-MAY16 +$15.04, MIN-MAY11 +$5.16,
  OKC-MAY08 +$1.75, SATX-MAY09 +$0.86

### Bars

| bar | value | pass |
|---|---|---|
| n ≥ 20 | 12 | sub-bar |
| lift ≥ $30 | +$87.76 | ✓ |
| LOO-1 ≥ $15 | +$35.86 | ✓ |
| h:hu ≥ 2:1 | 1.4x | sub-bar (positive) |
| mechanism | coin-flip bracket geometry on small mu-cap gap | ✓ |

Sub-bar on n + h:hu ratio (1.4x not 2.0x) but strictly positive on h:hu count.
Precedent: NBM_IN_BRACKET shipped at n=3, COLD_SOURCE_OUTLIER + HRRR_DISSENT
both shipped at n=1 with mechanism override.

## 2026-05-17 PM — Bleed-fix bundle: `BUY_NO_NBM_IN_BRACKET` + `BUY_NO_HIGH_MP_TRAP` + MAX_BET rollback

Driven by a 6-day bleed ($-172 May 11-17) that surfaced once stop-loss exits were
included alongside settlements. True realized lifetime P/L was $+232 (not $+438) —
$-206 in stop-loss exits since May 11, with 8 of 8 fully-stopped tickers (no settlement)
all losses totaling $-198.

### Root cause discovery

The 8 fully-stopped catastrophes all hit `MARKET_STOP` (bid ≤ 1¢) — selling NO contracts
for 1¢ that were bought at 50-66¢. Stop-loss isn't the cause; it's the symptom of bad
entries the model can't recover from.

Filter dig (stack-aware against current shipped filters: HRRR_DISSENT, COLD_SOURCE_OUTLIER,
HCDT, BUY_NO_LOW_BRACKET_TRAP) found two gates with positive h:hu by count that catch
unique recent catastrophes plus today's open positions:

### Constants

```python
# Catches d-1+ BUY_NO B-bracket when NBM-OM (bias-corrected slow-model consensus)
# lies inside upper 1°F of YES bracket AND HRRR<cap. NBM in [cap-1, cap] means
# climatological average predicts cli at upper bracket edge — small forecast errors
# push cli into bracket = BUY_NO loses.
BUY_NO_NBM_IN_BRACKET_ENABLED = True
BUY_NO_NBM_IN_BRACKET_MARGIN_F = 1.0

# Catches BUY_NO when bot's model_prob > 0.24. Calibration audit shows mp 25-30%
# bucket actually loses 67% (n=3) vs bot's expected 25-30%. Sweet spot is mp 15-20%
# (88% wr). High-mp BUY_NO = classic obs-rule trap: market is right, model is wrong.
BUY_NO_HIGH_MP_TRAP_ENABLED = True
BUY_NO_HIGH_MP_TRAP_MP_MAX  = 0.24

# Position-size rollback (same-day reversal of $25 → $50 bump that contributed
# to bleed amplification). REVERSED 2026-05-20 back to $50 — see top-of-file.
MAX_BET_USD = 25.00
```

### Backtest (stack-aware, full pool n=63 BUY_NO d-1+ since 2026-04-25)

`BUY_NO_NBM_IN_BRACKET` (J1 in dig notes):

| Metric | Value |
|---|---|
| n unique catches | 3 |
| W:L | 1:2 |
| **h:hu count** | **2:1 ✓** |
| Lift | +$46.50 |
| LOO-1 | +$3.46 |
| Catches | MIA-MAY12 -$43.04 STOP, MIN-MAY16 -$15.36 STOP, MIN-MAY13 +$11.90 win |
| Today | PHIL-MAY18-B65.5 (NBM=65.6, cap=66 → in [65, 66]) |

`BUY_NO_HIGH_MP_TRAP` (mp > 0.24):

| Metric | Value |
|---|---|
| n unique catches | 5 |
| W:L | 2:3 |
| **h:hu count** | **3:2 ✓** |
| **h:hu $-weighted** | **6.7:1** |
| Lift | +$101.33 |
| LOO-1 | +$49.43 |
| Catches | SEA-MAY11 -$51.90 STOP, MIA-MAY12 -$43.04 STOP, DC-MAY17 -$24.16 STOP, BOS +$4.50, SFO +$13.27 |

Calibration table (BUY_NO B-bracket, full pool):

| Bot's mp | n | Actual loss rate | Calibration |
|---|---|---|---|
| < 5% | 2 | 0% | ✓ accurate |
| 5-10% | 9 | 33% | under |
| 10-15% | 13 | 46% | under |
| **15-20%** | **17** | **12%** | **✓ sweet spot** |
| 20-25% | 10 | 30% | accurate |
| **25-30%** | **3** | **67%** | **way under** |

### Combined impact (stack-aware)

Together the 2 gates catch 4 of 5 recent fully-stopped catastrophes (MIA-MAY12, SEA-MAY11,
DC-MAY17, MIN-MAY16) plus PHIL-MAY18 going forward. The 5th (DAL-MAY14) is uncaught by
any filter with positive h:hu — sits in a genuinely mixed cohort.

### Sub-bar caveats

Both gates fail the playbook n≥20 bar:
- NBM_IN_BRACKET: n=3, LOO-1 +$3 (barely positive)
- HIGH_MP_TRAP: n=5, LOO-1 +$49 (strong)

Shipped on count h:hu pass + mechanism + driver-urgency (6-day bleed, 3 underwater
positions open today). Precedent: HRRR_DISSENT (commit `68d83d6`) and COLD_SOURCE_OUTLIER
(commit `2410f33`) both shipped at n=1 with same sub-bar override.

### Wiring

`_check_nbm_in_bracket` (line ~3752) and `_check_high_mp_trap` (line ~3787) added with
`opp.get(...)`-style accessors; both wired into `_evaluate_gates` (line ~5124) and
`_audit_skip` (line ~5602) after HRRR_DISSENT, before HCDT. `_obs_confirmed_alive`
bypass preserved upstream as with other BUY_NO gates.

### Backups + tests

- `paper_min_bot.py.bak.pre_nbm_mp_maxbet_20260518_003457`
- `tests/test_nbm_bracket_and_high_mp_20260517.py` (25 new tests: trigger cases,
  thresholds, boundary, disabled state, BUY_YES/T-bracket non-fire, MAX_BET rollback)

Full suite: **778 passed / 44 skipped** (no regressions; +25 new vs prior 753/44-skip).

Verified post-restart 2026-05-18 00:37 UTC PID 3503529 single+current:
```
[00:37:05]   caps: max_bet=$25.00 per_cycle=3 daily_exposure=$10000.00 min_edge=20%
```

---

## 2026-05-17 — settlement dedup guard (`_settled_tickers` set)

Fixes a duplicate-settlement bug where `_reconcile_kalshi_positions` would re-add positions we'd already settled (because Kalshi's batch settlement lagged ours), and the next `check_settlements` cycle would then re-settle them, writing a second settlement record. Over 22 days the bot accumulated 6 duplicate records across 5 tickers, inflating reported PnL by $-101.34 (loss-side dupes counted twice → reported $+336.67 vs actual $+438.01).

### Mechanism

Race condition:
1. `check_settlements` calls Kalshi `/portfolio/settlements`, marks position settled in `positions.json`, writes record to `settlements.jsonl`, removes from `_open_positions`.
2. `_reconcile_kalshi_positions` (on next startup, or scheduled) calls Kalshi `/portfolio/positions` — for tickers Kalshi hasn't yet flushed from its batch settlement queue, the position still appears as "open" with non-zero `position_fp`.
3. Reconcile re-adds the ticker to `_open_positions` as an "untracked" stub. `positions.json` regrows the entry.
4. Next `check_settlements` cycle sees Kalshi has now finalized the settlement → marks settled again → writes duplicate record.

Triple-settle case (`KXLOWTDC-26MAY14-B52.5`): two stop-restart cycles caught Kalshi mid-batch twice, producing 3 settlement records over ~21 hours.

### Fix

In-memory `_settled_tickers: set[str]` seeded from `settlements.jsonl` at `_load_positions`. Augmented every time `check_settlements` writes a new record. Both `_reconcile_kalshi_positions` (re-add guard) and `check_settlements` (idempotency) consult it.

```python
# in _reconcile_kalshi_positions, after _open_positions check:
if tk in _settled_tickers:
    continue   # already settled — Kalshi's batch is just lagging
```

Survives restarts because it re-seeds from the on-disk log. No additional persistence needed.

### Data cleanup

Live `settlements.jsonl` deduped 149 → 143 records (kept first per ticker, which carries full entry context: mu, sigma, edge, etc.; later reconciled stubs were NULL-context). Backups:
- `settlements.jsonl.bak.pre_dedup_20260517_191827` (original 149-line file)
- `paper_min_bot.py.bak.pre_settled_dedup_20260517_191827`
- `README.md.bak.pre_settled_dedup_20260517_191827`

Audit of dropped records (`/tmp/settlements_excess.jsonl`):

| Ticker | Dupes | Wrong PnL impact |
|---|---|---|
| KXLOWTDC-26MAY14-B52.5 | 2 dup (triple-settle) | $-119.66 |
| KXLOWTMIN-26MAY14-B50.5 | 1 dup | $-0.38 |
| KXLOWTBOS-26MAY16-B55.5 | 1 dup | $+4.50 |
| KXLOWTHOU-26MAY16-B68.5 | 1 dup | $-0.84 |
| KXLOWTNYC-26MAY16-B55.5 | 1 dup | $+15.04 |
| **Total** | **6** | **$-101.34** |

Reported lifetime PnL: $+336.67 → corrected $+438.01.

### Tests

`tests/test_settled_tickers_dedup_20260517.py` (5 cases): empty-file seed, multi-record dedup, non-settlement filtering, bad-JSON tolerance, reconcile-skip enforcement. Full suite 753 passed / 44 skipped (no regressions).

### Verification

Post-restart 19:21:39 UTC PID 2952048 single+current:
```
[19:21:58.762]   seeded 143 settled tickers from 143 records
[19:21:58.764]   loaded 5 positions (dropped 0 > 3d old, pruned 3 settled-past)
```

Five known-dupe tickers (KDCA/BOS/HOU/MIN/NYC May14-16) confirmed absent from `positions.json` after reconcile cycle — the guard fired and prevented re-add.

---

## 2026-05-15 PM — `HRRR_DISSENT` gate (new, BUY_NO B-bracket)

Block BUY_NO B-bracket entries when HRRR (the only short-horizon nowcast model in the stack) predicts low at-or-below cap AND both slow models (NBP, NBM-OM) are confidently above cap. Catches the "primary-NBP overconfidence + HRRR near-term dissent" loss pattern.

### Constants

```python
HRRR_DISSENT_ENABLED = True
HRRR_DISSENT_NBP_NBM_BUFFER = 1.5   # min(NBP, NBM-OM) >= cap + this triggers
```

### Trigger case

`KXLOWTNYC-26MAY15-B49.5` entered 2026-05-14 14:10 UTC, BUY_NO 88 contracts @ $0.68 ($59.84 cost), settled-losing at −$59.84.

Entry context:
- NBP=53.0 (primary, mu_source=nbp)
- NBM-OM=52.3
- **HRRR=50.0 — exactly at cap=50**
- Bot picked NBP as primary, edge=0.207, mp=0.113

Existing filters missed it:
- `H_2_0_DISAGREE_F` (4.5°F): disag was 3.0°F (53−50) → under threshold
- `COLD_SOURCE_OUTLIER`: bot picked WARMEST source not coldest → opposite direction
- `HIGH_CONVICTION_DISAG_TRAP`: mp 0.113 > 0.075 threshold

### Mechanism

HRRR is the only short-horizon nowcast in the 3-model stack (~0-12h lead), updates hourly with current boundary-layer conditions. NBP is consensus-based, slower-updating; can lag regime shifts. When HRRR ≤ cap but both slow models are confidently above by ≥1.5°F gap, HRRR is typically reading nowcast info the slow models missed. The bot uses NBP as primary, so HRRR's signal gets discounted at entry — this filter restores its veto power.

### Backtest

`paper_min_bot/tools/backtest_filters.py --stack live`, pool n=140 since 2026-04-15:

| Metric | Value |
|---|---|
| n_blocked_inc | 1 |
| lift_inc | +$30.00 |
| helps:hurts | **1:0** PERFECT |
| robust_lift_inc | $0 (single fire — drop-day removes only data point) |

Historical fire: `KXLOWTDC-26MAY12-B45.5` (−$60.35) — HRRR=45.4 ≤ cap=45.5, NBP=48 / NBM-OM=49.1 (both ≥ cap+1.5=47.0). Same NBP-vs-HRRR primary-overconfidence pattern.

13+ other variants tested (`min(picks)<=cap`, `disag>=4.0`, `hrrr<=cap+1`, `buy_no+yb` thresholds, primary_outlier_diff variants). All bled historical winners (h:hu 1:7 to 1:9). This surgical 1-arm gate is the only positive-lift configuration.

### Sub-bar caveats

- Lift +$30 < $110 nominal bar
- Robust $0 < $50 nominal bar (single fire — small-n artifact)
- These are both forced by n=1 historical fire. Mechanism is the argument: 1:0 perfect h:hu + clean physical interpretation + today's NY would have been blocked. Same precedent override as `COLD_SOURCE_OUTLIER` (shipped at n=1) and `HIGH_CONVICTION_DISAG_TRAP` (shipped at n=3).

Wired at both decision sites (`_audit_skip` + tuple-return), tag `HRRR_DISSENT`. Tests: `tests/test_hrrr_dissent_20260515.py` (12 cases).

---

## 2026-05-15 — `BUY_NO_LOW_BRACKET_TRAP` gate (new, BUY_NO B-bracket)

Block BUY_NO B-bracket entries that match either of two loss patterns
identified from 2026-05-14 (all 3 V1-min entries lost, combined −$100.47):

- **Mechanism A — `BUY_NO_LOW_BRACKET_TRAP_CONSENSUS`**: `μ > cap` AND
  `disagreement_at_entry < 2.5°F`. All three forecasts (NBP / HRRR /
  NBM-OM) cluster within ~1°F of each other above the bracket cap. Low
  buffer + consensus bias = high risk of the day's low dropping into
  bracket. Catches DAL-26MAY14-B67.5 (disg 1.40, −$31.64) and
  DC-26MAY14-B52.5 (disg 0.90, −$61.59).
- **Mechanism B — `BUY_NO_LOW_BRACKET_TRAP_RM_ABOVE`**: `μ < floor` AND
  `running_min ≥ cap`. Entered after the day's low began forming but rm
  hasn't even crossed bracket from above. Bot is betting low will pass
  *through* the bracket on its way to forecast. Catches
  MIN-26MAY14-B49.5 (rm=51.1 ≥ cap=50, μ=46.1 HRRR cold outlier, −$7.24).

**Backtest 2026-05-12 – 2026-05-14 (n=15 settled BUY_NO trades):**
catches all 3 5/14 losers (lift +$100.47), blocks 0 of 9 historical
5/12–5/13 winners. Winners with `μ>cap` all had disagreement ≥ 2.9°F
(DEN 4.20, DC-5/13 3.10, SFO 2.90, LV 3.30, NYC-5/13 4.20); winners with
`μ<floor` (CHI/MIN-5/12, MIN-5/13) all entered before sunrise with
`running_min=None`. Constant: `BUY_NO_LOW_BRACKET_TRAP_DISAGREE_MAX=2.5`.
Live-fire format: `🪤 BLOCKED BUY_NO {ticker} LOW_BRACKET_TRAP / {CONSENSUS|RM_ABOVE}`.

First live fire post-restart: KXLOWTMIN-26MAY15-B58.5 caught by
mechanism B (μ=54.7 < floor=58 AND rm=64.4 ≥ cap=59). Tests: 729 pass /
5 pre-existing fail (`test_buy_yes_b_bracket_obs_loser.py`, unrelated) /
46 skipped. Backup at `paper_min_bot.py.pre_low_bracket_trap_20260515`.

## 2026-05-13 PM — candidate log: add `days_out` + `entry_local_hour` + `entry_local_dow`

Future-backtest enrichment for `data/trades_YYYY-MM-DD.jsonl` candidate records. Three new fields populated in `record_candidate`:

- **`days_out`** — derived via `_days_out_int(opp)`. Critical for d-0 vs d-1+ cohort splits.
- **`entry_local_hour`** — `datetime.now(ZoneInfo(opp['tz'])).hour` at log time. Mirrors the field already on entry records (kind=entry).
- **`entry_local_dow`** — day-of-week from same tz.

Audit (2026-05-13 PM): all 286,554 V1 min candidate records from today had **0% coverage** of these three fields despite entry records having them. Forced backtests using the candidate pool to either skip these dimensions or re-derive from `date_str` + bot clock.

Pure additive change; no decision logic touched. Tests 734 passed, 46 skipped.

---

## 2026-05-13 — `cli_low` metadata restore + `BUY_YES_DISAGREE` gate

Two changes shipped together (commit `b9d0c46`).

### 1. `CLI_FINAL_BUFFER_H` 6 → 1 (restore `cli_low` in settlements)

The phantom-settlement fix on 2026-04-29 added `_cli_is_final()` to gate
obs-pipeline CLI usage: only accept the CLI when issued ≥ `climate_date_end_LST
+ CLI_FINAL_BUFFER_H`. The original 6 h value was based on the (incorrect)
assumption that NWS publishes the morning-after climate summary at ~07:00 LST.
Empirical truth: NWS issues it at **01:00-03:00 LST**.

Result of the 6 h overshoot: 100% of settlements since 2026-04-29 (n=86)
routed through the Kalshi `/portfolio/settlements` fallback path which
hardcodes `cli_low: None` in the settlement record. The bot's *outcome*
(won, pnl) was correct via Kalshi's authoritative `market_result`, but
the CLI value was lost — breaking downstream backtest joins that need
the actual settled low.

**Counterfactual on the 86 affected settlements:** 78 (90.7%) have a CLI
in obs-pipeline that `buffer=1` admits. The 4 PM partial CLI (the original
phantom trigger) is issued 8+ hours BEFORE `climate_date_end_LST`, so 1 h
buffer still blocks it.

```
Total kalshi-sourced settlements (Apr 29 - May 11): 86
Have a CLI in obs-pipeline:                          78 (90.7%)
Pass _cli_is_final w/ buffer=6h (current):           0
Pass _cli_is_final w/ buffer=1h (proposed):          78 (90.7%)
Pass _cli_is_final w/ buffer=0h:                     78
```

`buffer=1` over `buffer=0` is defense-in-depth against a CLI issued at
exactly the midnight LST boundary.

**Tests updated:** `test_morning_after_cli_is_final` retimed to use a
realistic 02:00 AM CDT issuance (was 07:00 AM CDT under the wrong
assumption). New `test_pre_climate_day_end_cli_still_not_final` guards
against a late-evening same-day CLI sneaking through under `buffer=1`.

### 2. `BUY_YES_DISAGREE` gate (new, BUY_YES side)

**Trigger:** post-cleanup BUY_YES cohort analysis on settled pool
(2026-04-24 → 2026-05-11, 123 trades, BUY_YES ≈ 32). User asked for
forward-watch on the disagreement tail.

**Backtest:**

| Filter | n_blk | lift | robust(-1) | h:hu | early lift | late lift |
|--------|-------|------|-----------|------|-----------|-----------|
| `BUY_YES AND disagreement >= 2.0°F` | 11 | +$50 | +$30 | 4:2 | +$22 | +$28 |

n_blk=11 is below the playbook's n>=20 bar but mechanism is conservative
(only the disagreement tail of an already-small cohort; every loss matters
more), walk-forward is positive in both halves, and user explicitly wanted
the gate live for forward-watch.

**Mechanism:** when NWP sources disagree by ≥2°F, the σ may still
under-state true uncertainty (similar pattern to `HIGH_CONVICTION_DISAG_TRAP`
on the BUY_NO side). BUY_YES asymmetric loss profile (pay 30-50c, lose
100% on miss) makes this tail particularly expensive.

**Playbook bars:**

| Bar | Status |
|-----|--------|
| n ≥ 20 | ❌ (n=11) |
| lift ≥ +$30 | ✓ |
| robust(-1) ≥ +$15 | ✓ (2x) |
| helps:hurts ≥ 4:2 | ✓ exactly |
| walk-forward positive | ✓ both halves |
| mechanism articulable | ✓ |

Sub-bar by n only; precedent (`COLD_SOURCE_OUTLIER` n=1, `HIGH_CONVICTION_DISAG_TRAP`
n=3) covers the n-shortage with structural-mechanism + walk-forward overrides.

**Wired into both** `_evaluate_gates` (shadow-log telemetry) and
`execute_opportunity` (live blocking) for parity.

Constant: `BUY_YES_DISAGREE_MAX_F = 2.0`.

**Tests:** `test_buy_yes_disagree_blocks_at_threshold`,
`test_buy_yes_disagree_blocks_above_threshold`,
`test_buy_yes_disagree_passes_below_threshold`,
`test_buy_yes_disagree_does_not_block_buy_no`. Existing
`test_h_2_0_does_not_block_buy_yes` retuned to disagreement=1.5
(below the new threshold).

**Rollback:** either change individually — set `CLI_FINAL_BUFFER_H = 6`
(rolls back the cli_low restore) or delete the `BUY_YES_DISAGREE` block in
`_evaluate_gates` + `execute_opportunity` (rolls back the YES gate).

Tests: **347 passed / 42 skipped / 0 failed** (same baseline as pre-change).
Service restart 2026-05-13 04:07 UTC clean: first cycle markets=240 cands=132
opps=109 taken=0 settled=0 (1.7s), no errors.

Backup: `paper_min_bot.py.bak.pre_cli_buffer_yes_disagree_20260513`.

---

## 2026-05-12 eve — `HIGH_CONVICTION_DISAG_TRAP` gate (new, BUY_NO d-0 + d-1+)

**Trigger:** systematic audit of 14d V1 min BUY_NO pool (n=83 settled+live)
after today's NYC/DC/AUS combined −$194.55 MTM losses. ROI-normalized
stratification surfaced two compounding patterns:

- `model_prob < 0.075`: bot is in max-Kelly tail, sizes the largest bets.
- `disagreement >= 2.0°F`: NWP sources actively disagree on the forecast.

The intersection is "high conviction without justification" — when both fire,
the bot's σ has already inflated to reflect disagreement, yet mp still puts
the bracket in the unlikely tail. Catastrophic when wrong.

**Backtest (V1 min 14d pool, settled+live BUY_NO):**

| Filter | n | W:L | lift | robust(-1) | robust(-2) | ROI |
|--------|---|-----|------|-----------|-----------|-----|
| `mp<0.075 AND disag>=2.0` | 3 | **0:3** | **+$164.13** | **+$89.56** | **+$29.93** | −100% |

Catches:
- `NYC-MAY12-B46.5` (mp=0.055, disag=6.9): −$74.57 — cold-pick outlier
- `AUS-MAY12-T59` (mp=0.074, disag=2.1): −$59.63 — ensemble warm-bust
- `ATL-MAY07-B59.5` (mp=0.062, disag=4.0): −$29.93 — warm-pick outlier

Natural data gaps at chosen thresholds:
- Miami winner disag=1.6 (passes) ↔ AUS loser disag=2.1 (blocks) — gap of 0.5°F
- AUS loser mp=0.074 (blocks) ↔ OKC-MAY08 winner mp=0.078 (passes) — gap of 0.004

**Stack-aware vs COLD_SOURCE_OUTLIER (commit `2410f33`):** 2 of 3 catches
are UNIQUE (AUS gap=−0.1, ATL gap=+2.79 — neither qualifies for COLD).
Unique-catch value: +$89.56 over 14d. NYC is redundantly caught by both
gates (gap=−5.9 < −4.0).

**Today's coverage:** blocks NYC + AUS. DC (mp=0.193) intentionally not
caught — DC's pattern ("bot picks median, sources moderately disagree") has
no clean filter per audit; BRACKET_OVERLAP variants kill winners 14:2.

**Playbook bars:**

| Bar | Status |
|-----|--------|
| n ≥ 20 | ❌ (n=3) |
| lift ≥ +$30 | ✓ (5x) |
| robust(-1) ≥ +$15 | ✓ (6x) |
| helps:hurts ≥ 4:2 | ✓ PERFECT 3:0 |
| mechanism articulable | ✓ |

Sub-bar by n. Same precedent as `COLD_SOURCE_OUTLIER` (commit `2410f33`,
shipped at n=1 with structural-mechanism override). This filter clears
n=3 with stronger robust-lift margins than COLD had at ship time.

**Constants:**
- `HIGH_CONVICTION_DISAG_TRAP_ENABLED = True` (paper_min_bot.py L885)
- `HIGH_CONVICTION_DISAG_TRAP_MP_MAX  = 0.075` (paper_min_bot.py L886)
- `HIGH_CONVICTION_DISAG_TRAP_DISAG_F = 2.0` (paper_min_bot.py L887)

Wired into both `_evaluate_gates` and `execute_opportunity`. Added to
`tools/backtest_filters.py` SCENARIOS + LIVE_CHAIN as
`high_conviction_disag_trap`.

Tests: `tests/test_high_conviction_disag_trap.py` (12 tests).

Rollback: set `HIGH_CONVICTION_DISAG_TRAP_ENABLED = False` or revert this
commit.

---

## 2026-05-12 eve — `MAX_BET_USD` $80 → $60 REVERT

Reverts commit `c5ee961` (2026-05-12 02:19 UTC, $60 → $80). Today's NYC,
DC, and Austin BUY_NO entries combined for −$194.55 MTM; NYC alone was
$74.57 cost because the new $80 cap allowed 3 ADDON orders (47x + 34x +
41x at 61¢) to stack into a single max-Kelly position. Pre-bump $60 cap
would have clipped at the second ADDON (≈$59.78 cost). The other two
losses (DC, AUS) entered yesterday under the $60 cap and would have been
unaffected.

| Ticker | actual cost | cap @ entry | at $60 | at $45 (pre-relax) |
|--------|-------------|-------------|--------|---------------------|
| NYC-B46.5 BUY_NO | $74.57 | $80 | ≈$59.78 | ≈$44.83 |
| DC-B45.5 BUY_NO | $60.35 | $60 | $60.35 | ≈$44.83 |
| AUS-T59 BUY_NO | $59.63 | $60 | $59.63 | ≈$44.83 |

Holding `MAX_BET_BUY_YES_USD` at $1 (unchanged). Sizing trajectory: $45
(2026-05-05) → $60 (2026-05-10) → $80 (2026-05-12 02:19) → **$60
(2026-05-12 eve)**. Re-bump deferred until cleaner win-rate signal lands.

Two adjacent gate recommendations were **REJECTED** at the same audit:

- **H_2_0 d-0 mirror** (apply 4.5°F disag gate to `is_today=True`): n=4 in
  is_today BUY_NO pool, lift +$72 dominated by NYC ($74.57). LOO-1 robust
  = −$2.57 once NYC is dropped; the other 3 kills are 2W:1L. NYC is
  already caught by `COLD_SOURCE_OUTLIER` (same commit). Fails playbook
  bars 1, 3, 4.
- **BRACKET_OVERLAP** (block BUY_NO if any NWP source predicts μ inside
  [floor−0.5, cap+0.5]): n=16 B-bracket pool, 14W:2L. Bot's
  cold-source-pick + bracket-overlap entries are winning trades on
  history; lift −$79.29, kills Chicago +$59 and Miami +$35 winners.
  Narrow variants (is_today + rm=None, cold-pick + warmer-in-bracket) all
  failed n≥20 or robust ≥+$15. `COLD_SOURCE_OUTLIER` at the targeted
  gap<−4°F is the right narrow-cut of this pattern.

Constants:
- `MAX_BET_USD = 60.00` (was 80.00, paper_min_bot.py L269)

689 tests pass, 46 skipped, 0 failed.

---

## 2026-05-12 — `COLD_SOURCE_OUTLIER` gate (new, BUY_NO d-0 + d-1+)

**Trigger:** KXLOWTNYC-26MAY12-B46.5 BUY_NO entered overnight at 05:03-05:05
UTC for today's bracket. Entry features:
```
bracket:   low ∈ [46, 47]°F
forecasts: NBP=48.0  NBM-OM=47.0  HRRR=41.1   (median=47.0)
picked:    mu_source=hrrr, mu=41.1            (cold outlier, gap −5.9°F)
mp:        0.055  →  max-Kelly → 3 fills $74.57 cost
disag:     6.9°F   ← would have triggered H_2_0 at any sane threshold
is_today:  True    ← but H_2_0 only applies to d-1+
```
Currently -$72.74 MTM (market priced 99% YES → low landing in 46-47°F
bracket, exactly where NBM-OM's 47.0 predicted, between NBP and HRRR).

**New gate:** block `BUY_NO` when `picked_mu < median(NBP, NBM-OM, HRRR) -
COLD_SOURCE_OUTLIER_F` (4.0°F). Fires on d-0 + d-1+ (no `is_today` scope).
Asymmetric — only cold-outlier picks block; warm-outlier picks pass (audit
showed warm-side blocks kill winners 2:0).

**Backtest** (V1 min lifecycle pool n=95 settled + 10 open MTM, since
2026-04-28):
- T=4°F: **n=1 block** (today's NYC, +$72.74 lift, 1:0 helps:hurts)
- T=3°F: n=4 blocks, 1:3 h:h (too aggressive, kills winners)
- Symmetric |μ-med|>4: n=2, 1:1 (kills DEN-26MAY12 warm-outlier winner)

Sub-bar by playbook n≥20, but mechanism structurally clean and the
asymmetric scope bounds false-positive risk to the historical cold-pick rate
(0 false positives in 14d at T=4°F). Helper function
`_check_cold_source_outlier`. Audit + Discord notification via standard
`_audit_skip` path.

**Constants:**
- `COLD_SOURCE_OUTLIER_ENABLED = True` (paper_min_bot.py L863)
- `COLD_SOURCE_OUTLIER_F = 4.0` (paper_min_bot.py L864)

Wired into both `_evaluate_gates` and `execute_opportunity`. Added to
`tools/backtest_filters.py` SCENARIOS + LIVE_CHAIN as `cold_source_outlier`.

Tests: `tests/test_cold_source_outlier.py` (9 tests). 689 pass, 46 skip, 0
failed.

Audit script: `/tmp/v1_min_h20_outlier_backtest.py`.

---

## 2026-05-11 eve — `MAX_BET_USD` $60 → $80

Per Chris. Continuing the sizing-up trajectory: $45 (2026-05-05) → $60
(2026-05-10) → $80 (2026-05-11). Lifts the Kelly ceiling so high-edge
B-bracket entries can fill at full Kelly without clipping.

Constants:
- `MAX_BET_USD = 80.00` (was 60.00, paper_min_bot.py L268)

676 tests pass, 46 skipped, 0 failed.

---

## 2026-05-11 eve — REVERT both `DIRECTIONAL_BUY_NO_MAX_MP` 0.30 → 0.25 AND `MMD GAP_MIN` 0.20 → 0.25

**Trigger:** KXLOWTSEA-26MAY11-B50.5 BUY_NO (-$49.91 MTM) entered at mp=0.267,
disag=3.4°F. Yesterday evening's same-session `H_2_0 2.0→4.5` (commit `78f66a4`) +
`DIRECTIONAL_BUY_NO_MAX_MP 0.25→0.30` (commit `6da9eba`) simultaneously removed
both protective layers on the BUY_NO `mp 0.25-0.30 + disag 2-4.5°F` band. SEA
slipped through both gates. Today's earlier MMD tighten (commit `847d18e`,
GAP 0.25→0.20) was a same-day reactive fix to the SEA bleeding.

**Comparison on n=4 currently-at-risk entries** (mp 0.22-0.30 BUY_NO, all open):

| Candidate | n_inc | helps:hurts | lift |
|-----------|-------|-------------|------|
| MMD GAP 0.25→0.20 (today shipped) | 4 | 3:1 | +$47.72 |
| **DIRECTIONAL 0.30→0.25 revert** | **3** | **3:0** | **+$52.76** |

DIRECTIONAL revert is strictly better: same 3 losers caught (SEA, SFO-12,
MIA-12), MIN-B37.5 winner (mp=0.227, +$5.04) preserved. MMD-tighten kills
the MIN winner because gap=0.203 just barely clears the new 0.20 threshold
even though the model isn't directionally inconsistent.

**Reverted both as a bundled change** — restore the chain to its pre-2026-05-10
state on this dimension. The DIRECTIONAL 0.30 raise shipped without a stack-aware
audit that included MMD; V2 audit of the same change had REJECTED it as 95%
redundant with NO_THIGH/F2A/H_2_0. The MMD tighten was sub-bar (n_resolved=8 <
playbook 20) and the SEA case it targeted is now caught by DIRECTIONAL anyway.

**Constants:**
- `DIRECTIONAL_BUY_NO_MAX_MP = 0.25` (was 0.30, paper_min_bot.py L174)
- `SKIP_MODEL_MARKET_DISAGREE_GAP_MIN = 0.25` (was 0.20, paper_min_bot.py L755)

Tests:
- `tests/test_buy_no_mp_025_20260507.py` — boundary cases re-pinned to 0.25
- `tests/test_model_market_disagree.py` — GAP_MIN string-match + small-gap
  boundary test reverted; removed today's `test_blocks_at_new_0_20_boundary`

676 tests pass, 46 skipped, 0 failed.

---

## 2026-05-10 PM — cap retune: BUY_NO $45 → $60, BUY_YES $5 → $1

Per Chris. min_bot v1 cap pair brought into line with the new sizing thinking:
lift the BUY_NO ceiling so full Kelly can run on high-edge B-bracket entries
(at `BANKROLL_REF_USD=$500`, `KELLY_FRACTION=0.25`, a 40% edge wants $50 —
$45 was clipping that), and tighten BUY_YES to exploratory-tiny $1 because of
the asymmetric blast radius (full-cost loss on miss vs ≤0.50 max payout per
dollar). Mirrors the min_bot v2 retune from earlier today.

Constants:
- `MAX_BET_USD = 60.00` (was 45.00, paper_min_bot.py L263)
- `MAX_BET_BUY_YES_USD = 1.00` (was 5.00, paper_min_bot.py L279)

Backup: `paper_min_bot.py.bak.pre_caps_*`.

---

## Latest change (2026-05-09) — Vegas series-name typo fix (`KXLOWTLAS` → `KXLOWTLV`)

**Trigger:** thorough audit of "do all forecast fetchers use the exact Kalshi settlement station for every city" found min_bot had `KXLOWTLAS` (a non-existent series) hardcoded in 4 places where the real Kalshi ticker is `KXLOWTLV` (Vegas low-temp). Same bug class as the 2026-05-08 `KXLOWTDAL→KDAL` fix.

**Locations fixed:**
1. `paper_min_bot.py:413` in `_SERIES_TO_ICAO`
2. `paper_min_bot.py:600` in `PER_SERIES_D0_PRIMARY`
3. `tools/auto_select_per_series_primary.py:37` in `HARDCODED_D0_PRIMARY`
4. `tools/auto_select_per_series_primary.py:65` in `SERIES_TO_ICAO`

**Impact:** because the typo broke the ICAO lookup AND the d-0 hardcoded fallback, Vegas silently fell through `get_d0_primary` to the universal default `'hrrr'`. Live confirmation pre-fix on 2026-05-09 08:12 UTC: KXLOWTLV-26MAY09-B66.5 candidate had `mu_source='hrrr'` (mu=72.4) instead of the auto-selected `'nbp'` (mu=72.0). NBP is calibrated to be 0.18°F MAE better at d-0 (1.50°F vs 1.68°F, n=10) and 2.01°F MAE better at d-1 (1.50°F vs 3.51°F, n=10) — the bot was eating that whole gap on every Vegas decision.

**Fix verification:** post-restart at 08:12:40 UTC, next KXLOWTLV-26MAY09-T64 candidate had `mu_source='nbp_d0_override'` (mu=72.0). `get_d0_primary("KXLOWTLV")` returns `'nbp'`, `_SERIES_TO_ICAO["KXLOWTLV"]` returns `'KLAS'`.

**Regression test:** `tests/test_canonical_series_names.py` (10 tests) — verifies every per-series dict in the bot and the auto-select tool has only canonical KXLOWT* names with no typos, and that Vegas resolves end-to-end through the auto-primary chain. Future audits run on every PR.

**Audit also confirmed (no fix needed):** V1 + V2 high-temp bots, all 20 cities, every forecast fetcher (NWS, NBM, HRRR, MOS, NAM-MOS, WETHR, NBP, ECMWF, GFS), every primary-source override dict (`V1_PRIMARY_SRC_BY_SERIES`, `V2_PRIMARY_SRC_BY_SERIES`, manual overrides). Lat/lon distance from CITIES coords to ICAO airport coords ≤2 mi for all 60 (city, bot) cells (max 1.87 mi for Miami, all within HRRR/NBM 3 km grid resolution).

## Previous change (2026-05-08 late afternoon) — `KLAX_BUY_NO_HIGH_SIGMA` gate (live block + Discord notify)

**Trigger:** KLAX is the single worst station in the 14d extended pool: 1W:3L, **−$53.70**. All 4 trades are BUY_NO B-bracket on `nbp`, three losers had σ ≥ 2.5°F (5/1 −$29 σ=3.0, 5/2 −$24 σ=2.5, 5/3 −$17 σ=5.0); the only winner had σ=1.67 (4/30 +$16). The pattern survives the existing `PER_SERIES_SIGMA_MULT=2.5` (which inflates Kelly-shrink but doesn't gate entries).

**Filter:** block BUY_NO B-bracket when `series == "KXLOWTLAX" AND sigma >= 2.5°F`. Constant `LAX_BUY_NO_HIGH_SIGMA_THRESHOLD = 2.5` at `paper_min_bot.py:215`. Gate logic in both `_evaluate_gates` and `execute_opportunity`. Audit row `KLAX_BUY_NO_HIGH_SIGMA` to `bot_decisions.sqlite`; Discord notification to channel 1497464077608550570 (per-ticker 6h dedup).

**Mechanism:** Pacific coastal microclimate (marine layer push, fog, advection) is poorly modeled by HRRR/NBP/NBM relative to inland stations. The auto_select picker confirms NBP is the lowest-MAE source for KLAX d-0 (1.42°F MAE) — but even the best source still has 1.4°F+ error, and σ ≥ 2.5°F flags entries where the bot's Gaussian model is operating in the regime where forecast uncertainty exceeds what NBP-calibrated σ captures alone (the 2.5x PER_SERIES multiplier already amplifies σ proportionally — when σ_final ≥ 2.5°F it means the underlying source σ was ≥ 1.0°F, which is poor).

**Backtest** (extended pool n=71 decided): catches 3 records, **0W:3L**, lift +$69.90, robust +$40.74, helps:hurts 3:0. Below standard sample bar (n≥5) but mechanism is structurally clean and the cohort is narrowly station-targeted, so generalization risk is low. **Cross-station σ≥2.5 BUY_NO B-bracket rejected** because KMSP (3W:0L +$65), KDFW (3W:1L +$15), KPHX (3W:1L +$9) all show σ≥2.5 is a positive signal at non-Pacific-coastal stations. KLAX is the structural outlier.

**Risk:** the σ=1.67 winner (KLAX-26APR30-B56.5 +$16.20) would NOT be blocked at the 2.5°F threshold. If a future 1.7-2.4°F-σ KLAX BUY_NO appears as a winner, it is preserved. False-positive cost ceiling is ~$16/winner.

**Live-verified post-restart 18:35:32 UTC.** Commit pending.

## Previous change (2026-05-08 mid-day) — `BUY_NO_EXTREME_SIGMA` gate (live block + Discord notify)

**Trigger:** today's KXLOWTDEN-26MAY08-B41.5 BUY_NO loss (−$1.34, σ=8.34, mp=9.2%, mu=43.6 vs cap=42, actual 42 in bracket) — the only one of today's three losers with no existing filter coverage. CHI was caught structurally by the bias_corr disable; DAL was self-limited by thin-orderbook + market_stop. DEN stood alone.

**Filter:** block BUY_NO when `sigma >= 8.0°F AND model_prob < 10%`. Constants in `paper_min_bot.py:199-200`. Gate logic in both `_evaluate_gates` (eval-only) and `execute_opportunity` (live). When live gate fires, `_audit_skip` writes a `BUY_NO_EXTREME_SIGMA` row to `bot_decisions.sqlite` AND a per-ticker-deduped Discord notification is sent to channel 1497464077608550570 via new helper `_discord_skip_send` (6h re-log window).

**Mechanism (the actual reason high-σ BUY_NO loses):**

1. min_bot's σ starts at NBP-calibrated ~2-4°F, then compounds three multipliers:
   - `PER_SERIES_SIGMA_MULT`: KLAX=2.5×, KPHX=2.0×, **KDEN=1.5×**, KLV=1.5× (the 4 known-poor-MAE stations)
   - Disagreement inflation up to 1.5× when sources spread >2°F
   - NBP staleness up to 1.3× after 1h+
2. `σ ≥ 8` requires the compound case — i.e. **a known-bad-MAE station AND active source disagreement.** It marks the bot's WORST-known-uncertainty configuration.
3. The bot's `mp = Φ((cap-mu)/σ) - Φ((floor-mu)/σ)` is a Gaussian CDF. **As σ rises, P(in any narrow bracket far from mu) DECREASES** (the bell flattens, mass spreads). The bot reads the lower mp as "even safer to BUY_NO" — exactly the wrong direction.
4. Concrete: DEN today σ=8.34, mu=43.6, bracket [41,42]. Gaussian gives mp=4.6% with σ=8.34 vs 8.7% with base σ=4.0 — the inflation made the bot 47% MORE confident. Same with PHX-26MAY05 (σ=9.32, mp shifted from 5.2%→3.8%, also lost).

**Backtest (extended pool n=71 decided):** σ≥8 AND mp<10% on BUY_NO catches 2 historical records (PHX-26MAY05-B65.5 −$6.70, DEN-26MAY08-B41.5 −$1.34), 0W:2L, lift +$8.04. Below standard sample bar (n≥5) but mechanism is structurally clean and the cohort is so narrow that false-positive risk is small (~1-2 firings/month). Re-evaluate at n≥5 firings.

**Risk:** this WILL block some winners eventually. The σ ≥ 6 cohort is 4W:3L — wins exist when actual lands far from mu by chance. The threshold at 8 is calibrated to the rare compound-multiplier configuration where mp distortion is largest. Discord notification means each block is reviewable in real time.

**Why ship despite low n:** alternative ("wait for more samples") would let the next σ≥8 BUY_NO trade, which a structurally clean argument predicts is −EV due to Gaussian-in-fat-tailed-regime mismatch. Cost of a hard block on a winner: forfeit a few dollars. Cost of letting one through: full bracket loss ($30+).

**Forward audit:** Discord channel 1497464077608550570 receives one notification per ticker per filter trigger (6h dedup). `bot_decisions.sqlite` gets a `BUY_NO_EXTREME_SIGMA` decision row at every firing for backtest aggregation.

**Live-verified post-restart 17:33:44 UTC.** Commit pending.

## Previous change (2026-05-08 mid-day) — `USE_BIAS_CORRECTION = False`

`paper_min_bot.py:78`. n=4 bias_corr trades since 2026-05-05 deploy: 0 had entry decision flipped (Δmp 1-6pp can't escape [0.05, 0.85] BUY_NO band). Net pnl −$43 (W:L 2:2), dominated by forecast misses too big for bias to fix (CHI cluster 44°F vs actual 42°F). Position-sizing nudge of a few % is symmetric across W/L → near-zero net effect. Cron table refresh kept; re-enable trivial. Commit `53ec38a`.

## Previous change (2026-05-08 early-morning) — `_get_metar_running_min` LST-window fix (CRITICAL)

**Bug:** `_get_metar_running_min(station, climate_date)` used a `±12h pad` around UTC midnight, creating a **48-hour window** that pulled the PRIOR climate day's morning cooling cycle into today's query.

For KHOU on 2026-05-08, the bug pulled in 5/7 09:53 CDT awc/ldm reading of 64.94°F. `_cli_aligned_rmin` half-up rounded to 65. `_check_obs_confirmed_alive` then evaluated `rm=65 < floor=70 - 3°F = 67` → **TRUE** → fired `OBS_CONFIRMED_ALIVE` on `KXLOWTHOU-26MAY08-B70.5`, bypassed all forecast gates, kelly×1.5 boost, entered 13 BUY_NO contracts at $11.61. **Today's actual rm was 73.4** — bracket [70, 71] was reachable. Same leak hit `KXLOWTDAL-26MAY08-B60.5` via `calc_bracket_probability_min`'s "bracket impossible" guard returning 0.0 on yesterday's KDFW morning low.

**Fix:** new helper `_lst_climate_window_utc(station, climate_date)` computes the proper per-station LST climate-day window via the Jan-15 (standard-time-only) UTC offset trick. KHOU 5/8 = `[2026-05-08T06:00 UTC, 2026-05-09T06:00 UTC]` (24h, CST=UTC-6). `_get_metar_running_min` updated to use it.

Failure mode documented in `memory/feedback_per_station_lst_climate_window.md`. Bug live since `USE_CLI_ALIGNED_RMIN = True` (2026-05-06).

**Live-verified post-restart 07:07:34 UTC:** zero `OBS_CONFIRMED_ALIVE` firings across all stations. Pre-fix, HOU was firing every cycle. Commit `a7e5c9d`.

`tests/test_lst_climate_window_fix.py`: 13 OK.

## Previous change (2026-05-07 evening) — `OBS_CONFIRMED_LOSER` hygiene check (BUY_NO)

Added a `current_obs < running_min - 0.5°F → skip LOSER` sanity check in `_check_obs_confirmed_loser` BUY_NO branch. New helper `_get_current_temp_f(station)` reads the latest `temp_f` from `obs.sqlite.observations`.

**Why:** when bot's rm is ABOVE the latest observed temp, rm is stale or sourced differently from live obs (cli-aligned RM occasionally lags raw METAR briefly during cooling). The LOSER block is unreliable in that state. Backtest 2026-04-23..05-07 (n=20 BUY_NO/B first-fires): 1 false-positive caught (`KLAX-26MAY06-B56.5`, cobs=55.4 < rm=57.0), **0 correct-blocks lost.**

**Honest scope:** small fix. ~$25/14d ≈ $1.80/day expected lift. Below the standard "lift > $110" filter validation bar — but it's a *loosening* of an existing block (zero-hurt by construction), not a new gate, and the mechanism is interpretable. Other false positives in the gate (~4/14d) are unfilterable from real-time signals (cooling resumes after a stable window, METAR's 5–30-min sampling cadence misses brief sub-cycle dips).

`tests/test_obs_loser_hygiene.py`: 10 OK.



## Latest change (2026-05-07 early-morning) — NBP fetcher: NCEP fallback + Last-Modified caching (V2 port)

`_nbp_fetch_latest_bulletin()` was S3-only (`noaa-nbm-grib2-pds`). When the AWS S3 mirror lagged behind NCEP nomads (e.g. 5/7 01Z 4h+ late at S3 while nomads already had it), min_bot was forced onto a stale fallback cycle and fired Discord `STALE CACHE FALLBACK NBP` alerts. V1+V2 both already used NCEP-first parallel HEAD; this ports the same pattern to min_bot.

Three changes:

1. **Parallel HEAD** against `https://nomads.ncep.noaa.gov/...` and S3 — first 200 wins. NCEP typically publishes ~90s ahead of S3 mirror, and S3 outages don't block NBP fetch entirely.
2. **Last-Modified caching** — `_nbp_last_modified[url]` tracks each bulletin's LM header. When the cycle hasn't advanced (typical fall-through during the natural 6h inter-cycle gap), the bot doesn't re-download the 33MB bulletin.
3. **Heartbeat on unchanged-bulletin** — bumps `_nbp_cache_ts` even when no new download happens, so the stale-cache watchdog doesn't false-alarm when the bot is healthy but no new cycle has dropped.

**Subtle httpx quirk discovered:** `httpx.head(url, follow_redirects=True)` and `httpx.request("HEAD", url, follow_redirects=True)` both silently return 302 without following. Only `httpx.Client(follow_redirects=True).head(url)` actually follows. NCEP nomads returns 302 for blend URLs, so without a Client instance the helper would silently fall back to S3-only. Module-level `_NBP_HTTP = httpx.Client(timeout=30.0, follow_redirects=True)` is now used for all NBP HEAD probes and bulletin GETs.

**Live-verified post-restart 06:15 UTC:** cold-start grabbed 5/6 19Z via S3 (most-recent-immediately-responsive cycle), then poller advanced to **5/7 01Z via NCEP** within ~3 min. Cache `cycle_dt` now `2026-05-07T01:00:00+00:00`. Same 2-step pattern V2 uses (cold-start grabs whatever returns first, poller catches up).

`tests/test_nbp_ncep_fallback.py`: 9 OK. Re-evaluate ~2026-05-21.

## Previous change (2026-05-07 early-morning) — `refresh_nbm_om_forecasts` model fix

`refresh_nbm_om_forecasts()` was passing `model="best_match"` to Open-Meteo since min_bot's first commit. `best_match` is OM's auto-picker — for US points short-range it returns HRRR, longer-range GFS/ICON. So the function named "NBM-OM" was actually returning HRRR-equivalent data for d-0, never NBM. The bug was masked while the free endpoint throttled (cache-lag made the streams look different by accident); the 5/5 customer-api fix removed the throttle and exposed it. By 5/7 morning, `mu_nbm_om == mu_hrrr` 100% of the time across 34,764 candidate rows.

**Impact:** the bot's "3-source consensus" was effectively 2 sources. `H_2_DISAGREE`, `MSG`, `mu_blended`, and the new auto-select cron all double-counted HRRR.

**Fix:** one-line — `model="best_match"` → `model="ncep_nbm_conus"` (matches V1/V2). Plus a regression test that `'model="best_match"'` is not present in source.

**Live-verified post-restart 05:12 UTC:** 0/120 candidates identical (vs 100% before). Real NBM is generally 2-6°F warmer than HRRR on tonight's mins — e.g. KMIA HRRR 71.2 / NBM 75.4, KMSP HRRR 32.0 / NBM 37.6.

Auto-select cron at 14:30 UTC will now see real NBM-vs-HRRR MAE for the first time. Re-evaluate ~2026-05-21.

## Previous change (2026-05-07 early-morning) — `STALE_BRACKET` gate

`execute_opportunity` now refuses any opp where `_days_out_int(opp) < 0`. Kalshi keeps markets tradeable until CLI publishes, which can lag bracket settlement by days — saw 4 `ENTRY_FILLED` audit events at 04:10 UTC 2026-05-07 firing on `26APR28-B45.5` / `26MAY01-T46/T56` (`days_out=-5` to `-8`). They never actually filled on Kalshi, but the retry churn is wasted work and the audit log was misleading. Gate sits at the top of `execute_opportunity` (before addon-eligibility), so neither fresh entries nor add-ons fire on stale brackets. `days_out=None` (parse failure) falls through unchanged. Live-verified: 1 `STALE_BRACKET` row in bot_decisions within 2 min of restart.

`tests/test_stale_bracket_gate.py`: 6 OK. Re-evaluate ~2026-05-21.

## Previous change (2026-05-06 afternoon) — auto-select per-series forecast source

Replaces the manual every-few-days `PER_SERIES_D{0,1}_PRIMARY` recalibration with a daily cron that picks the lowest-MAE source per (station, days_out) cell. Same methodology as the manual 5/4 audit, automated.

### How it works

1. **Daily cron at 14:30 UTC** runs `tools/auto_select_per_series_primary.py`:
   - Pulls last 14 days of (`mu_nbp`, `mu_hrrr`, `mu_nbm_om`) from candidate logs
   - Pairs with actual rm via CLI (preferred) or `running_min` (fallback)
   - Computes **exponentially-weighted MAE** per source (half-life=7d, recent days dominate)
   - Picks the lowest-MAE source per cell with **hysteresis** (must beat current by ≥0.30°F AND ≥1.20× relative; min n=5; sanity floor 4°F MAE)
   - Writes `data/auto_primary_selection.json`
   - Appends a per-run summary to `data/auto_primary_log.jsonl`

2. **Bot reads JSON each cycle** via `get_d0_primary(series)` / `get_d1_primary(series)`. Hot-reload on file mtime change. Cache invalidated if `computed_at` > 36h old.

3. **Resolution chain (highest priority wins):**
   1. `MANUAL_PRIMARY_OVERRIDES_D{0,1}` — operator pin in code (empty by default)
   2. `auto_primary_selection.json` — daily cron
   3. `PER_SERIES_D{0,1}_PRIMARY` — hardcoded baseline (still used as fallback)
   4. `'hrrr'` (d-0) or `'nbp'` (d-1) — ultimate default

### First-run effect

The 5/6 first run flipped exactly **one cell**: KSEA d-0 NBP → HRRR (1.78°F → 1.16°F MAE, ratio 1.54×, n=9). Triggered by NBP's persistent +2°F warm bias at KSEA over the last 5 days (caused yesterday's SEA-B51.5 −$29.44 loss and today's open SEA-B50.5 −$15ish loss in progress). Every other cell stayed put.

### Anti-flap protections

| Risk | Mitigation |
|---|---|
| Source flap-flop on noise | Switch requires ≥0.30°F absolute AND ≥1.20× relative improvement |
| Over-react to recent regime | 7-day half-life damps but emphasizes recent (today w=1.00, day-7 w=0.50, day-14 w=0.25) |
| Brief data gap → spurious switch | min_samples=5 per source per cell; below that, keep current |
| Cron fails / stale JSON | Bot falls back to hardcoded `PER_SERIES_D{0,1}_PRIMARY` if JSON age > 36h |
| All sources broken (MAE > 4°F) | Sanity floor — keep current, don't switch |
| Operator wants to pin a station | `MANUAL_PRIMARY_OVERRIDES_D{0,1}` dict in code wins over auto |

### Tests

- `tests/test_auto_primary.py`: 11 OK — fallback chain, mtime reload, staleness boundary, manual override priority, malformed JSON resilience.
- `tests/test_paper_min.py`: 384 OK + 1 skip + 1 pre-existing unrelated failure (`TestBiasCorrectionWiring.test_no_applicable_min_cells_today`, marked "Remove this test" — fails on baseline too).

### Verification (live, 20:17 UTC)

Post-restart, `KXLOWTSEA-26MAY06` d-0 candidates show `mu_source=hrrr`, `mu=51.2` (HRRR's value) instead of `mu_source=nbp_d0_override`, `mu=53.0` (the previous NBP value). The auto-select chain is live.

### Safety fixes (2026-05-06 evening)

Two robustness improvements after edge-case audit:

1. **Atomic JSON write** in `tools/auto_select_per_series_primary.py` — write to `OUTPUT_JSON.tmp` then `os.replace()` (atomic POSIX rename). Prevents the bot from reading a half-written file mid-cron.

2. **Retry-storm guard** in `_maybe_reload_auto_primary()` — on JSON parse failure or `computed_at` parse failure, bump `loaded_mtime` to the current file mtime so we skip retries on the same corrupt file until the next cron rewrite. Previously every cycle would re-read+re-parse the same bad file.

No behavior change in normal operation. 11 tests still pass.

Backups: `paper_min_bot.py.bak.pre_autoselect_20260506-200615` + parallels.

Re-evaluate ~2026-05-20.

## Previous change (2026-05-06 mid-morning) — `MAX_EDGE` 0.50 → 0.70 after sweep audit

Follow-up sweep on the same 8d resolved-candidate pool (n=2,125, deduped by ticker+action with outcomes attributed via `obs.sqlite running_min`). With the new `MIN_MODEL_PROB=[0.05, 0.85]` band already in place from the morning's deploy:

- Cumulative BUY_NO `sum_pnl` at `MAX_EDGE=0.45` was +$27.39
- At `MAX_EDGE=0.50` (this morning's deploy): +$27.94
- At `MAX_EDGE=0.70`: **+$28.89 — the peak**, flat through 1.00
- The single freed candidate above 0.50 is at edge 0.65–0.70, and it **won**. Zero losers in any cell ≥ 0.50 within the [0.05, 0.85] mp band

So 0.70 captures the marginal lift with no observed downside. Small absolute lift (~$0.12/day expected on this sample), but it's a strict improvement on the data — every cell in the MIN×MAX grid above MAX_EDGE=0.50 is monotonically ≥ 0.50.

**Why not even higher (0.85, 1.00)?** The data is flat above 0.70 — no additional candidates surface. 0.70 is the natural stopping point.

**Why not raise `MAX_MODEL_PROB`?** Confirmed asymmetric: zero BUY_NO candidates exist at mp > 0.85 (DIRECTIONAL_BUY_NO_MAX_MP=0.20 + edge math both block). The 3 BUY_YES candidates at mp ≥ 0.85 in the 8d pool all lost (−$1.69 freed pool). MAX_MODEL_PROB stays at 0.85.

### Tests
- `tests/test_paper_min.py`: 384 OK + 1 skip — value-pin updated to `MAX_EDGE == 0.70`; 4 boundary tests bumped from edge=0.55 → 0.75 to remain unambiguously above the new cap
- `tests/test_model_market_disagree.py`: 24 OK
- 5 pre-existing ladder-test failures (`test_ladder_chase_20260503`, `test_ladder_no_fill_continue_20260504`) unchanged and unrelated

Backups: `paper_min_bot.py.bak.pre_maxedge070_20260506-073759`, `tests/test_paper_min.py.bak.pre_maxedge070_20260506-073759`, `README.md.bak.pre_maxedge070_20260506-073759`.

Re-evaluate ~2026-05-20.

## Previous change (2026-05-06 morning) — Overfilter rollback: `MIN_MODEL_PROB` 0.15→0.05, `MAX_EDGE` 0.45→0.50, `COASTAL_TIGHT_FLOOR` disabled

A 5/4–5/6 audit (8 days of `bot_decisions.sqlite` rows + `trades_*.jsonl` candidates joined to `obs.sqlite running_min` for actual outcomes) found three gates were costing more than they were saving in the current regime. All three changed today.

**1. `MIN_MODEL_PROB` 0.15 → 0.05** — `MP_RANGE` blocked **8,022 candidates over 3 days**. Sample of 31 → ~28 would have **won**. The bot was killing its own deep-tail BUY_NO channel where the model's signal is strongest (mp 3–14% means model says 3–14% chance of being in bracket, and the model is right ~90% of the time). V2 uses 0.03; min_bot now at 0.05 for marginal conservatism. **Update 2026-05-10:** F2A rule A (`mp < F2A_PROB_LO`) was also disabled, so the full mp 0–5% deep tail is now open — see F2A row below.

**2. `MAX_EDGE` 0.45 → 0.50** — At the 0.45 cap (since 4/29), 5/4–5/6 blocks were **21 winners / 9 losers** (70% would-win rate). The 4/29 rollback to 0.45 was based on an n=5 BUY_NO-bypass-loss observation; current regime has the model correctly identifying deep-tail BUY_NO winners at high apparent edge. V2 raised 0.45→0.50 on 5/3 (P3 of filter audit, +$418 era-wide, h:h 5:2) — this is a parity change. min_bot has the V2 downstream catchers (`MODEL_MARKET_DISAGREE` shipped 5/4, `OBS_CONFIRMED_LOSER` long-standing).

**3. `COASTAL_TIGHT_FLOOR` disabled via `_COASTAL_TIGHT_FLOOR_ENABLED = False`** — Original 5/3 backtest (n=9) showed h:h 6:0 (6 losses prevented). Live 5/4–5/6 result: 7 BUY_NO blocks, **6 won, 1 lost** — gate is regime-flipped. Cool-front shifted coastal lows out of brackets, breaking the marine-cold-bias premise. Predicate preserved (`COASTAL_TIGHT_FLOOR_STATIONS`, `COASTAL_TIGHT_FLOOR_MIN_GAP_F`) for fresh sliding-window re-enable. Re-evaluate ~2026-05-13.

**Why this matters:** entry rate had dropped from ~10/day (4/29–5/3) to 4–7/day (5/4–5/6) with PnL flipping from +$15–50/day realized to −$3/day. The combination of these three gates was suppressing the bot's profitable BUY_NO B-bracket channel (era-wide n=28, W=25, L=3, +$164). Estimated overfilter cost: $200–500/day in missed profit.

### Tests
- `tests/test_paper_min.py` — 384 OK (`test_low_mp_passes_when_nbp_consistent` re-enabled 2026-05-10 after F2A_PROB_LO disabled)
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

**2. Ladder extended from BUY_NO-only to BUY_YES.** AUS-MAY04 BUY_YES showed the same 89% orphan rate as BUY_NO cases, but the 2026-05-03 ladder deploy was BUY_NO-only. Now action-aware: BUY_NO chases `no_ask_dollars` against `MAX_BET_USD` ($60) cap with edge `1 − mp_yes − price`; BUY_YES chases `yes_ask_dollars` against `MAX_BET_BUY_YES_USD` ($1) cap with edge `mp_yes − price`. Add-ons still iterate via the existing scan loop, unchanged.

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
| `MAX_EDGE` | `0.70` | Skip edges > 70%. Evolution: 0.40 → 0.42 (4/27) → 0.55 (4/28) → 0.45 (4/29) → 0.50 (5/6 morning) → **0.70 (5/6 mid-morning)**. Sweep with new MP_RANGE in place: 0.50→0.70 freed 1 BUY_NO candidate that won (+$0.95/8d), zero losers above 0.50. No bypass. |
| ~~PRICE_ZONE~~ | ~~yes_bid 30-40c~~ | **REMOVED 2026-04-29** after gate-audit on min_bot historical candidates: 2/2 PRICE_ZONE-blocked cases were winners (NYC-26APR25-B42.5 BUY_NO @66¢ → +$0.34/c; ATL-26APR27-B59.5 BUY_NO @67¢ → +$0.33/c). V2's max-temp finding (50% WR / −$99 / n=50) does not appear to hold for min-temp markets. Sample is small (n=2) but 100% winners and gate-cost is non-trivial; re-enable from git history if a future audit flips the signal. |
| **H_2.0** | `disagreement 4.5°F` | **BUY_NO d-1+ only** (V2-inspired). Skip when pairwise forecast disagreement (NBP/HRRR/NBM max diff) > 4.5°F on day-1+ markets where we have no obs to break ties. **Raised 2.0 → 4.5 on 2026-05-10** per stack-aware audit (n=109 historical blocks → 81 resolved unique kills after netting overlap with MU_NEAR_BELOW_BRACKET / BUY_NO_EXTREME_SIGMA / SPREAD / KLAX_HIGH_SIGMA). At 2.0°F the gate blocked 52W:29L (64% winners) with LOO-1 robust −$1.60/c — one day carrying all the lift. Threshold sweep showed 2-4°F is noise (low-disag bins 20W:4L net positive = over-blocking); 5°F+ is signal where the asymmetric loss profile of d-1+ BUY_NO B-brackets makes the remaining blocks net-negative. T=4.5 passes all 5 playbook bars: n=39 released, net +$3.73/c (~+$280/12d at $45 bet), robust +$2.23/c, helps:hurts 7:3. Bypassed by `_obs_confirmed_alive`. |
| `MIN_MODEL_PROB` / `MAX_MODEL_PROB` | `0.05` / `0.85` | Skip wildly unlikely or near-certain. Lowered 0.15→0.05 on **2026-05-06** after audit found 8,022 blocks / 3 days, ~90% would-win rate — bot was killing its own deep-tail BUY_NO channel. **Bypassed by `_nbp_consistent_with_recent_cli`** when forecast μ is within ±2°F of the station's last-7-day CLI low range. (Bypass was previously near-moot for the lower side because F2A_PROB_LO=0.05 blocked at the same threshold; F2A rule A now disabled 2026-05-10 so the bypass is meaningful again.) |
| **Directional consistency** | `mp 0.20 / 0.60` | BUY_NO requires `mp ≤ 0.20`; BUY_YES requires `mp ≥ 0.60`. Don't bet against your own model. **Evolution**: 0.50 (initial) → 0.40/0.60 (2026-04-27, CHI-T41 mp=34% / NYC-T44 mp=42% BUY_YES losers triggered) → **BUY_NO 0.40 → 0.20** (2026-04-29 night, V2 port). 2026-04-29 audit on deduped n=42 BUY_NO pool: trades with mp ∈ (0.20, 0.40] had 12L:1W, lift +$5.16 over no-filter; every mp bucket above 0.20 is net negative. April 29 forward-test: of 13 entries, only LAX-30-B56.5 would have been newly blocked (currently coin-flip; small + impact). BUY_YES side kept at 0.60 — symmetric tightening to 0.80 not yet validated on min_bot. mp_range_bypass (NBP-CLI consistency) applies to MIN/MAX_MODEL_PROB only, NOT to directional consistency. |
| `MIN_ABS_DISTANCE_F` | `0.5°F` | **BUY_NO only** — skip when `\|mu − bracket_mid\| < 0.5°F` (mu inside or near bracket center). 1.0 → 1.5 (2026-04-27 AM) → reverted to 0.5 (2026-04-27 PM) after Kalshi-truth audit on n=15: at 1.5°F we'd have blocked 9/9 winners with dist 0.5–1.5°F (BUY_NO with mu *at the bracket edge*, not inside). PHIL-B44.5 (0.1°F, mu inside) is still caught at 0.5°F. BUY_YES intentionally not gated. |
| **F2A asymmetry gate** | sigma only | **BUY_NO only** (V2 port). Currently fires on `sigma < 1.5°F` (over-confident model) only. **Rule A disabled 2026-05-10** (`F2A_PROB_LO = 0.05 → 0.0`): stack-aware audit on 12d showed 15W:1L (94% wr) unique kills on the `mp < 0.05` branch, net +$3.36/c (~+$168/12d at $45 bet), LOO-1 robust +$2.22/c, helps:hurts 7:0. Mirrors V2 max bot's narrowing on 2026-04-30 (commit `84c7878`) which removed both prob branches on identical evidence (9W:0L, +$42.53/13d freed) and has run stably for 11+ days. Rule B (`mp ≥ 0.30`) is dead code: `DIRECTIONAL_BUY_NO` catches `mp > 0.30` strictly first, leaving F2A's `mp ≥ 0.30` to fire only on exact `mp == 0.30`. Sub-rule C (`sigma < 1.5°F`) kept: only 6 unique kills 4W:2L with LOO-1 −$0.22, marginal — same call V2 made. V2's distance-from-edge sub-check was never ported. Bypassed when `_obs_confirmed_alive`. |
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
