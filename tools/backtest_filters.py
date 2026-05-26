#!/usr/bin/env python3
"""min_bot backtest engine — analyze filter EV with proper stacked accounting.

Mirrors V2's tools/backtest_filters.py and V1's tools/v1_backtest_filters.py.
Each candidate (or custom) predicate is measured INCREMENTALLY on top of the
deployed live filter chain, so standalone-overstates-lift overlap bugs are
caught at the validation step.

Data path: the pool is the bot's REALIZED trades, reconstructed per ticker as
entry-time features (kind=entry in trades_*.jsonl) joined to realized pnl =
Σ(kind=exit pnl) ∪ Σ(settlements.jsonl pnl). 2026-05-25: this REPLACED a
settlements.jsonl-only loader that was survivorship-biased — the bot exits
intraday (time_exit/take_profit/market_stop/hard_stop), so stopped-out losers
are logged as exits and never settle, and the old pool silently dropped them
(saw 29/29 thin-margin winners but only 9/28 losers). This is still a
POST-LIVE-CHAIN pool: every record already passed every deployed gate, so
stack predicates fire RETROACTIVELY ("if today's chain were applied to these
entries, this is what would have been blocked"). won = realized money outcome
(pnl > 0).

For forward filter validation: the stack returns False on every filled
record (they all passed), so `lift_inc` ≈ `lift_naive`. The stack interface
is still load-bearing because (a) it makes the incremental framing
explicit, (b) it's the deploy gate when adding new scenarios, (c) future-
proofs for when min_bot adds a per-cycle candidate log analogous to V1's
weather_candidates_*.jsonl.

Usage:
  python3 tools/backtest_filters.py --pool-stats
  python3 tools/backtest_filters.py --scenario list
  python3 tools/backtest_filters.py --scenario obs_confirmed_loser --stack live
  python3 tools/backtest_filters.py --custom my.py:should_skip --stack live
"""
import argparse
import glob
import gzip
import importlib.util
import json
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path("/home/ubuntu/paper_min_bot")
SETTLEMENTS_PATH = ROOT / "data" / "settlements.jsonl"
TRADES_PATH = ROOT / "data" / "trades.jsonl"

DEFAULT_SINCE = "2026-04-15"
DEFAULT_MAX_BET_NORM = 30.0  # min_bot's MAX_BET_USD


# ─────────────────────────────────────────────────────────────────────────────
# Data loading
# ─────────────────────────────────────────────────────────────────────────────
_BOT_REC_DATE_RE = re.compile(r"trades_(\d{4}-\d{2}-\d{2})\.jsonl")


def _iter_bot_records(since_date):
    """Yield the bot's own kind=entry / kind=exit records out of the daily
    trades_YYYY-MM-DD.jsonl[.gz] logs, for dates >= since_date.

    These logs are huge (raw market ticks); the bot's own records are a tiny
    fraction. For plain .jsonl we shell out to grep (≫ faster than a Python
    scan of 300MB/file); .gz are read inline."""
    files = sorted(
        glob.glob(str(ROOT / "data" / "trades_*.jsonl"))
        + glob.glob(str(ROOT / "data" / "trades_*.jsonl.gz"))
    )
    day_lo = since_date[:10]
    for f in files:
        m = _BOT_REC_DATE_RE.search(f)
        if not m or m.group(1) < day_lo:
            continue
        lines = []
        if f.endswith(".gz"):
            try:
                with gzip.open(f, "rt") as fh:
                    lines = [ln for ln in fh
                             if '"kind": "entry"' in ln or '"kind": "exit"' in ln]
            except Exception:
                lines = []
        else:
            try:
                lines = subprocess.run(
                    ["grep", "-hE", r'"kind": "(entry|exit)"', f],
                    capture_output=True, text=True, timeout=180,
                ).stdout.splitlines()
            except Exception:
                lines = []
        for ln in lines:
            try:
                yield json.loads(ln)
            except Exception:
                continue


