"""
Daily updater / backfiller for the market-share dashboard.

Usage:
  python pipeline/update.py                 # fetch latest missing days
  python pipeline/update.py --backfill 365  # backfill last N calendar days
  python pipeline/update.py --date 2026-07-02

Writes/updates:
  site/data/daily.json    - per-trading-day metrics + NSE share %
  site/data/monthly.json  - monthly aggregates + share %
  site/data/insights.json - rule-based insights + matching news headlines
  site/data/meta.json     - freshness + data-quality report
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from sources import fetch_day  # noqa: E402
from insights import build_insights  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("update")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"

SHARE_DEFS = {
    # share_key: (nse_metric, competitor_metric, label)
    "cm": ("nse_cm", "bse_cm", "Cash Market (vs BSE)"),
    "stk_fut": ("nse_stk_fut", "bse_stk_fut", "Stock Futures (vs BSE)"),
    "idx_fut": ("nse_idx_fut", "bse_idx_fut", "Index Futures (vs BSE)"),
    "fut": ("nse_fut", "bse_fut", "Total Futures (vs BSE)"),
    "stk_optp": ("nse_stk_optp", "bse_stk_optp", "Stock Options (vs BSE)"),
    "idx_optp": ("nse_idx_optp", "bse_idx_optp", "Index Options (vs BSE)"),
    "optp": ("nse_optp", "bse_optp", "Total Options (vs BSE)"),
    "fo": ("nse_fo", "bse_fo", "Total F&O (vs BSE)"),
    "com": ("nse_com", "mcx_com", "Commodity Derivatives (vs MCX)"),
}


def derive_totals(m: dict) -> None:
    """Add derived F&O totals (nse_fo / bse_fo = futures notional +
    options premium) so Total F&O share can be computed."""
    for side in ("nse", "bse"):
        fut = m.get(f"{side}_fut")
        optp = m.get(f"{side}_optp")
        if fut is not None and optp is not None:
            m[f"{side}_fo"] = round(fut + optp, 2)


def load(name: str, default):
    p = DATA / name
    if p.exists():
        return json.loads(p.read_text())
    return default


def save(name: str, obj) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / name).write_text(json.dumps(obj, indent=1, ensure_ascii=False))


def compute_shares(m: dict) -> dict:
    out = {}
    for key, (a, b, _) in SHARE_DEFS.items():
        if m.get(a) is not None and m.get(b) is not None:
            tot = m[a] + m[b]
            if tot > 0:
                out[f"share_{key}"] = round(100.0 * m[a] / tot, 2)
    return out


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def run_dates(dates: list[date]) -> None:
    daily = load("daily.json", {"rows": {}})
    rows = daily["rows"]
    quality: dict = {}

    # prune bad legacy rows (holiday rows that stored only zeros)
    for k in [k for k, m in rows.items()
              if m.get("nse_cm") is None and m.get("nse_fut") is None]:
        del rows[k]

    for d in dates:
        key = d.isoformat()
        existing = rows.get(key, {})
        # skip days that are already complete (incl. contract-level data)
        if existing and not existing.get("_errors") \
                and "cx_nse" in existing and "cx_mcx" in existing:
            continue
        metrics, errors = fetch_day(d)
        if not metrics:
            # holiday or all sources down - record nothing
            log.info("%s: no data (holiday?)", d)
            continue
        row = {**existing, **metrics}
        row.pop("_errors", None)
        if errors:
            row["_errors"] = errors
        derive_totals(row)
        row.update(compute_shares(row))
        rows[key] = row
        quality[key] = errors
        time.sleep(1.5)  # be polite to exchange servers

    daily["rows"] = dict(sorted(rows.items()))
    save("daily.json", daily)

    monthly = aggregate_monthly(daily["rows"])
    save("monthly.json", monthly)

    ins = build_insights(daily["rows"], monthly)
    save("insights.json", ins)

    last_day = max(daily["rows"]) if daily["rows"] else None
    save("meta.json", {
        "updated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "last_trading_day": last_day,
        "recent_fetch_errors": {k: v for k, v in quality.items() if v},
        "segments": {k: v[2] for k, v in SHARE_DEFS.items()},
    })
    log.info("done. days stored: %d", len(daily["rows"]))


def aggregate_monthly(rows: dict) -> dict:
    months: dict = {}
    for day, m in rows.items():
        mo = day[:7]
        agg = months.setdefault(mo, {"days": 0})
        agg["days"] += 1
        for k, v in m.items():
            if k.startswith("_") or k.startswith("share_"):
                continue
            if isinstance(v, (int, float)):
                agg[k] = agg.get(k, 0.0) + v
            elif isinstance(v, dict):  # contract-level (cx_nse / cx_mcx)
                sub = agg.setdefault(k, {})
                for g, gv in v.items():
                    if isinstance(gv, (int, float)):
                        sub[g] = round(sub.get(g, 0.0) + gv, 2)
    for mo, agg in months.items():
        derive_totals(agg)
        agg.update(compute_shares(agg))
        for k in list(agg):
            if isinstance(agg[k], float):
                agg[k] = round(agg[k], 2)
    return dict(sorted(months.items()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0,
                    help="backfill last N calendar days")
    ap.add_argument("--date", type=str, default=None,
                    help="fetch a single date YYYY-MM-DD")
    args = ap.parse_args()

    today = date.today()
    if args.date:
        dates = [date.fromisoformat(args.date)]
    elif args.backfill:
        dates = [today - timedelta(days=i)
                 for i in range(args.backfill, 0, -1)]
    else:
        # daily mode: catch up on the last 7 days (handles holidays/reruns)
        dates = [today - timedelta(days=i) for i in range(7, 0, -1)]

    dates = [d for d in dates if not is_weekend(d)]
    run_dates(dates)


if __name__ == "__main__":
    main()
