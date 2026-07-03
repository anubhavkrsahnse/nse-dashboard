"""
Rule-based insight generation + news validation.

Insights computed from the share time series (no LLM, fully deterministic):
  - day-over-day and week-over-week share shifts per segment
  - month-over-month share shifts
  - 12-month highs / lows in NSE share
  - momentum streaks (share rising/falling N sessions)

News validation: pulls free RSS feeds (Google News query + ET Markets) and
attaches headlines that mention the relevant exchange/segment keywords, so
each insight can be cross-checked against reported news.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

UA = "Mozilla/5.0 (compatible; MarketShareDashboard/1.0)"

SEGMENTS = {
    "cm": {"label": "Cash market", "rival": "BSE",
           "keywords": ["cash market", "equity turnover", "cash segment",
                        "market share", "bse", "nse"]},
    "fut": {"label": "Equity futures", "rival": "BSE",
            "keywords": ["futures", "derivatives", "f&o", "bse", "nse"]},
    "optp": {"label": "Options premium", "rival": "BSE",
             "keywords": ["options", "premium turnover", "derivatives",
                          "expiry", "sensex options", "bse", "nse"]},
    "com": {"label": "Commodity derivatives", "rival": "MCX",
            "keywords": ["commodity", "mcx", "gold futures", "crude",
                         "electricity futures", "nse commodity"]},
}

NEWS_FEEDS = [
    ("Google News",
     "https://news.google.com/rss/search?q=NSE+BSE+market+share+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Google News F&O",
     "https://news.google.com/rss/search?q=BSE+NSE+derivatives+options+premium+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Google News MCX",
     "https://news.google.com/rss/search?q=MCX+NSE+commodity+derivatives+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("ET Markets",
     "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
]


def _fetch_news() -> list[dict]:
    items: list[dict] = []
    for src, url in NEWS_FEEDS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                tree = ET.fromstring(r.read())
            for it in tree.iter("item"):
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                pub = (it.findtext("pubDate") or "").strip()
                if title and link:
                    items.append({"title": title, "link": link,
                                  "source": src, "date": pub})
        except Exception:  # noqa: BLE001 - news is best-effort
            continue
    # dedupe by title
    seen, out = set(), []
    for it in items:
        k = it["title"].lower()[:80]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out[:200]


def _match_news(news: list[dict], keywords: list[str], limit=3) -> list[dict]:
    scored = []
    for it in news:
        t = it["title"].lower()
        score = sum(1 for kw in keywords if kw in t)
        if score >= 2 or (score >= 1 and ("market share" in t or "%" in t)):
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def _series(rows: dict, key: str) -> list[tuple[str, float]]:
    return [(d, m[key]) for d, m in sorted(rows.items())
            if key in m and m[key] is not None]


def _fmt_pp(x: float) -> str:
    return f"{x:+.2f}pp"

def build_insights(daily_rows: dict, monthly: dict) -> dict:
    news = _fetch_news()
    insights = []

    for seg, cfg in SEGMENTS.items():
        key = f"share_{seg}"
        s = _series(daily_rows, key)
        if len(s) < 2:
            continue
        label = cfg["label"]
        latest_d, latest = s[-1]
        prev = s[-2][1]
        dod = latest - prev
        wow = latest - s[-6][1] if len(s) >= 6 else None

        vals12 = [v for _, v in s]
        hi, lo = max(vals12), min(vals12)

        seg_news = _match_news(news, cfg["keywords"])

        headline = (f"NSE {label.lower()} share at {latest:.2f}% "
                    f"({_fmt_pp(dod)} DoD"
                    + (f", {_fmt_pp(wow)} WoW" if wow is not None else "")
                    + f") vs {cfg['rival']}")
        insights.append({
            "segment": seg, "label": label, "type": "level",
            "severity": "high" if abs(dod) >= 1 or (wow and abs(wow) >= 2)
            else "normal",
            "text": headline, "date": latest_d, "news": seg_news,
        })

        if latest >= hi - 1e-9 and len(s) > 20:
            insights.append({
                "segment": seg, "label": label, "type": "record",
                "severity": "high",
                "text": f"NSE {label.lower()} share ({latest:.2f}%) is at its "
                        f"highest in the tracked period", "date": latest_d,
                "news": seg_news})
        if latest <= lo + 1e-9 and len(s) > 20:
            insights.append({
                "segment": seg, "label": label, "type": "record",
                "severity": "high",
                "text": f"NSE {label.lower()} share ({latest:.2f}%) is at its "
                        f"lowest in the tracked period - {cfg['rival']} is "
                        f"gaining ground", "date": latest_d,
                "news": seg_news})

        # streak detection
        diffs = [s[i][1] - s[i - 1][1] for i in range(1, len(s))]
        streak = 0
        for x in reversed(diffs):
            if (x < 0) == (diffs[-1] < 0) and x != 0:
                streak += 1
            else:
                break
        if streak >= 4:
            direction = "declined" if diffs[-1] < 0 else "risen"
            insights.append({
                "segment": seg, "label": label, "type": "streak",
                "severity": "high" if diffs[-1] < 0 else "normal",
                "text": f"NSE {label.lower()} share has {direction} for "
                        f"{streak} consecutive sessions", "date": latest_d,
                "news": seg_news})

        # month-over-month
        mo = [(m, v[key]) for m, v in monthly.items() if key in v]
        if len(mo) >= 2:
            mom = mo[-1][1] - mo[-2][1]
            if abs(mom) >= 0.5:
                insights.append({
                    "segment": seg, "label": label, "type": "mom",
                    "severity": "high" if abs(mom) >= 2 else "normal",
                    "text": f"NSE {label.lower()} share moved {_fmt_pp(mom)} "
                            f"MoM ({mo[-2][0]}: {mo[-2][1]:.2f}% -> "
                            f"{mo[-1][0]}: {mo[-1][1]:.2f}%)",
                    "date": mo[-1][0], "news": seg_news})

    sev_rank = {"high": 0, "normal": 1}
    insights.sort(key=lambda i: (sev_rank.get(i["severity"], 2), i["segment"]))

    return {
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "insights": insights,
        "news_pool": news[:30],
    }