def load_settled(since_date=DEFAULT_SINCE, normalize_bet=DEFAULT_MAX_BET_NORM):
    """Load min_bot's REALIZED trades, keyed by ticker.

    2026-05-25 SURVIVORSHIP-BIAS FIX: the bot does intraday exits
    (time_exit_10am / take_profit / market_stop / hard_stop), so a
    stopped-out loser is logged as a kind=exit in trades_*.jsonl and NEVER
    gets a settlements.jsonl record. The old loader read settlements.jsonl
    only → it saw the winners (held to settle) but dropped the stopped
    losers. On the May data it saw 29/29 thin-margin winners but only 9/28
    losers, flipping a true −$307 filter into a tool-visible +$73 and
    reporting a good entry filter as harmful. Any entry filter that
    correlates with stop-outs was mis-measured.

    Now: realized pnl per ticker = Σ(exit pnl) + Σ(settlement pnl), with
    entry-time features taken from the kind=entry record (richest feature
    set). Settlement-only tickers (held to expiry, or recovered/obs_pipeline
    sourced with no entry log in window) are still included. won is the
    realized money outcome (pnl > 0), since that is what filter validation
    should optimize once exits are in play."""
    entries = {}
    exits = defaultdict(list)
    for r in _iter_bot_records(since_date):
        t = r.get("market_ticker")
        if not t:
            continue
        if r.get("kind") == "entry":
            entries.setdefault(t, []).append(r)
        elif r.get("kind") == "exit":
            exits[t].append(r)

    setl = defaultdict(list)
    if SETTLEMENTS_PATH.exists():
        for line in SETTLEMENTS_PATH.read_text().splitlines():
            try:
                s = json.loads(line)
            except Exception:
                continue
            if s.get("ts", "") < since_date:
                continue
            tk = s.get("market_ticker")
            if tk:
                setl[tk].append(s)

    def _dedup_settle_pnl(recs):
        # the .bak settlement files can double-list a settlement; dedup on
        # (rounded pnl, day) before summing.
        seen, tot = set(), 0.0
        for s in recs:
            if s.get("pnl") is None:
                continue
            key = (round(float(s["pnl"]), 2), s.get("ts", "")[:10])
            if key in seen:
                continue
            seen.add(key)
            tot += float(s["pnl"])
        return tot

    def _finalize(base, pnl, cost):
        rec = dict(base)
        if normalize_bet is not None and cost and cost > normalize_bet:
            scale = normalize_bet / float(cost)
            cost = float(normalize_bet)
            pnl = float(pnl) * scale
            rec["_normalized"] = True
        rec["pnl"] = float(pnl)
        rec["won"] = float(pnl) > 0
        rec["cost"] = float(cost)
        rec["kelly_bet"] = float(cost)
        rec["date"] = rec.get("date_str") or rec.get("ts", "")[:10]
        return rec

    pool = []
    seen_tickers = set()
    for t, erecs in entries.items():
        if erecs[0].get("action") not in ("BUY_NO", "BUY_YES"):
            continue
        base = sorted(erecs, key=lambda x: x.get("ts", ""))[0]
        ex, st = exits.get(t, []), setl.get(t, [])
        pnl_ex = sum((e.get("pnl") or 0) for e in ex)
        pnl_st = _dedup_settle_pnl(st)
        if not ex and not st:
            continue  # still open — no realized outcome
        cost = sum((e.get("cost") or 0) for e in erecs) or base.get("cost", 0.0)
        pool.append(_finalize(base, pnl_ex + pnl_st, cost))
        seen_tickers.add(t)

    # Settlement-only tickers with no entry log in the window (held to expiry,
    # or recovered/obs_pipeline-sourced). Preserve the old pool's coverage.
    for t, st in setl.items():
        if t in seen_tickers:
            continue
        base = sorted(st, key=lambda x: x.get("ts", ""))[0]
        if base.get("action") not in ("BUY_NO", "BUY_YES"):
            continue
        if base.get("pnl") is None:
            continue
        pool.append(_finalize(base, _dedup_settle_pnl(st), base.get("cost") or 0.0))

    return pool


