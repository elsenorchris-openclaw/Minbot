"""Auto-select per-series primary forecast source for min_bot.

Cron-runnable. Pulls last 14d of candidate forecasts × actuals, computes
weighted MAE per (station, days_out, source), picks the best source per
cell with hysteresis vs current selection.

Outputs:
  - data/auto_primary_selection.json    (live data the bot reads)
  - data/auto_primary_log.jsonl         (append-only history of decisions)

Usage:
  python3 auto_select.py                  # production: writes JSON + appends log
  python3 auto_select.py --dry-run        # print, don't write
  python3 auto_select.py --explain        # verbose per-cell decision log
"""
import json
import os
import sqlite3
import glob
import argparse
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from math import exp

# ─── Constants ────────────────────────────────────────────────────────
LOOKBACK_DAYS = 14
HALF_LIFE_DAYS = 7
MIN_SAMPLES = 5
SWITCH_MAE_THRESHOLD_F = 0.30           # absolute °F improvement required
SWITCH_RELATIVE_THRESHOLD = 1.20        # new must be ≥20% relatively better
SANITY_MAE_CEILING_F = 4.0              # if all sources MAE > this, don't switch (model is broken)

# Hardcoded defaults (mirrors current paper_min_bot.py state — 2026-05-06)
HARDCODED_D0_PRIMARY = {
    "KXLOWTBOS":  "nbp",
    "KXLOWTLAS":  "nbp",
    "KXLOWTLAX":  "nbp",
    "KXLOWTPHIL": "nbp",
    "KXLOWTSEA":  "nbp",
    "KXLOWTSFO":  "nbp",
    "KXLOWTATL":  "nbp",
    "KXLOWTDAL":  "nbp",
}
DEFAULT_D0 = "hrrr"  # default when not in dict

HARDCODED_D1_PRIMARY = {
    "KXLOWTCHI":  "hrrr",
    "KXLOWTOKC":  "hrrr",
    "KXLOWTHOU":  "hrrr",
    "KXLOWTNOLA": "hrrr",
    "KXLOWTSATX": "hrrr",
    "KXLOWTMIA":  "nbm",
    "KXLOWTDEN":  "nbm",
    "KXLOWTMIN":  "hrrr",
}
DEFAULT_D1 = "nbp"  # default when not in dict

# All min_bot tracked stations (icao → series)
SERIES_TO_ICAO = {
    "KXLOWTNYC": "KNYC", "KXLOWTSFO": "KSFO", "KXLOWTSEA": "KSEA",
    "KXLOWTLAX": "KLAX", "KXLOWTMIA": "KMIA", "KXLOWTHOU": "KHOU",
    "KXLOWTPHX": "KPHX", "KXLOWTPHIL": "KPHL", "KXLOWTOKC": "KOKC",
    "KXLOWTBOS": "KBOS", "KXLOWTCHI": "KMDW", "KXLOWTMIN": "KMSP",
    "KXLOWTDEN": "KDEN", "KXLOWTLAS": "KLAS", "KXLOWTATL": "KATL",
    "KXLOWTDC": "KDCA", "KXLOWTDAL": "KDAL", "KXLOWTAUS": "KAUS",
    "KXLOWTSATX": "KSAT", "KXLOWTNOLA": "KMSY",
}

OBS_DB_PATH = '/home/ubuntu/obs-pipeline/data/obs.sqlite'
CANDIDATE_GLOB = '/home/ubuntu/paper_min_bot/data/trades_2026-*.jsonl'
OUTPUT_JSON = '/home/ubuntu/paper_min_bot/data/auto_primary_selection.json'
LOG_JSONL = '/home/ubuntu/paper_min_bot/data/auto_primary_log.jsonl'


