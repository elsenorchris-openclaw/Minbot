# MIN_BOT Backtest Playbook

Read this **before proposing any filter, threshold, or gate change** to
`paper_min_bot.py`. Same idea as V2's `BACKTEST_PLAYBOOK.md` but scaled for
min_bot's smaller volume and shorter history.

## TL;DR — what passes, what doesn't

A filter / threshold change is acceptable when it clears **all five** bars:

1. **n_blocked ≥ 20** distinct settled candidates that the change affects
2. **Net P/L lift ≥ +$30** (per-contract) on the affected set
3. **Robust lift ≥ +$15** with the largest single day removed (LOO-1)
4. **Helps:Hurts ≥ 4:2** across days (or stations, whichever has ≥ 6 buckets)
5. **Mechanism is articulable** — you can explain in one sentence *why* the
   change captures edge or removes a real failure mode

Below 20 the answer is **"come back when there's more data."** Don't mistake
random noise for signal.

## Why these numbers (and not V2's)

V2's bar:
- lift > **+$110**, robust > **+$50**, n_kept ≥ **130**, helps:hurts ≥ **6:4**.

Min_bot is roughly 1/4 of V2's volume (lower bankroll, smaller bets, fewer
markets eligible per cycle). Scaling V2's thresholds by ~1/4 gets you to
this playbook's numbers. Shorter-history-mean-thinner-evidence is a real
constraint — be humbler about claims, not louder.

## How to run the audit

```bash
ssh -i ~/.ssh/kalshi-bot-key ubuntu@54.225.174.220
cd /home/ubuntu/paper_min_bot
python3 tools/gate_audit.py --list-gates           # known gate names
python3 tools/gate_audit.py                        # all gates summary
python3 tools/gate_audit.py --gate PRICE_ZONE      # detail on one gate
python3 tools/gate_audit.py --action BUY_NO        # filter by action
python3 tools/gate_audit.py --days 7               # last 7 days only
python3 tools/gate_audit.py --include-active       # also poll Kalshi /markets
                                                   # (slow; needed for cases
                                                   # not in /portfolio history)
```

Gate names: `WOULD_ENTER`, `NO_ACTION`, `MIN_EDGE`, `OBS_CONFIRMED_LOSER`,
`MAX_EDGE`, `MP_RANGE`, `DIRECTIONAL_BUY_NO`, `DIRECTIONAL_BUY_YES`,
`ABS_DIST`, `F2A`, `MSG`, `H_2_0`, `MAX_DISAGREEMENT`, `MU_VS_RM`, `SPREAD`.

The `blocked_by` field is now stamped on every candidate at write time
(commit `f2bc531`, 2026-04-29) so the tool reads it directly when present
and falls back to recomputing via `paper_min_bot._evaluate_gates(opp)` for
older records.

## Output: how to read it

Each gate gets a line:

```
MAX_DISAGREEMENT     n=  2 wr=100% avg_pnl=$+0.485/c roi=+94.2%
```

- `n` — distinct candidates the gate blocked (or would have blocked) with
  resolved settlement
- `wr` — fraction that **would have won** if the gate hadn't fired
- `avg_pnl` — mean per-contract P/L if entered at the recorded entry_price
- `roi` — `pnl / cost` (per dollar deployed)

The "costing us" marker fires when net P/L of the blocked set is **positive** —
removing the gate would have made money.

## Common mistakes

- **Treating "100% wr on n=2" as proof.** It's not. n=2 is a coin-flip away
  from n=0. Wait for n ≥ 20 before acting.
- **Comparing absolute P/L without normalizing for size.** The bot's bet
  sizes have changed 5× in the past week. Use ROI or per-contract P/L, not
  total dollars.
- **Cherry-picking a single day.** Always check the LOO-1 (drop biggest day)
  bar. If removing one day flips the sign, you're chasing noise.
- **Forgetting bot has different gates by day-offset.** d-0 has running_min;
  d-1+ doesn't. H_2.0 only fires on d-1+. Audit per day-offset when relevant.
- **Skipping the mechanism check.** "It works in the data" without a story
  about *why* it works = overfitting. Statistical fit alone is not enough.

## When in doubt, hold

The bot has been live for a week. Most decisions can wait another week for
n to grow. Premature filter changes have cost min_bot $0 so far — but only
because the changes were small. As the bot scales, the cost of overfit
filters compounds.

If a candidate filter passes 4 of 5 bars but fails one, it's a **shadow**
candidate, not an active deploy. Add the logic in code with a flag set to
"shadow only" — collect outcome data for another week — then re-evaluate.

## Where to look when something looks broken

- **`data/trades_YYYY-MM-DD.jsonl`** — every candidate with `blocked_by`
- **`data/settlements.jsonl`** — settled outcomes
- **`data/positions.json`** — currently open positions (check
  `_recovered_from_kalshi` / `_recovered_from_trades_log` flags)
- **`logs/min_bot_YYYY-MM-DD.log`** — runtime log (skip-debounced)

## Coordination

- This playbook tracks **min_bot only**. V2's playbook
  (`~/obs-pipeline-bot/BACKTEST_PLAYBOOK.md`) covers max-temp markets and
  has different thresholds.
- A signal observed on V2 does not necessarily transfer to min_bot.
  PRICE_ZONE is the canonical example: V2 backtest validated the gate
  (50% WR / −$99 / n=50 on max-temp); min_bot audit invalidated it
  (2/2 winners blocked on min-temp). Min markets price overnight cooling
  signals; max markets price daytime warming signals — different physics
  in the same band.
- When porting a V2 finding to min_bot: re-run the audit on min_bot data
  before deploying. Don't assume.
