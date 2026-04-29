"""Source-MAE audit. For each historical candidate / settled CLI, compare
forecast μ from each source (NBP, HRRR, NBM-OM) against the actual CLI low.
Per-source MAE + per-city + per-day-offset breakdowns.

This is the data that justifies (or doesn't) integrating NWS forecast on
min_bot. V2's case for NWS-PRIMARY rests on a 0.86°F MAE advantage over NBP
on max-temp d-1; this tool tells us whether the gap is similar on min-temp.

Usage:
  python3 tools/source_audit.py
  python3 tools/source_audit.py --days 14
  python3 tools/source_audit.py --series KXLOWTLAX
  python3 tools/source_audit.py --day-offset 0   # d-0 only

Output (per source):
  n   = candidates with that source available
  mae = mean(|μ_source − cli|)
  bias = mean(μ_source − cli)  (negative = forecast cooler than reality)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(str(ROOT))
import paper_min_bot as bot  # noqa: E402

DATA_DIR = ROOT / "data"
LEGACY_TRADES = DATA_DIR / "trades.jsonl"
DB = "/home/ubuntu/obs-pipeline/data/obs.sqlite"


def _candidate_files(days: int | None) -> list[Path]:
    out: list[Path] = []
    if LEGACY_TRADES.exists():
        out.append(LEGACY_TRADES)
    if days is None:
        for p in sorted(DATA_DIR.glob("trades_*.jsonl")):
            out.append(p)
    else:
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
        for p in sorted(DATA_DIR.glob("trades_*.jsonl")):
            try:
                d = datetime.strptime(p.stem.replace("trades_", ""), "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= cutoff:
                out.append(p)
    return out


def _first_per_ticker(days: int | None) -> list[dict]:
    """First-occurrence candidate record per (ticker)."""
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
        except Exception:
            pass
    return out


def _all_cli_lows() -> dict[tuple[str, str], int]:
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5.0)
    out: dict[tuple[str, str], int] = {}
    for cd, st, low in conn.execute(
        "SELECT climate_date, station, low_f FROM cli_reports "
        "WHERE low_f IS NOT NULL"
    ):
        out[(st, cd)] = int(low)
    conn.close()
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, help="Last N days only")
    p.add_argument("--series", help="Filter to one KXLOWT series ticker")
    p.add_argument("--day-offset", type=int, choices=(-1, 0, 1, 2, 3),
                   help="Filter to one day-offset (d+0 = today, d-1 = tomorrow)")
    args = p.parse_args()

    cands = _first_per_ticker(args.days)
    cli = _all_cli_lows()
    print(f"Candidates: {len(cands)}    CLI rows: {len(cli)}")

    rows: list[dict] = []
    for c in cands:
        st = c.get("station")
        ds = c.get("date_str")
        if not st or not ds:
            continue
        if args.series and c.get("series") != args.series:
            continue
        actual = cli.get((st, ds))
        if actual is None:
            continue  # not yet settled / obs-pipeline missing
        # day-offset at evaluation time: derive from ts vs date_str
        try:
            d_eval = datetime.fromisoformat(c.get("ts", "").replace("Z", "+00:00")).date()
            d_target = datetime.fromisoformat(ds + "T00:00:00+00:00").date()
            d_off = (d_target - d_eval).days
        except Exception:
            d_off = None
        if args.day_offset is not None and d_off != args.day_offset:
            continue
        rows.append({
            "ticker": c["market_ticker"],
            "station": st, "series": c.get("series"),
            "date_str": ds, "actual": actual, "day_off": d_off,
            "mu_nbp": c.get("mu_nbp"),
            "mu_hrrr": c.get("mu_hrrr"),
            "mu_nbm_om": c.get("mu_nbm_om"),
            "mu_used": c.get("mu"),  # bot's actual chosen μ
            "mu_source": c.get("mu_source"),
        })

    print(f"Resolved (with CLI): {len(rows)}")
    print()

    # Per-source MAE
    def source_stats(field: str, label: str) -> None:
        diffs = []
        for r in rows:
            v = r.get(field)
            if v is None:
                continue
            diffs.append(float(v) - r["actual"])
        if not diffs:
            print(f"  {label:18}  n=0")
            return
        n = len(diffs)
        mae = sum(abs(d) for d in diffs) / n
        bias = sum(diffs) / n
        rmse = (sum(d * d for d in diffs) / n) ** 0.5
        print(f"  {label:18}  n={n:>4}  MAE={mae:.2f}°F  bias={bias:+.2f}°F  RMSE={rmse:.2f}°F")

    print("=== Overall per-source ===")
    source_stats("mu_nbp", "NBP")
    source_stats("mu_hrrr", "HRRR")
    source_stats("mu_nbm_om", "NBM-OM")
    source_stats("mu_used", "bot.mu (chosen)")

    # NBP+HRRR mean (V2's d-0 NWS+HRRR analog)
    diffs = []
    for r in rows:
        if r.get("mu_nbp") is not None and r.get("mu_hrrr") is not None:
            blend = (float(r["mu_nbp"]) + float(r["mu_hrrr"])) / 2.0
            diffs.append(blend - r["actual"])
    if diffs:
        n = len(diffs); mae = sum(abs(d) for d in diffs)/n; bias = sum(diffs)/n
        print(f"  NBP+HRRR mean      n={n:>4}  MAE={mae:.2f}°F  bias={bias:+.2f}°F  RMSE={(sum(d*d for d in diffs)/n)**0.5:.2f}°F")

    # Triple blend (NBP+HRRR+NBM-OM mean)
    diffs = []
    for r in rows:
        a = r.get("mu_nbp"); b = r.get("mu_hrrr"); c = r.get("mu_nbm_om")
        if a is not None and b is not None and c is not None:
            blend = (float(a) + float(b) + float(c)) / 3.0
            diffs.append(blend - r["actual"])
    if diffs:
        n = len(diffs); mae = sum(abs(d) for d in diffs)/n; bias = sum(diffs)/n
        print(f"  Triple-mean        n={n:>4}  MAE={mae:.2f}°F  bias={bias:+.2f}°F  RMSE={(sum(d*d for d in diffs)/n)**0.5:.2f}°F")

    # By day-offset
    print()
    print("=== By day-offset ===")
    for offset in (-1, 0, 1, 2, 3):
        sub = [r for r in rows if r.get("day_off") == offset]
        if not sub:
            continue
        print(f"\nday_off = {offset:+d}  (n={len(sub)})")
        for field, label in (("mu_nbp", "NBP"), ("mu_hrrr", "HRRR"),
                             ("mu_nbm_om", "NBM-OM"), ("mu_used", "bot.mu")):
            diffs = [float(r[field]) - r["actual"] for r in sub if r.get(field) is not None]
            if diffs:
                n = len(diffs); mae = sum(abs(d) for d in diffs)/n; bias = sum(diffs)/n
                print(f"  {label:18} n={n:>3}  MAE={mae:.2f}°F  bias={bias:+.2f}°F")

    # By city
    print()
    print("=== By city (NBP MAE; only cities with n≥3) ===")
    by_series: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_series[r["series"]].append(r)
    print(f"  {'series':<14} {'n':>4} {'NBP MAE':>9} {'NBP bias':>10} {'HRRR MAE':>10} {'HRRR bias':>11}")
    for series, sub in sorted(by_series.items()):
        diffs_nbp = [float(r["mu_nbp"]) - r["actual"] for r in sub if r.get("mu_nbp") is not None]
        diffs_hrrr = [float(r["mu_hrrr"]) - r["actual"] for r in sub if r.get("mu_hrrr") is not None]
        if not diffs_nbp or len(diffs_nbp) < 3:
            continue
        nbp_mae = sum(abs(d) for d in diffs_nbp)/len(diffs_nbp)
        nbp_bias = sum(diffs_nbp)/len(diffs_nbp)
        if diffs_hrrr:
            hrrr_mae = sum(abs(d) for d in diffs_hrrr)/len(diffs_hrrr)
            hrrr_bias = sum(diffs_hrrr)/len(diffs_hrrr)
            hrrr_str = f"{hrrr_mae:>10.2f} {hrrr_bias:>+11.2f}"
        else:
            hrrr_str = f"{'—':>10} {'—':>11}"
        print(f"  {series:<14} {len(sub):>4} {nbp_mae:>9.2f} {nbp_bias:>+10.2f} {hrrr_str}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
