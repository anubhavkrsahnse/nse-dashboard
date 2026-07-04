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
