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
    """Sum turnover from a SEBI UDIFF-format bhavcopy.
    Futures rows: FinInstrmTp in (STF, IDF) -> notional value.
    Options rows: FinInstrmTp in (STO, IDO) -> TtlTrfVal is premium value.
    UDIFF TtlTrfVal is in absolute rupees -> convert to crore."""
    fut = opt = 0.0
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        try:
            v = float(row.get("TtlTrfVal") or 0)
        except ValueError:
            continue
        t = (row.get("FinInstrmTp") or "").strip()
        if t in ("STF", "IDF"):
            fut += v
        elif t in ("STO", "IDO"):
            opt += v
    return {"fut": fut / 1e7, "optp": opt / 1e7}  # rupees -> crore


def fetch_nse_fo(d: date) -> dict:
    """NSE F&O daily futures notional + options premium via UDIFF bhavcopy
    on the archives server."""
    url = (f"https://nsearchives.nseindia.com/content/fo/"
           f"BhavCopy_NSE_FO_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    try:
        r = _get(url, "https://www.nseindia.com/")
    except FetchError:
        url = url.replace("nsearchives", "archives")
        r = _get(url, "https://www.nseindia.com/")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]
        res = _sum_udiff(z.read(name).decode("utf-8", "replace"))
    return {"nse_fut": res["fut"], "nse_optp": res["optp"]}


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
    res = _sum_udiff(r.text)
    return {"bse_fut": res["fut"], "bse_optp": res["optp"]}
# ------------------------------------------------------------- Commodity ----

def fetch_nse_com(d: date) -> dict:
    """NSE commodity derivatives via UDIFF bhavcopy (small segment)."""
    url = (f"https://nsearchives.nseindia.com/content/com/"
           f"BhavCopy_NSE_COM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")
    try:
        r = _get(url, "https://www.nseindia.com/")
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    except (FetchError, zipfile.BadZipFile) as e:
        raise FetchError(f"NSE COM {d}: {e}")
    total = 0.0
    for row in csv.DictReader(io.StringIO(text)):
        try:
            total += float(row.get("TtlTrfVal") or 0)
        except ValueError:
            continue
    return {"nse_com": total / 1e7}


MCX_STRATEGIES = [
    # (url, is_post, payload)  - tried in order until one works
    ("https://www.mcxindia.com/backpage.aspx/GetDateWiseBhavCopy", True,
     lambda d: {"Date": d.strftime("%m/%d/%Y"), "InstrumentName": "ALL"}),
    ("https://www.mcxindia.com/backpage.aspx/GetBhavCopyDateWise", True,
     lambda d: {"Date": d.strftime("%m/%d/%Y"), "InstrumentName": "ALL"}),
]


def fetch_mcx(d: date) -> dict:
    """MCX total traded value. MCX web-methods return rows with a 'Value'
    field in Rs. lakh. Tries known endpoints in order."""
    referer = "https://www.mcxindia.com/market-data/bhavcopy"
    last_err = None
    for url, is_post, payload in MCX_STRATEGIES:
        try:
            s = _session(referer)
            s.headers["Content-Type"] = "application/json; charset=UTF-8"
            s.headers["X-Requested-With"] = "XMLHttpRequest"
            # MCX requires a warm cookie from the page first
            s.get(referer, timeout=30)
            r = s.post(url, json=payload(d), timeout=30)
            if r.status_code != 200:
                raise FetchError(f"HTTP {r.status_code}")
            data = r.json().get("d")
            if isinstance(data, str):
                data = json.loads(data)
            rows = data.get("Data") if isinstance(data, dict) else data
            if not rows:
                raise FetchError("empty rows")
            total_lakh = 0.0
            for row in rows:
                for key in ("Value", "ValueInLacs", "TradedValue"):
                    if key in row and row[key] not in (None, ""):
                        total_lakh += float(row[key])
                        break
            if total_lakh <= 0:
                raise FetchError("zero total")
            return {"mcx_com": total_lakh * CRORE_PER_LAKH}
        except Exception as e:  # noqa: BLE001 - try next strategy
            last_err = e
            continue
    raise FetchError(f"MCX {d}: all strategies failed ({last_err})")


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