def compute_pool_stats(pool, label="pool"):
    n = len(pool)
    if n == 0:
        return {"label": label, "n": 0}
    won = sum(1 for r in pool if r["won"])
    pnl = sum(r["pnl"] for r in pool)
    cost = sum(r["kelly_bet"] for r in pool)
    return {
        "label": label, "n": n, "won": won, "win_rate": won / n,
        "total_pnl": pnl, "total_cost": cost, "roi": (pnl / cost) if cost > 0 else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stack scenarios — predicates replicating each currently-deployed min_bot
# entry filter. Each predicate(record) -> bool; True = block.
#
# When a NEW filter is deployed in min_bot, add it here AND to LIVE_CHAIN
# below in the same commit. LIVE_CHAIN is the deploy gate — partial stacks
# silently overstate lift_inc when the candidate overlaps an unlisted
# deployed filter.
# ─────────────────────────────────────────────────────────────────────────────

def _model_prob_out_of_range(t):
    """MODEL_PROB_OUT_OF_RANGE — block when model_prob outside acceptable
    range. min_bot's bands depend on action/bracket-type; conservative
    catch-all here is mp not in [0.03, 0.97]."""
    mp = t.get("model_prob")
    if mp is None:
        return False
    return mp < 0.03 or mp > 0.97


def _max_edge_exceeded(t):
    """MAX_EDGE_EXCEEDED — block when edge exceeds 0.70 (paper_min_bot.py:145).
    Indicator of model/market mispricing too good to be true."""
    edge = t.get("edge")
    return edge is not None and edge > 0.70


def _directional_buy_no_max_mp(t):
    """DIRECTIONAL_NO_DISAGREE — BUY_NO with model_prob > 0.20 means model
    thinks YES likely → BUY_NO contradicts the model directionally
    (paper_min_bot.py:174)."""
    if t.get("action") != "BUY_NO":
        return False
    mp = t.get("model_prob")
    return mp is not None and mp > 0.20


def _directional_buy_yes_min_mp(t):
    """DIRECTIONAL_YES_DISAGREE — BUY_YES with model_prob < 0.65 means model
    thinks NO likely → BUY_YES contradicts the model
    (paper_min_bot.py:175, retuned 2026-05-04 from 0.60)."""
    if t.get("action") != "BUY_YES":
        return False
    mp = t.get("model_prob")
    return mp is not None and mp < 0.65


def _h_2_disagree(t):
    """H_2_DISAGREE — d-1+ BUY_NO with HRRR-vs-NBM disagreement >= 4.5°F.
    Threshold raised from 2.0 on 2026-05-10 per stack-aware audit + threshold
    sweep (see H_2_0_DISAGREE_F constant comment in paper_min_bot.py).
    Approximated using mu_hrrr_at_entry vs mu_nbm_om_at_entry; only fires
    for d-1+ entries."""
    if t.get("action") != "BUY_NO":
        return False
    if t.get("is_today_at_entry"):
        return False
    h = t.get("mu_hrrr_at_entry")
    nbm = t.get("mu_nbm_om_at_entry")
    if h is None or nbm is None:
        return False
    return abs(float(h) - float(nbm)) >= 4.5


def _obs_confirmed_loser(t):
    """OBS_CONFIRMED_LOSER — running_min_at_entry already inside the bracket,
    so obs has effectively decided the bracket against BUY_NO. Approximated
    from running_min_at_entry vs floor/cap."""
    rm = t.get("running_min_at_entry")
    if rm is None:
        return False
    fl = t.get("floor")
    cap = t.get("cap")
    if fl is None or cap is None:
        return False
    # For LOW brackets (kind=tail_low / B-low), BUY_NO loses if rm enters
    # bracket. min_bot trades min temps — convention here mirrors v1's high
    # bracket geometry inverted.
    return float(fl) <= float(rm) <= float(cap)


def _no_thigh(t):
    """NO_THIGH — min_bot's T-high BUY_NO gate, blocks when forecast
    cushion above floor is too thin (analog of V1's TLOW filter)."""
    if t.get("action") != "BUY_NO":
        return False
    if t.get("kind") != "tail_high":
        return False
    fl = t.get("floor")
    mu = t.get("mu")
    if fl is None or mu is None:
        return False
    return (float(mu) - float(fl)) < 1.5


def _cold_source_outlier(t):
    """COLD_SOURCE_OUTLIER — BUY_NO with picked μ colder than median of
    {NBP, NBM-OM, HRRR} by > 4.0°F. Fires on d-0 + d-1+ (no is_today scope).
    Settlement records use the _at_entry suffix; live opp dicts use bare keys.
    Approximate from settlement fields here."""
    if t.get("action") != "BUY_NO":
        return False
    mu = t.get("mu")
    if mu is None:
        return False
    import statistics as _stat
    sources = []
    for k in ("mu_nbp_at_entry", "mu_nbm_om_at_entry", "mu_hrrr_at_entry"):
        v = t.get(k)
        if v is not None:
            sources.append(float(v))
    if len(sources) < 2:
        return False
    med = _stat.median(sources)
    return float(mu) - med < -4.0


def _high_conviction_disag_trap(t):
    """HIGH_CONVICTION_DISAG_TRAP — BUY_NO with mp < 0.075 AND disag >= 2.0°F.
    Catches the max-Kelly-trap pattern: low mp triggers max bet size while
    active NWP source disagreement signals σ may not capture true uncertainty.
    DEPLOYED 2026-05-12 eve. n=3 in 14d pool, W:L 0:3, lift +$164, robust(-1)
    +$89.56. Unique catches beyond COLD_SOURCE_OUTLIER: AUS-MAY12 + ATL-MAY07."""
    if t.get("action") != "BUY_NO":
        return False
    mp = t.get("model_prob")
    if mp is None:
        return False
    if float(mp) >= 0.075:
        return False
    disag = t.get("disagreement_at_entry")
    if disag is None:
        disag = t.get("disagreement")
    if disag is None:
        return False
    return float(disag) >= 2.0


def _thin_margin(t):
    """THIN_MARGIN — BUY_NO skipped when μ sits within 2.0°F of the NEAR
    bracket edge (μ−cap if μ>cap, else floor−μ): a boundary coin-flip that
    loses on the NO payoff asymmetry (breakeven WR == price). Mirrors
    paper_min_bot.py THIN_MARGIN_MIN_F. DEPLOYED 2026-05-26. On the realized
    (settle∪exit) pool: lift_inc +$222.6, robust_lift_inc +$153.6, n=49."""
    if t.get("action") != "BUY_NO":
        return False
    mu = t.get("mu")
    if mu is None:
        return False
    mu = float(mu)
    fl, cp = t.get("floor"), t.get("cap")
    if cp is not None and mu > float(cp):
        return (mu - float(cp)) <= 2.0
    if fl is not None and mu < float(fl):
        return (float(fl) - mu) <= 2.0
    return False


SCENARIOS = {
    "model_prob_oor": (
        _model_prob_out_of_range,
        "MODEL_PROB_OUT_OF_RANGE: mp not in [0.03, 0.97] — DEPLOYED",
    ),
    "max_edge_exceeded": (
        _max_edge_exceeded,
        "MAX_EDGE_EXCEEDED: edge > 0.70 — DEPLOYED",
    ),
    "buy_no_max_mp": (
        _directional_buy_no_max_mp,
        "DIRECTIONAL_NO_DISAGREE: BUY_NO mp > 0.20 — DEPLOYED",
    ),
    "buy_yes_min_mp": (
        _directional_buy_yes_min_mp,
        "DIRECTIONAL_YES_DISAGREE: BUY_YES mp < 0.65 — DEPLOYED 2026-05-04",
    ),
    "h_2_disagree": (
        _h_2_disagree,
        "H_2_DISAGREE: d-1+ BUY_NO HRRR-vs-NBM gap >= 4.5F — DEPLOYED (raised 2026-05-10)",
    ),
    "obs_confirmed_loser": (
        _obs_confirmed_loser,
        "OBS_CONFIRMED_LOSER: running_min_at_entry already in bracket — DEPLOYED",
    ),
    "no_thigh": (
        _no_thigh,
        "NO_THIGH: T-high BUY_NO mu - floor < 1.5F — DEPLOYED",
    ),
    "cold_source_outlier": (
        _cold_source_outlier,
        "COLD_SOURCE_OUTLIER: BUY_NO picked μ < median(NBP,NBM-OM,HRRR) - 4.0F — DEPLOYED 2026-05-12",
    ),
    "high_conviction_disag_trap": (
        _high_conviction_disag_trap,
        "HIGH_CONVICTION_DISAG_TRAP: BUY_NO mp < 0.075 AND disag >= 2.0F — DEPLOYED 2026-05-12 eve",
    ),
    "thin_margin": (
        _thin_margin,
        "THIN_MARGIN: BUY_NO μ within 2.0F of near bracket edge — DEPLOYED 2026-05-26",
    ),
}

# 2026-05-06: `live` meta-scenario — the canonical full-live entry filter
# chain. ALWAYS use `--stack live` for filter validation. Partial stacks
# silently overstate lift_inc when the candidate overlaps any deployed
# filter not in the partial list. When a NEW filter is deployed in min_bot,
# add it both to SCENARIOS above AND to LIVE_CHAIN below in the same commit.
# This list IS the deploy gate.
LIVE_CHAIN = [
    "model_prob_oor",
    "max_edge_exceeded",
    "buy_no_max_mp",
    "buy_yes_min_mp",
    "h_2_disagree",
    "obs_confirmed_loser",
    "no_thigh",
    "cold_source_outlier",
    "high_conviction_disag_trap",
    "thin_margin",
]
SCENARIOS["live"] = (
    lambda t: any(SCENARIOS[name][0](t) for name in LIVE_CHAIN),
    f"LIVE chain (canonical full deployed entry filters): {','.join(LIVE_CHAIN)}",
)


def stacked_predicate(scenario_names):
    """Return a predicate that ORs together a comma-separated list of
    scenario names. Unknown names raise."""
    if not scenario_names:
        return None
    names = [n.strip() for n in scenario_names.split(",") if n.strip()]
    fns = []
    for n in names:
        if n not in SCENARIOS:
            raise SystemExit(f"Unknown scenario in --stack: '{n}'. Use --scenario list.")
        fns.append(SCENARIOS[n][0])

    def stack(t):
        return any(f(t) for f in fns)
    stack.__name__ = "stack:" + ",".join(names)
    stack._labels = names
    return stack


# ─────────────────────────────────────────────────────────────────────────────
# Reports
# ─────────────────────────────────────────────────────────────────────────────
def _stats(pool, drop_fn=None):
    kept = [t for t in pool if not (drop_fn and drop_fn(t))]
    n = len(kept)
    bet = sum(t.get("kelly_bet", 0) or 0 for t in kept)
    pnl = sum(t["pnl"] for t in kept)
    won = sum(1 for t in kept if t["won"])
    return {
        "n": n, "bet": bet, "pnl": pnl,
        "win_pct": (100 * won / n) if n else 0,
        "roi_pct": (100 * pnl / bet) if bet else 0,
    }


def _record_date(t):
    return t.get("date") or t.get("date_str") or t.get("ts", "")[:10]


def report_stacked(pool, stack_fn, candidate_fn, stack_label, candidate_label):
    """Three-tier report: baseline → stack → stack+candidate. Reports
    INCREMENTAL lift of candidate ON TOP OF the stack — the right
    comparison when validating a new filter against the deployed chain.

    Ported from V2 (obs-pipeline-bot/tools/backtest_filters.py:report_stacked)
    via V1 (tools/v1_backtest_filters.py:report_stacked)."""
    combined_fn = lambda t: stack_fn(t) or candidate_fn(t)
    base = _stats(pool)
    s = _stats(pool, stack_fn)
    c = _stats(pool, combined_fn)

    # Stack alone helps:hurts vs full baseline
    s_by_date = defaultdict(lambda: {"base": 0.0, "kept": 0.0})
    for t in pool:
        d = _record_date(t)
        s_by_date[d]["base"] += t["pnl"]
        if not stack_fn(t):
            s_by_date[d]["kept"] += t["pnl"]
    s_helps = s_hurts = s_neut = 0
    s_total = 0.0
    s_big = 0.0
    s_big_d = None
    for d, v in s_by_date.items():
        delta = v["kept"] - v["base"]
        if delta > 0.01: s_helps += 1
        elif delta < -0.01: s_hurts += 1
        else: s_neut += 1
        s_total += delta
        if abs(delta) > abs(s_big):
            s_big = delta
            s_big_d = d
    s_robust = s_total - max(0, s_big)

    # Candidate INCREMENTAL helps:hurts vs stack-only baseline
    by_date = defaultdict(lambda: {"stack": 0.0, "combined": 0.0})
    for t in pool:
        d = _record_date(t)
        if not stack_fn(t):
            by_date[d]["stack"] += t["pnl"]
        if not combined_fn(t):
            by_date[d]["combined"] += t["pnl"]
    inc_helps = inc_hurts = inc_neut = 0
    inc_total = 0.0
    inc_biggest = 0.0
    inc_biggest_date = None
    for d, v in by_date.items():
        delta = v["combined"] - v["stack"]
        if delta > 0.01: inc_helps += 1
        elif delta < -0.01: inc_hurts += 1
        else: inc_neut += 1
        inc_total += delta
        if abs(delta) > abs(inc_biggest):
            inc_biggest = delta
            inc_biggest_date = d
    inc_robust = inc_total - max(0, inc_biggest)
    n_drop_inc = s["n"] - c["n"]

    print(f"\n=== Stack: {stack_label} | Candidate: {candidate_label} ===")
    print(f"{'metric':<14} {'baseline':>10} {'stack':>10} {'+cand':>10}")
    print(f"{'n':<14} {base['n']:>10} {s['n']:>10} {c['n']:>10}")
    print(f"{'$bet':<14} {base['bet']:>10.2f} {s['bet']:>10.2f} {c['bet']:>10.2f}")
    print(f"{'$P&L':<14} {base['pnl']:>+10.2f} {s['pnl']:>+10.2f} {c['pnl']:>+10.2f}")
    print(f"{'win%':<14} {base['win_pct']:>9.1f}% {s['win_pct']:>9.1f}% {c['win_pct']:>9.1f}%")
    print(f"{'ROI%':<14} {base['roi_pct']:>+9.1f}% {s['roi_pct']:>+9.1f}% {c['roi_pct']:>+9.1f}%")
    print()
    print(f"  STACK alone vs baseline:")
    print(f"    n_blocked       {base['n'] - s['n']}")
    print(f"    lift            {s['pnl']-base['pnl']:>+10.2f}")
    print(f"    helps:hurts     {s_helps}:{s_hurts} (+{s_neut} neutral)")
    print(f"    robust lift     {s_robust:>+10.2f} (drop {s_big_d}, delta {s_big:+.2f})")
    print()
    print(f"  CANDIDATE incremental over stack:")
    print(f"    n_blocked_inc   {n_drop_inc}")
    print(f"    lift_inc        {c['pnl']-s['pnl']:>+10.2f}")
    print(f"    helps:hurts_inc {inc_helps}:{inc_hurts} (+{inc_neut} neutral)")
    print(f"    robust_lift_inc {inc_robust:>+10.2f} (drop {inc_biggest_date}, delta {inc_biggest:+.2f})")
    if n_drop_inc == 0 and base["n"] == s["n"]:
        print()
        print("  NOTE: Pool is realized trades (settle ∪ exit), post-live-chain.")
        print("  Stack predicates fire on 0 records, so lift_inc here is")
        print("  identical to a naive on-pool measurement. The stack interface")
        print("  still serves as documentation that the measurement IS incremental.")


def analyze_filter_naive(pool, drop_fn, label="filter"):
    """Naive: for each pool record, ask if filter would have blocked it.
    Use when --stack is not provided. WARNING: standalone naive measurement
    can overstate lift when candidate overlaps deployed filters."""
    n_total = len(pool)
    dropped = [r for r in pool if drop_fn(r)]
    kept = [r for r in pool if not drop_fn(r)]
    n_dropped = len(dropped)
    if n_dropped == 0:
        print(f"  {label}: no trades dropped (filter never fires on this pool)")
        return
    drop_pnl = sum(r["pnl"] for r in dropped)
    kept_pnl = sum(r["pnl"] for r in kept)
    won_drop = sum(1 for r in dropped if r["won"])
    print(f"\n=== Filter analysis: {label} ===")
    print(f"  Pool size: {n_total}, dropped by filter: {n_dropped}, kept: {len(kept)}")
    print(f"  Dropped: {won_drop}W / {n_dropped - won_drop}L = {won_drop/n_dropped*100:.0f}% win rate in blocked set")
    print(f"  Dropped trades pnl: ${drop_pnl:+.2f}")
    print(f"  Kept trades pnl:    ${kept_pnl:+.2f}")
    print(f"  FILTER LIFT (-drop_pnl): ${-drop_pnl:+.2f}")


# ─────────────────────────────────────────────────────────────────────────────
# Custom predicate runner
# ─────────────────────────────────────────────────────────────────────────────
def load_custom_predicate(spec):
    """spec: 'path/to/file.py:function_name' — predicate(record)->bool."""
    if ":" not in spec:
        raise ValueError("--custom requires format 'path.py:function_name'")
    path, fname = spec.split(":", 1)
    p = Path(path)
    spec_obj = importlib.util.spec_from_file_location("custom", p)
    mod = importlib.util.module_from_spec(spec_obj)
    spec_obj.loader.exec_module(mod)
    return getattr(mod, fname)


def list_scenarios():
    print("min_bot backtest scenarios:")
    for name, (fn, desc) in SCENARIOS.items():
        if name == "live":
            continue
        in_live = " [LIVE]" if name in LIVE_CHAIN else ""
        print(f"  {name:25s}{in_live}  {desc}")
    print(f"  {'live':25s} [META]  {SCENARIOS['live'][1]}")


def list_filters():
    """List all decision tags written by min_bot to bot_decisions over the
    last 30 days. Useful to discover deployed filter names that should be
    added to SCENARIOS."""
    import sqlite3
    db = sqlite3.connect("/home/ubuntu/shared_tools/data/bot_decisions.sqlite")
    cur = db.execute(
        "SELECT decision, COUNT(*) FROM decisions "
        "WHERE bot='min_bot' AND stage='gate' "
        "AND ts >= strftime('%s','now') - 86400*30 "
        "GROUP BY decision ORDER BY 2 DESC")
    print("min_bot gate decisions (last 30d):")
    for d, n in cur:
        print(f"  {n:>7d}  {d}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="min_bot filter backtest engine")
    ap.add_argument("--list-filters", action="store_true",
                    help="List all min_bot gate-decision tags from bot_decisions")
    ap.add_argument("--pool-stats", action="store_true",
                    help="Show baseline settled-pool stats")
    ap.add_argument("--scenario",
                    help="Built-in scenario name as candidate predicate (or 'list')")
    ap.add_argument("--custom",
                    help="path/to/predicate.py:function_name (predicate(record)->bool)")
    ap.add_argument("--stack",
                    help="Comma-separated scenario names (or 'live') used as the BASELINE for "
                         "incremental measurement. Always pair --custom / --scenario with "
                         "--stack live to validate against the deployed chain.")
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"Era cutoff (default {DEFAULT_SINCE})")
    ap.add_argument("--no-normalize", action="store_true",
                    help="Don't cap cost at $30 (preserves raw historical bet sizes)")
    args = ap.parse_args()

    norm = None if args.no_normalize else DEFAULT_MAX_BET_NORM

    if args.list_filters:
        list_filters()
        return

    if args.scenario == "list":
        list_scenarios()
        return

    if args.pool_stats:
        pool = load_settled(since_date=args.since, normalize_bet=norm)
        s = compute_pool_stats(pool, label=f"min_bot settled trades since {args.since}")
        print(f"\nPool stats:")
        print(f"  n: {s['n']}")
        if s["n"] > 0:
            print(f"  Won: {s['won']} ({s['win_rate']*100:.0f}%)")
            print(f"  Total pnl: ${s['total_pnl']:+.2f}")
            print(f"  Total cost: ${s['total_cost']:.2f}")
            print(f"  ROI: {s['roi']*100:.0f}%")
        return

    candidate_fn = None
    candidate_label = None
    if args.scenario:
        if args.scenario not in SCENARIOS:
            raise SystemExit(f"Unknown scenario '{args.scenario}'. Use --scenario list.")
        candidate_fn = SCENARIOS[args.scenario][0]
        candidate_label = args.scenario
    elif args.custom:
        candidate_fn = load_custom_predicate(args.custom)
        candidate_label = args.custom

    if candidate_fn is not None:
        pool = load_settled(since_date=args.since, normalize_bet=norm)
        if args.stack:
            stack_fn = stacked_predicate(args.stack)
            report_stacked(pool, stack_fn, candidate_fn,
                           stack_label=args.stack, candidate_label=candidate_label)
        else:
            print("WARNING: no --stack supplied — running NAIVE analysis (may overstate "
                  "lift when candidate overlaps deployed filters). Use --stack live for "
                  "incremental measurement against the deployed chain.")
            analyze_filter_naive(pool, candidate_fn, label=candidate_label)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
