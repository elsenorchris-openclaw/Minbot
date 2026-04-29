"""Gate audit / backtest tool for min_bot.

Reads candidate records from data/trades_YYYY-MM-DD.jsonl + data/trades.jsonl,
applies CURRENT bot gates via _evaluate_gates, resolves outcome via Kalshi
market_result, reports per-gate (n / wr / EV).

Usage:
  python3 tools/gate_audit.py                      # all gates summary
  python3 tools/gate_audit.py --gate PRICE_ZONE    # detail on one gate
  python3 tools/gate_audit.py --action BUY_NO      # filter by action
  python3 tools/gate_audit.py --days 7             # last 7 days only
  python3 tools/gate_audit.py --include-active     # also poll Kalshi /markets
                                                   #  for tickers we never held
  python3 tools/gate_audit.py --list-gates         # show all gate names

The shadow-logged `blocked_by` field on each candidate is used directly when
present. For pre-shadow records (before commit f2bc531 / 2026-04-29) the
script falls back to recomputing via paper_min_bot._evaluate_gates so old
data is still usable.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
import paper_min_bot as bot  # noqa: E402  (sys.path mutated above)

DATA_DIR = ROOT / "data"
LEGACY_TRADES = DATA_DIR / "trades.jsonl"
SETTLEMENTS = DATA_DIR / "settlements.jsonl"

GATE_NAMES = (
    "WOULD_ENTER",      # synthetic — blocked_by=None
    "NO_ACTION",
    "MIN_EDGE",
    "OBS_CONFIRMED_LOSER",
    "MAX_EDGE",
    "MP_RANGE",
    "DIRECTIONAL_BUY_NO",
    "DIRECTIONAL_BUY_YES",
    "ABS_DIST",
    "F2A",
    "MSG",
    "H_2_0",
    "MAX_DISAGREEMENT",
    "MU_VS_RM",
    "SPREAD",
)


def _candidate_files(days: int | None) -> list[Path]:
    """Return ordered list of trade files to read. Legacy single-file plus
    every dated file (or just the last `days` of dated files when set)."""
    files: list[Path] = []
    if LEGACY_TRADES.exists():
        files.append(LEGACY_TRADES)
    if days is None:
        for p in sorted(DATA_DIR.glob("trades_*.jsonl")):
            files.append(p)
    else:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        for p in sorted(DATA_DIR.glob("trades_*.jsonl")):
            try:
                d = datetime.strptime(p.stem.replace("trades_", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= cutoff:
                files.append(p)
    return files


def _load_first_candidates(days: int | None) -> list[dict]:
    """Stream candidate records, keeping the first occurrence per ticker."""
    seen: set[str] = set()
    out: list[dict] = []
    for path in _candidate_files(days):
        try:
            with open(path) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get("kind") != "candidate":
                        continue
                    tk = r.get("market_ticker")
                    if not tk or tk in seen:
                        continue
                    seen.add(tk)
                    out.append(r)
        except Exception as e:
            print(f"  warn: failed to read {path}: {e}", file=sys.stderr)
    return out


def _local_settlements() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not SETTLEMENTS.exists():
        return out
    with open(SETTLEMENTS) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            tk = r.get("market_ticker")
            if tk:
                out[tk] = r
    return out


def _kalshi_settlements() -> dict[str, dict]:
    """Pull all KXLOWT settlements from Kalshi /portfolio/settlements (paginated)."""
    out: dict[str, dict] = {}
    cursor = ""
    for _ in range(20):
        params = {"limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            r = bot.kalshi_get("/trade-api/v2/portfolio/settlements", params)
        except Exception as e:
            print(f"  warn: kalshi settlements pull failed: {e}", file=sys.stderr)
            break
        for s in (r.get("settlements") or []):
            if s.get("ticker", "").startswith("KXLOWT"):
                out[s["ticker"]] = s
        cursor = r.get("cursor") or ""
        if not cursor:
            break
    return out


def _resolve_outcome(tk: str, action: str, entry_price: float,
                     local: dict[str, dict], kalshi: dict[str, dict],
                     query_market: bool):
    """Returns (won, pnl_per_contract, source) or (None, None, None) if
    market hasn't settled and we can't resolve."""
    if tk in local:
        s = local[tk]
        won = bool(s.get("won"))
        return won, (1.0 - entry_price) if won else (-entry_price), "local"
    if tk in kalshi:
        ks = kalshi[tk]
        mr = ks.get("market_result")
        if mr in ("yes", "no"):
            yes_wins = mr == "yes"
            won = yes_wins if action == "BUY_YES" else (not yes_wins)
            return won, (1.0 - entry_price) if won else (-entry_price), "kalshi"
    if query_market:
        try:
            m = bot.kalshi_get(f"/trade-api/v2/markets/{tk}").get("market", {})
            result = m.get("result")
            if result in ("yes", "no"):
                yes_wins = result == "yes"
                won = yes_wins if action == "BUY_YES" else (not yes_wins)
                return won, (1.0 - entry_price) if won else (-entry_price), "market"
        except Exception:
            pass
    return None, None, None


def _build_opp(c: dict) -> dict:
    """Project a candidate record back to the dict shape execute_opportunity
    expects (so _evaluate_gates can run on it)."""
    fields = (
        "market_ticker", "event_ticker", "station", "series", "date_str",
        "label", "floor", "cap", "yes_bid", "yes_ask", "no_bid", "no_ask",
        "volume", "mu", "sigma", "mu_source", "mu_nbp", "sigma_nbp",
        "mu_nbm_om", "mu_hrrr", "disagreement", "running_min",
        "post_sunrise_lock", "is_today", "model_prob", "yes_ask_frac",
        "no_ask_frac", "action", "edge", "entry_price",
    )
    return {k: c.get(k) for k in fields}


def _classify(c: dict) -> str:
    """blocked_by from the record if present (post-shadow-logging),
    otherwise recompute via _evaluate_gates."""
    bb = c.get("blocked_by")
    if bb is not None:
        return bb
    if "blocked_by" in c:
        return "WOULD_ENTER"
    blocked, _ = bot._evaluate_gates(_build_opp(c))
    return blocked or "WOULD_ENTER"


def _bracket_kind(c: dict) -> str:
    fl, cp = c.get("floor"), c.get("cap")
    if fl is not None and cp is not None:
        return "B"
    if fl is not None:
        return "T-high"
    if cp is not None:
        return "T-low"
    return "?"


def _stats(records: list[dict], label: str) -> None:
    if not records:
        print(f"  {label:55} n=0")
        return
    n = len(records)
    won = sum(1 for r in records if r["won"])
    pnl = sum(r["pnl_pc"] for r in records)
    cost = sum(r["entry_price"] for r in records)
    avg_pnl = pnl / n
    roi = pnl / cost if cost else 0.0
    print(f"  {label:55} n={n:>4} wr={won/n:.0%} avg_pnl=${avg_pnl:+5.3f}/c roi={roi:+6.1%}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--gate", help="Show only candidates blocked by this gate")
    p.add_argument("--action", choices=("BUY_NO", "BUY_YES"),
                   help="Filter to one action")
    p.add_argument("--days", type=int,
                   help="Only candidates from last N days (default: all)")
    p.add_argument("--include-active", action="store_true",
                   help="Also query Kalshi /markets for tickers without settlement")
    p.add_argument("--list-gates", action="store_true",
                   help="Print known gate names and exit")
    args = p.parse_args()

    if args.list_gates:
        for g in GATE_NAMES:
            print(g)
        return 0

    bot._load_kalshi_auth()

    candidates = _load_first_candidates(days=args.days)
    print(f"Unique candidates: {len(candidates)}")

    if args.action:
        candidates = [c for c in candidates if c.get("action") == args.action]
        print(f"  filtered to action={args.action}: {len(candidates)}")

    local = _local_settlements()
    print(f"Local settlements: {len(local)}")
    kalshi = _kalshi_settlements()
    print(f"Kalshi settlements (/portfolio): {len(kalshi)}")

    # Resolve and classify
    resolved: list[dict] = []
    unresolved = 0
    for c in candidates:
        action = c.get("action")
        ep = c.get("entry_price")
        if action is None or ep is None or ep < 0.05:
            continue
        won, pnl_pc, src = _resolve_outcome(
            c["market_ticker"], action, ep, local, kalshi,
            query_market=args.include_active,
        )
        if won is None:
            unresolved += 1
            continue
        resolved.append({
            **c,
            "blocked_by_resolved": _classify(c),
            "won": won,
            "pnl_pc": pnl_pc,
            "src": src,
            "kind": _bracket_kind(c),
        })

    print(f"Resolved: {len(resolved)}    Unresolved (no settlement available): {unresolved}")

    if args.gate:
        gate = args.gate.upper()
        bucket = [r for r in resolved if r["blocked_by_resolved"] == gate]
        print()
        print(f"=== {gate} blocked candidates (n={len(bucket)}) ===")
        _stats(bucket, gate)
        for r in sorted(bucket, key=lambda x: x.get("date_str") or ""):
            flag = "WIN " if r["won"] else "LOSS"
            print(f"  {flag} {r.get('date_str','?')} {r['market_ticker']:34} "
                  f"{r['action']:7} {r['kind']:6} edge={(r.get('edge') or 0):.0%} "
                  f"mp={(r.get('model_prob') or 0):.0%} "
                  f"entry={(r.get('entry_price') or 0):.2f} "
                  f"μ={r.get('mu')} σ={r.get('sigma')} "
                  f"pnl=${r['pnl_pc']:+.3f}/c")
        return 0

    # Aggregate per gate
    print()
    print("=== Per-gate summary (would-have-been-blocked outcomes) ===")
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in resolved:
        grouped[r["blocked_by_resolved"]].append(r)
    for gate in sorted(grouped, key=lambda g: (g != "WOULD_ENTER", -len(grouped[g]))):
        _stats(grouped[gate], gate)

    print()
    print("=== Per gate × action ===")
    for gate in sorted(grouped, key=lambda g: (g != "WOULD_ENTER", -len(grouped[g]))):
        recs = grouped[gate]
        for action_label, action_filter in (
            ("BUY_NO", "BUY_NO"), ("BUY_YES", "BUY_YES"),
        ):
            sub = [r for r in recs if r["action"] == action_filter]
            if sub:
                _stats(sub, f"{gate}  {action_label}")

    print()
    print("=== Gates that blocked WINNERS (potentially over-aggressive) ===")
    for gate in sorted(grouped, key=lambda g: (g != "WOULD_ENTER", -len(grouped[g]))):
        if gate == "WOULD_ENTER":
            continue
        recs = grouped[gate]
        winners = [r for r in recs if r["won"]]
        if not winners:
            continue
        n, wn = len(recs), len(winners)
        pnl_winners = sum(r["pnl_pc"] for r in winners)
        pnl_losers = sum(r["pnl_pc"] for r in recs if not r["won"])
        net = pnl_winners + pnl_losers
        marker = "  *** GATE COSTING US ***" if net > 0 else ""
        print(f"\n--- {gate}: blocked {n}, {wn} winners ({wn/n:.0%}), "
              f"net ${net:+.2f}{marker} ---")
        for r in sorted(winners, key=lambda x: -x["pnl_pc"])[:5]:
            print(f"  WIN {r.get('date_str','?')} {r['market_ticker']:34} "
                  f"{r['action']:7} edge={(r.get('edge') or 0):.0%} "
                  f"entry={(r.get('entry_price') or 0):.2f} pnl=${r['pnl_pc']:+.3f}/c")

    return 0


if __name__ == "__main__":
    sys.exit(main())