# ─── Data pulls ──────────────────────────────────────────────────────
def load_ground_truth():
    """(station, climate_date) -> actual_rm. Prefer CLI low_f, fallback running_min."""
    con = sqlite3.connect(f'file:{OBS_DB_PATH}?mode=ro', uri=True)
    cur = con.cursor()
    gt = {}
    cur.execute("SELECT station, climate_date, low_f FROM cli_reports WHERE low_f IS NOT NULL")
    for r in cur.fetchall():
        gt[(r[0], r[1])] = ('cli', float(r[2]))
    cur.execute("SELECT station, climate_date, min_f FROM running_min WHERE min_f IS NOT NULL")
    for r in cur.fetchall():
        if (r[0], r[1]) not in gt:
            gt[(r[0], r[1])] = ('rm', float(r[2]))
    con.close()
    return gt


def load_forecast_samples(now_utc):
    """Returns dict[(icao, climate_date, days_out, source)] = mu (most recent
    candidate per cell wins; older entries shadowed)."""
    cutoff_dt = now_utc - timedelta(days=LOOKBACK_DAYS)
    samples = {}  # (icao, cd, days_out, src) -> (ts, mu)

    for fp in sorted(glob.glob(CANDIDATE_GLOB)):
        try:
            with open(fp) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    if r.get('kind') != 'candidate':
                        continue
                    st = r.get('station')
                    cd = r.get('date_str')
                    if not st or not cd:
                        continue
                    # Only stations we care about
                    if st not in SERIES_TO_ICAO.values():
                        continue
                    # Filter by recency of climate_date
                    try:
                        cd_dt = datetime.strptime(cd, '%Y-%m-%d').replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if cd_dt < cutoff_dt:
                        continue
                    # Determine days_out
                    is_today = bool(r.get('is_today'))
                    days_out = 0 if is_today else 1  # min_bot only trades d0/d1
                    ts = r.get('ts', '')
                    for src_field, src_name in [('mu_nbp', 'nbp'), ('mu_hrrr', 'hrrr'), ('mu_nbm_om', 'nbm')]:
                        mu = r.get(src_field)
                        if mu is None:
                            continue
                        key = (st, cd, days_out, src_name)
                        prev = samples.get(key)
                        if prev is None or ts > prev[0]:
                            samples[key] = (ts, float(mu))
        except FileNotFoundError:
            continue
    return samples


