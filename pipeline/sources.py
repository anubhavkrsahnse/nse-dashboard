"""
Data sources for NSE vs BSE vs MCX market-share dashboard.

Each fetcher returns a dict of segment metrics (Rs. crore) for one trading
date, or raises FetchError. All endpoints are free & official exchange data.

Segments and metrics collected per day:
  nse_cm   : NSE cash-market traded value
  bse_cm   : BSE cash-market traded value
  nse_fut  : NSE equity futures traded value (notional)
  nse_optp : NSE equity options PREMIUM traded value
  bse_fut  : BSE equity futures traded value
  bse_optp : BSE equity options PREMIUM traded value
  mcx_com  : MCX commodity futures+options traded value
  nse_com  : NSE commodity derivatives traded value
Debt (monthly, best-effort):
  nse_debt / bse_debt : monthly traded value from business-growth pages
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from datetime import date

import requests

log = logging.getLogger("sources")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

CRORE_PER_LAKH = 0.01


class FetchError(Exception):
    pass


def _session(referer: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": referer,
        "Origin": referer.split("/", 3)[0] + "//" + referer.split("/", 3)[2],
    })
    return s


def _get(url: str, referer: str, timeout: int = 30, **kw) -> requests.Response:
    r = _session(referer).get(url, timeout=timeout, **kw)
    if r.status_code != 200:
        raise FetchError(f"{url} -> HTTP {r.status_code}")
    return r


# ---------------------------------------------------------------- NSE CM ----

def fetch_nse_cm(d: date) -> dict:
    """NSE cash-market daily traded value from the Market Activity report
    (archives server - NOT geo-blocked, verified)."""
    url = (f"https://archives.nseindia.com/archives/equities/mkt/"
           f"MA{d:%d%m%y}.csv")
    text = _get(url, "https://www.nseindia.com/").text
    for line in text.splitlines():
        if "Traded Value" in line:
            val = line.split(",")[-1].strip()
            return {"nse_cm": float(val)}
    raise FetchError(f"NSE MA report {d}: 'Traded Value' row not found")


# ------------------------------------------------------------- UDIFF F&O ----

def _sum_udiff(csv_text: str) -> dict:
    """Sum turnover from a SEBI UDIFF-format bhavcopy, split stock vs index.
    STF/IDF = stock/index futures (notional).
    STO/IDO = stock/index options (TtlTrfVal = premium).
    UDIFF TtlTrfVal is in absolute rupees -> crore.
    Also captures NbOfCtrcts * ... not needed; ADT is derived downstream."""
    sf = idf = so = ido = 0.0
    for row in csv.DictReader(io.StringIO(csv_text)):
        try:
            v = float(row.get("TtlTrfVal") or 0)
        except ValueError:
            continue
        t = (row.get("FinInstrmTp") or "").strip()
        if t == "STF":
            sf += v
        elif t == "IDF":
            idf += v
        elif t == "STO":
            so += v
        elif t == "IDO":
            ido += v
    c = 1e7
    return {"stk_fut": sf / c, "idx_fut": idf / c,
            "stk_optp": so / c, "idx_optp": ido / c}


def fetch_nse_fo(d: date) -> dict:
    """NSE F&O split by stock/index futures & options premium via UDIFF."""
    url = (f"https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    try:
        r = _get(url, "https://www.nseindia.com/")
    except FetchError:
        url = url.replace("nsearchives", "archives")
        r = _get(url, "https://www.nseindia.com/")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        s = _sum_udiff(z.read(z.namelist()[0]).decode("utf-8", "replace"))
    sf, idf, so, ido = (s["stk_fut"], s["idx_fut"],
                        s["stk_optp"], s["idx_optp"])
    return {"nse_stk_fut": sf, "nse_idx_fut": idf, "nse_fut": sf + idf,
            "nse_stk_optp": so, "nse_idx_optp": ido, "nse_optp": so + ido}


def fetch_bse_cm(d: date) -> dict:
    """BSE cash-market total turnover by summing the equity bhavcopy."""
    url = (f"https://www.bseindia.com/download/BhavCopy/Equity/"
           f"BhavCopy_BSE_CM_0_0_0_{d:%Y%m%d}_F_0000.CSV")
    r = _get(url, "https://www.bseindia.com/")
    total = 0.0
    for row in csv.DictReader(io.StringIO(r.text)):
        try:
            total += float(row.get("TtlTrfVal") or 0)
        except ValueError:
            continue
    if total <= 0:
        raise FetchError(f"BSE CM bhavcopy {d}: zero total")
    return {"bse_cm": total / 1e7}


def fetch_bse_fo(d: date) -> dict:
    """BSE derivatives futures + options premium via UDIFF bhavcopy."""
    url = (f"https://www.bseindia.com/download/Bhavcopy/Derivative/"
           f"BhavCopy_BSE_FO_0_0_0_{d:%Y%m%d}_F_0000.CSV")
    r = _get(url, "https://www.bseindia.com/")
    s = _sum_udiff(r.text)
    sf, idf, so, ido = (s["stk_fut"], s["idx_fut"],
                        s["stk_optp"], s["idx_optp"])
    if sf + idf + so + ido <= 0:
        raise FetchError(f"BSE FO bhavcopy {d}: zero total (holiday?)")
    return {"bse_stk_fut": sf, "bse_idx_fut": idf, "bse_fut": sf + idf,
            "bse_stk_optp": so, "bse_idx_optp": ido, "bse_optp": so + ido}


# ------------------------------------------------------------- Commodity ----

# Contract groups for the NSE-vs-MCX commodity matrix.
# Symbols are matched by prefix after stripping whitespace.
CONTRACT_GROUPS = {
    "crude":       {"nse": ["CRUDEOIL", "BRCRUDEOIL"], "mcx": ["CRUDEOIL"]},
    "electricity": {"nse": ["ELEC"], "mcx": ["ELEC"]},
    "gold10g":     {"nse": ["GOLD10G"], "mcx": ["GOLDPETAL", "GOLDGUINEA"]},
    "natgas":      {"nse": ["NATURALGAS", "NATGAS"], "mcx": ["NATURALGAS", "NATGAS"]},
}


def _group_of(symbol: str, side: str) -> str | None:
    sym = (symbol or "").strip().upper()
    for grp, cfg in CONTRACT_GROUPS.items():
        if any(sym.startswith(p) for p in cfg[side]):
            return grp
    return None


def fetch_nse_com(d: date) -> dict:
    """NSE commodity derivatives via UDIFF bhavcopy (file prefix is
    BhavCopy_NSE_CO_..., verified via the daily-reports API)."""
    url = (f"https://nsearchives.nseindia.com/content/com/"
           f"BhavCopy_NSE_CO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    try:
        r = _get(url, "https://www.nseindia.com/")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    except (FetchError, zipfile.BadZipFile) as e:
        raise FetchError(f"NSE COM {d}: {e}")
    total = 0.0
    groups: dict = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            v = float(row.get("TtlTrfVal") or 0)
        except ValueError:
            continue
        total += v
        g = _group_of(row.get("TckrSymb", ""), "nse")
        if g:
            groups[g] = groups.get(g, 0.0) + v
    return {"nse_com": total / 1e7,
            "cx_nse": {k: round(v / 1e7, 2) for k, v in groups.items()}}


def fetch_mcx(d: date) -> dict:
    """MCX daily bhavcopy via the market-data JSON endpoint (endpoint and
    response shape verified in-browser). 'Value' is in Rs. lakh."""
    referer = "https://www.mcxindia.com/market-data/bhavcopy"
    s = _session(referer)
    s.headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    s.headers["X-Requested-With"] = "XMLHttpRequest"
    s.get(referer, timeout=30)  # warm Akamai cookies
    r = s.get("https://www.mcxindia.com/market-data/bhavcopy/"
              "GetDateWiseBhavCopy",
              params={"InstrumentName": "ALL",
                      "fromDate": d.strftime("%d/%m/%Y")},
              timeout=60)
    if r.status_code != 200:
        raise FetchError(f"MCX {d}: HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        raise FetchError(f"MCX {d}: non-JSON response (WAF block?)")
    rows = data.get("d") or data.get("Data") or data
    if isinstance(rows, dict):
        rows = rows.get("Data") or []
    if not rows:
        raise FetchError(f"MCX {d}: empty rows (holiday?)")
    total_lakh = 0.0
    groups: dict = {}
    for row in rows:
        try:
            v = float(row.get("Value") or 0)
        except (ValueError, TypeError):
            continue
        total_lakh += v
        g = _group_of(row.get("Symbol", ""), "mcx")
        if g:
            groups[g] = groups.get(g, 0.0) + v
    if total_lakh <= 0:
        raise FetchError(f"MCX {d}: zero total")
    return {"mcx_com": round(total_lakh * CRORE_PER_LAKH, 2),
            "cx_mcx": {k: round(v * CRORE_PER_LAKH, 2)
                       for k, v in groups.items()}}


# ------------------------------------------------------- Debt (monthly) -----

def fetch_nse_debt_monthly() -> list[dict]:
    """NSE debt-segment monthly business growth. The /api endpoint is
    geo-blocked outside India but usually reachable from some runners;
    best-effort with cookie warm-up."""
    s = _session("https://www.nseindia.com/debt/historical_debt_businessgrowth")
    s.get("https://www.nseindia.com/", timeout=30)
    r = s.get("https://www.nseindia.com/api/historical/debt-businessgrowth",
              timeout=30)
    if r.status_code != 200 or "region" in r.text[:500].lower():
        raise FetchError(f"NSE debt API blocked/unavailable ({r.status_code})")
    data = r.json().get("data", [])
    if not data:
        raise FetchError("NSE debt API: empty")
    return data


# ------------------------------------------------------------- Registry -----

DAILY_FETCHERS = {
    "nse_cm": fetch_nse_cm,
    "nse_fo": fetch_nse_fo,
    "bse_cm": fetch_bse_cm,
    "bse_fo": fetch_bse_fo,
    "nse_com": fetch_nse_com,
    "mcx": fetch_mcx,
}


def fetch_day(d: date) -> tuple[dict, dict]:
    """Fetch all segments for one date.
    Returns (metrics, errors)."""
    metrics: dict = {}
    errors: dict = {}
    for name, fn in DAILY_FETCHERS.items():
        try:
            metrics.update(fn(d))
        except Exception as e:  # noqa: BLE001
            errors[name] = str(e)
            log.warning("%s %s: %s", name, d, e)
    return metrics, errors


# ------------------------------------------------ India-only live feeds -----
# The endpoints below (www.nseindia.com/api/*) are GEO-BLOCKED outside
# India: they work from an India IP (self-hosted runner / local machine /
# in-browser capture) but FAIL from GitHub's US-hosted Actions runners.
# Schemas verified in-browser on 2026-07-05.

RFQ_TRADE_INDEXES = ("rfqtrades_listed", "rfqtrades_un-listed",
                     "rfqtrades_GSec", "rfqtrades_CP-CDs")


def fetch_nse_rfq_obpp(d: date | None = None) -> dict:
    """NSE RFQ debt-platform turnover (Rs. crore) for the current/last
    trading day.

    Endpoint (verified): /api/liveCorp-bonds?index=<idx>&marketType=CBM
    Row schema: {isin, descriptor, ltp, lty, noOfTrades,
                 tradeValue (Rs. LAKH, aggregated per ISIN), wap, way}
    NOTE: /api/debt-rfq is the RFQ ORDER-BOOK view (records.data), not
    trades. obpp_turnover is None because the trade feed aggregates per
    ISIN, so sub-Rs-1-lakh (OBPP-style) trades cannot be isolated from it.
    """
    s = _session("https://www.nseindia.com/market-data/"
                 "debt-market-request-for-quote-rfq")
    s.get("https://www.nseindia.com/", timeout=30)  # warm Akamai cookies
    total_lakh = 0.0
    trades = 0
    for idx in RFQ_TRADE_INDEXES:
        r = s.get("https://www.nseindia.com/api/liveCorp-bonds",
                  params={"index": idx, "marketType": "CBM"}, timeout=30)
        if r.status_code != 200:
            raise FetchError(f"NSE RFQ {idx}: HTTP {r.status_code}")
        for row in r.json().get("data", []):
            total_lakh += float(row.get("tradeValue") or 0)
            trades += int(row.get("noOfTrades") or 0)
    if total_lakh <= 0:
        raise FetchError("NSE RFQ: zero turnover (holiday/weekend?)")
    return {"rfq_turnover": round(total_lakh * CRORE_PER_LAKH, 2),
            "rfq_trades": trades,
            "obpp_turnover": None}


def fetch_nse_egr(d: date | None = None) -> dict:
    """NSE EGR (Electronic Gold Receipts) daily turnover in Rs. crore.
    Endpoint (verified):
      /api/NextApi/apiClient/egrApi?functionName=getEGRNMData
    Row schema: {symbol, series:'EG', totalTurnover (Rs.),
                 totalTradedVolume, lastPrice, orderBook{...}}."""
    s = _session("https://www.nseindia.com/market-data/"
                 "gold-electronic-gold-receipts")
    s.get("https://www.nseindia.com/", timeout=30)
    r = s.get("https://www.nseindia.com/api/NextApi/apiClient/egrApi",
              params={"functionName": "getEGRNMData"}, timeout=30)
    if r.status_code != 200:
        raise FetchError(f"NSE EGR: HTTP {r.status_code}")
    data = r.json().get("data", [])
    if not data:
        raise FetchError("NSE EGR: empty data")
    total = sum(float(x.get("totalTurnover") or 0) for x in data)
    return {"egr_turnover": round(total / 1e7, 4)}


def fetch_nse_debt_bg_monthly(from_yr: int, to_yr: int) -> dict:
    """NSE debt business-growth MONTHLY history, Rs. crore. Endpoints
    (verified; yearly works without params):
      /api/historicalOR/{rfq|cbrics|wdm|rdm|triparty}/tbg/yearly
      /api/historicalOR/.../tbg/monthly?from=<FY-start>&to=<FY-end>
    Row: {INSTRUMENT, BG_FOR:'JUL-2026', TRADING_DAYS, TOTAL_TRADES_COUNT,
          TOTAL_TRADE_VALUE (Rs. crore), AVG_TRADE_VALUE, AVG_TRADE_SIZE}"""
    s = _session("https://www.nseindia.com/debt/"
                 "historical_debt_businessgrowth")
    s.get("https://www.nseindia.com/", timeout=30)
    out: dict = {}
    for seg in ("rfq", "cbrics"):
        r = s.get(f"https://www.nseindia.com/api/historicalOR/{seg}/tbg/"
                  "monthly", params={"from": from_yr, "to": to_yr},
                  timeout=30)
        if r.status_code != 200:
            raise FetchError(f"NSE {seg} tbg: HTTP {r.status_code}")
        out[seg] = [x.get("data", {}) for x in r.json().get("data", [])]
    return out


def fetch_bse_starmf(d1: date, d2: date) -> dict:
    """BSE StAR MF orders/turnover between two dates (inclusive). Endpoint
    (verified; NOT geo-blocked):
      api.bseindia.com/BseIndiaAPI/api/MfMarketTurnOver_Bata/w
        ?fromdate=YYYY/MM/DD&todate=YYYY/MM/DD
    Row: {Date, Subscription_order, Subscription_Value, Redemption_order,
          Redemption_Value, Total_order, TotalVal (Rs.)}"""
    r = _get("https://api.bseindia.com/BseIndiaAPI/api/"
             "MfMarketTurnOver_Bata/w"
             f"?fromdate={d1:%Y/%m/%d}&todate={d2:%Y/%m/%d}",
             "https://www.bseindia.com/markets/MutualFund/TurnOverHLMF")
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        raise FetchError("BSE StAR MF: empty/unexpected response")
    return {"bse_mf_orders": sum(int(x.get("Total_order") or 0)
                                 for x in rows),
            "bse_mf_value": round(sum(float(x.get("TotalVal") or 0)
                                      for x in rows) / 1e7, 2)}


# Registered separately: from GitHub's US-hosted runners these will only
# record fetch errors (geo-block). Run pipeline/update.py from an India
# machine or a self-hosted runner to fill them.
INDIA_ONLY_FETCHERS = {
    "nse_rfq": fetch_nse_rfq_obpp,
    "nse_egr": fetch_nse_egr,
}
DAILY_FETCHERS.update(INDIA_ONLY_FETCHERS)
