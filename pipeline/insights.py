"""
Rule-based insight generation for NSE brAIn agent.

For each segment we compute the share time series and emit ONE structured
insight with four parts:
  headline   - the number and the move (DoD / WoW / MoM)
  impact     - what the move means for NSE's competitive position
  reason     - a *hypothesised* driver, phrased as a hypothesis and, where
               possible, cross-checked against recent news headlines
  actionable - a single concrete next step for NSE (one-liner)

News validation pulls free RSS feeds and attaches matching headlines so a
human can confirm or reject the hypothesised reason. Nothing here states a
cause as established fact - drivers are always framed as "likely/possible".
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime

UA = "Mozilla/5.0 (compatible; NseBrainAgent/1.0)"

# segment_key: (label, rival, keywords, actionable-if-losing, actionable-if-gaining)
SEGMENTS = {
    "cm": ("Cash Market", "BSE",
           ["cash market", "equity turnover", "cash segment", "market share",
            "delivery", "bse", "nse"],
           "Defend cash liquidity: review transaction-charge slabs and "
           "market-maker incentives on mid/small-caps where BSE is gaining.",
           "Cash lead intact: lock in flow with co-location and retail "
           "onboarding pushes before BSE responds."),
    "stk_fut": ("Stock Futures", "BSE",
                ["stock futures", "single stock", "futures", "derivatives",
                 "bse", "nse"],
                "Protect single-stock futures franchise: check margin and "
                "lot-size competitiveness on the top-20 stock underlyings.",
                "Extend stock-futures dominance: add underlyings and tighten "
                "spreads while the lead is wide."),
    "idx_fut": ("Index Futures", "BSE",
                ["index futures", "nifty futures", "sensex futures",
                 "derivatives", "bse", "nse"],
                "Index-futures share slipping: benchmark Nifty vs Sensex "
                "futures costs and expiry-day mechanics against BSE.",
                "Index-futures lead holding: bundle with options to keep the "
                "hedging flow on NSE."),
    "fut": ("Total Futures", "BSE",
            ["futures", "derivatives", "bse", "nse"],
            "Total futures share under pressure - escalate a futures pricing "
            "and product review.",
            "Futures franchise strong; maintain."),
    "stk_optp": ("Stock Options", "BSE",
                 ["stock options", "single stock options", "options",
                  "derivatives", "premium", "bse", "nse"],
                 "Stock-options premium share falling: assess strike density "
                 "and market-maker rebates on active names.",
                 "Stock-options lead solid; keep MM incentives funded."),
    "idx_optp": ("Index Options", "BSE",
                 ["index options", "sensex options", "nifty options",
                  "bank nifty", "expiry", "premium", "options", "bse", "nse"],
                 "Index-options premium is the key battleground - BSE's "
                 "Sensex/Bankex weeklies are pulling premium share. Review "
                 "expiry-day calendar and contract specs urgently.",
                 "Index-options premium share rising: consolidate with "
                 "liquidity incentives around weekly expiries."),
    "optp": ("Total Options", "BSE",
             ["options", "premium turnover", "derivatives", "expiry",
              "bse", "nse"],
             "Total options premium share is the headline metric BSE is "
             "chasing - treat any sustained decline as strategic.",
             "Total options premium lead healthy; defend expiry liquidity."),
    "fo": ("Total F&O", "BSE",
           ["f&o", "derivatives", "futures", "options", "bse", "nse"],
           "Overall F&O share eroding - board-level competitive review "
           "warranted.",
           "Overall F&O dominance intact."),
    "com": ("Commodity Derivatives", "MCX",
            ["commodity", "mcx", "gold", "crude", "electricity futures",
             "natural gas", "nse commodity"],
            "Commodities remain MCX-dominated - pick 1-2 contracts (e.g. "
            "electricity, gold-10g) to build depth rather than spreading thin.",
            "Commodity foothold growing vs MCX: double down on the winning "
            "contract's liquidity scheme."),
}

NEWS_FEEDS = [
    ("Google News",
     "https://news.google.com/rss/search?q=NSE+BSE+market+share+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Google News F&O",
     "https://news.google.com/rss/search?q=BSE+NSE+options+premium+expiry+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("Google News MCX",
     "https://news.google.com/rss/search?q=MCX+NSE+commodity+when:14d&hl=en-IN&gl=IN&ceid=IN:en"),
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
    seen, out = set(), []
    for it in items:
        k = it["title"].lower()[:80]
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out[:200]


def _match_news(news: list[dict], keywords: list[str], limit=2) -> list[dict]:
    scored = []
    for it in news:
        t = it["title"].lower()
        score = sum(1 for kw in keywords if kw in t)
        if score >= 2 or (score >= 1 and ("market share" in t or "%" in t)):
            scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored[:limit]]


def _series(rows: dict, key: str):
    return [(d, m[key]) for d, m in sorted(rows.items())
            if key in m and m[key] is not None]


def build_insights(daily_rows: dict, monthly: dict) -> dict:
    news = _fetch_news()
    insights = []

    for seg, (label, rival, kw, act_lose, act_gain) in SEGMENTS.items():
        key = f"share_{seg}"
        s = _series(daily_rows, key)
        if len(s) < 2:
            continue
        latest_d, latest = s[-1]
        dod = latest - s[-2][1]
        wow = latest - s[-6][1] if len(s) >= 6 else None
        vals = [v for _, v in s]
        hi, lo = max(vals), min(vals)

        mo = [(m, v[key]) for m, v in monthly.items() if key in v]
        mom = (mo[-1][1] - mo[-2][1]) if len(mo) >= 2 else None

        losing = dod < 0 or (wow is not None and wow < 0)
        actionable = act_lose if losing else act_gain

        # impact
        if latest >= hi - 1e-9 and len(s) > 20:
            impact = (f"NSE {label.lower()} share is at its highest in the "
                      f"tracked window - momentum is with NSE.")
            sev = "high"
        elif latest <= lo + 1e-9 and len(s) > 20:
            impact = (f"NSE {label.lower()} share is at its tracked-period low "
                      f"- {rival} is at its strongest here; treat as a red flag.")
            sev = "high"
        elif losing and (wow is not None and abs(wow) >= 1):
            impact = (f"{rival} has gained ~{abs(wow):.1f}pp of {label.lower()} "
                      f"share over the past week - a sustained trend, not noise.")
            sev = "high"
        elif not losing and (wow is not None and abs(wow) >= 1):
            impact = (f"NSE added ~{abs(wow):.1f}pp of {label.lower()} share "
                      f"this week - competitive position strengthening.")
            sev = "normal"
        else:
            impact = (f"NSE {label.lower()} share is broadly stable "
                      f"(±{abs(dod):.2f}pp on the day).")
            sev = "normal"

        # reason (hypothesis, validated by news where possible)
        seg_news = _match_news(news, kw)
        if seg in ("idx_optp", "optp", "fo") and abs(dod) >= 0.5:
            reason = ("Likely driven by expiry-day mix and the relative pull "
                      "of BSE weekly index options - confirm against the "
                      "linked headlines and the expiry calendar.")
        elif seg == "com":
            reason = ("Commodity share swings track which contracts (crude, "
                      "gold, electricity) saw volume that session - see the "
                      "contract matrix for attribution.")
        elif seg_news:
            reason = ("Possible driver in the news below - verify before "
                      "acting.")
        else:
            reason = ("No single obvious driver in headlines; likely routine "
                      "session-to-session mix. Watch for a multi-day trend.")

        wow_txt = f", {wow:+.2f}pp WoW" if wow is not None else ""
        mom_txt = f", {mom:+.2f}pp MoM" if mom is not None else ""
        insights.append({
            "segment": seg, "label": label, "rival": rival, "severity": sev,
            "date": latest_d,
            "headline": f"NSE {label} share {latest:.2f}% "
                        f"({dod:+.2f}pp DoD{wow_txt}{mom_txt}) vs {rival}",
            "impact": impact,
            "reason": reason,
            "actionable": actionable,
            "news": seg_news,
        })

    sev_rank = {"high": 0, "normal": 1}
    order = list(SEGMENTS.keys())
    insights.sort(key=lambda i: (sev_rank.get(i["severity"], 2),
                                 order.index(i["segment"])))
    return {
        "generated_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "insights": insights,
        "news_pool": news[:30],
    }