# ─── Scoring + selection ─────────────────────────────────────────────
def compute_weighted_mae(forecasts, gt, now_utc):
    """forecasts: dict[(icao, cd, d_out, src)] = (ts, mu)
    Returns: dict[(icao, d_out, src)] -> {'mae': float, 'n': int, 'bias': float, 'days': [...]}"""
    by_cell = defaultdict(list)  # (icao, d_out, src) -> [(cd, days_ago, mu, actual)]
    for (icao, cd, d_out, src), (ts, mu) in forecasts.items():
        if (icao, cd) not in gt:
            continue
        _, actual = gt[(icao, cd)]
        try:
            cd_dt = datetime.strptime(cd, '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except Exception:
            continue
        days_ago = (now_utc.date() - cd_dt.date()).days
        if days_ago < 0 or days_ago > LOOKBACK_DAYS:
            continue
        by_cell[(icao, d_out, src)].append((cd, days_ago, mu, actual))

    out = {}
    for key, rows in by_cell.items():
        rows = sorted(set(rows), key=lambda x: -x[1])  # most recent first
        # One row per (cd) (already deduped via samples taking latest ts)
        weights = []
        abs_errs = []
        signed_errs = []
        for cd, days_ago, mu, actual in rows:
            w = exp(-days_ago / HALF_LIFE_DAYS)
            err = mu - actual
            weights.append(w)
            abs_errs.append(abs(err))
            signed_errs.append(err)
        if not weights:
            continue
        wsum = sum(weights)
        wmae = sum(w * e for w, e in zip(weights, abs_errs)) / wsum
        wbias = sum(w * e for w, e in zip(weights, signed_errs)) / wsum
        out[key] = {
            'mae': wmae,
            'bias': wbias,
            'n': len(rows),
            'rows': rows,  # for explain mode
        }
    return out


def select_primary(scores, current_primary, days_out_label):
    """Apply hysteresis + sanity floors. Returns (chosen_src, decision_reason).

    scores: dict[src] -> {'mae': float, 'n': int, 'bias': float}
    current_primary: 'nbp' | 'hrrr' | 'nbm'
    days_out_label: 'd0' or 'd1'
    """
    if not scores:
        return current_primary, 'no_data — keep current'

    # Eligible candidates: must have min_samples
    eligible = {s: m for s, m in scores.items() if m['n'] >= MIN_SAMPLES}
    if not eligible:
        return current_primary, f'insufficient samples (max n={max(m["n"] for m in scores.values())} < {MIN_SAMPLES})'

    # Sanity floor: if even the best source has MAE > ceiling, model is broken — keep current
    best_overall = min(eligible.values(), key=lambda x: x['mae'])
    if best_overall['mae'] > SANITY_MAE_CEILING_F:
        return current_primary, f'all sources >{SANITY_MAE_CEILING_F}F MAE — model broken, keep current'

    # Find lowest-MAE source
    best_src = min(eligible.keys(), key=lambda s: eligible[s]['mae'])
    best_mae = eligible[best_src]['mae']

    if best_src == current_primary:
        return current_primary, f'{current_primary} still best ({best_mae:.2f}F MAE, n={eligible[best_src]["n"]})'

    # Hysteresis: switch only if new is materially better
    current_mae = eligible.get(current_primary, {}).get('mae')
    if current_mae is None:
        # Current source has no data — switch to best
        return best_src, f'{current_primary} has no data; switching to {best_src} (MAE {best_mae:.2f}F, n={eligible[best_src]["n"]})'

    abs_diff = current_mae - best_mae  # positive means new is better
    rel_ratio = current_mae / best_mae if best_mae > 0 else float('inf')

    if abs_diff < SWITCH_MAE_THRESHOLD_F or rel_ratio < SWITCH_RELATIVE_THRESHOLD:
        return current_primary, (
            f'{best_src} better ({best_mae:.2f}F vs {current_primary}={current_mae:.2f}F, '
            f'diff={abs_diff:.2f}F, ratio={rel_ratio:.2f}) — within hysteresis, keep current'
        )

    return best_src, (
        f'SWITCH: {current_primary}={current_mae:.2f}F → {best_src}={best_mae:.2f}F '
        f'(diff={abs_diff:.2f}F, ratio={rel_ratio:.2f}, n={eligible[best_src]["n"]})'
    )


# ─── Main flow ────────────────────────────────────────────────────────
def run(dry_run=False, explain=False):
    now_utc = datetime.now(timezone.utc)
    print(f'auto_select.py @ {now_utc.isoformat()}')
    print(f'Config: lookback={LOOKBACK_DAYS}d half_life={HALF_LIFE_DAYS}d '
          f'min_samples={MIN_SAMPLES} threshold={SWITCH_MAE_THRESHOLD_F}F + {SWITCH_RELATIVE_THRESHOLD}x relative')
    print()

    gt = load_ground_truth()
    forecasts = load_forecast_samples(now_utc)
    scores = compute_weighted_mae(forecasts, gt, now_utc)

    # Group by (icao, days_out): {src -> stats}
    by_cell = defaultdict(dict)
    for (icao, d_out, src), v in scores.items():
        by_cell[(icao, d_out)][src] = v

    selections = {}
    for series, icao in SERIES_TO_ICAO.items():
        sel = {}
        for d_out in [0, 1]:
            cell_scores = by_cell.get((icao, d_out), {})
            if d_out == 0:
                current = HARDCODED_D0_PRIMARY.get(series, DEFAULT_D0)
            else:
                current = HARDCODED_D1_PRIMARY.get(series, DEFAULT_D1)
            chosen, reason = select_primary(cell_scores, current, f'd{d_out}')
            sel[f'd{d_out}_primary'] = chosen
            sel[f'd{d_out}_current'] = current
            sel[f'd{d_out}_changed'] = (chosen != current)
            sel[f'd{d_out}_reason'] = reason
            sel[f'd{d_out}_scores'] = {s: {'mae': round(m['mae'], 2), 'bias': round(m['bias'], 2), 'n': m['n']}
                                       for s, m in cell_scores.items()}
        selections[icao] = sel

    # Pretty-print
    print('=== Selections (changes only) ===')
    n_changes = 0
    for icao, sel in sorted(selections.items()):
        for d_out in [0, 1]:
            if sel[f'd{d_out}_changed']:
                n_changes += 1
                print(f'  {icao} d-{d_out}: {sel[f"d{d_out}_current"]} → {sel[f"d{d_out}_primary"]}')
                print(f'    {sel[f"d{d_out}_reason"]}')
                print(f'    scores: {sel[f"d{d_out}_scores"]}')
    if n_changes == 0:
        print('  (no changes)')

    print(f'\n=== Full selections ===')
    for icao, sel in sorted(selections.items()):
        for d_out in [0, 1]:
            chosen = sel[f'd{d_out}_primary']
            current = sel[f'd{d_out}_current']
            changed = '*' if sel[f'd{d_out}_changed'] else ' '
            scores_str = ', '.join(f'{s}={v["mae"]:.2f}/n{v["n"]}' for s, v in sel[f'd{d_out}_scores'].items())
            print(f'  {changed} {icao} d-{d_out}: {chosen:<5} (current={current:<5}) [{scores_str}]')

    if explain:
        print('\n=== Explain (per-cell day-by-day) ===')
        for icao, sel in sorted(selections.items()):
            for d_out in [0, 1]:
                cell = by_cell.get((icao, d_out), {})
                if not cell:
                    continue
                print(f'\n  {icao} d-{d_out}: chosen={sel[f"d{d_out}_primary"]}')
                # Aggregate by date
                by_date = defaultdict(dict)
                for src, v in cell.items():
                    for cd, da, mu, actual in v['rows']:
                        by_date[cd][src] = (mu, actual, mu - actual)
                for cd in sorted(by_date.keys()):
                    actual = list(by_date[cd].values())[0][1] if by_date[cd] else None
                    err_strs = []
                    for src in ['nbp', 'hrrr', 'nbm']:
                        if src in by_date[cd]:
                            mu, _, err = by_date[cd][src]
                            err_strs.append(f'{src}={err:+.1f}')
                        else:
                            err_strs.append(f'{src}=-')
                    print(f'    {cd} actual={actual:.1f} → {", ".join(err_strs)}')

    # Write output
    payload = {
        'computed_at': now_utc.isoformat(),
        'lookback_days': LOOKBACK_DAYS,
        'half_life_days': HALF_LIFE_DAYS,
        'min_samples': MIN_SAMPLES,
        'switch_mae_threshold_f': SWITCH_MAE_THRESHOLD_F,
        'switch_relative_threshold': SWITCH_RELATIVE_THRESHOLD,
        'sanity_mae_ceiling_f': SANITY_MAE_CEILING_F,
        'selections': selections,
    }
    if not dry_run:
        tmp_path = OUTPUT_JSON + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, OUTPUT_JSON)
        # Append summary log
        log_entry = {
            'ts': now_utc.isoformat(),
            'n_changes': n_changes,
            'changes': [
                {
                    'icao': icao,
                    'days_out': d_out,
                    'from': sel[f'd{d_out}_current'],
                    'to': sel[f'd{d_out}_primary'],
                    'reason': sel[f'd{d_out}_reason'],
                }
                for icao, sel in sorted(selections.items())
                for d_out in [0, 1]
                if sel[f'd{d_out}_changed']
            ],
        }
        with open(LOG_JSONL, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
        print(f'\nWrote {OUTPUT_JSON} ({n_changes} changes)')
    else:
        print('\n[dry-run, no files written]')

    return payload


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--explain', action='store_true')
    args = p.parse_args()
    run(dry_run=args.dry_run, explain=args.explain)
