# NSE Market Share Command Center

A CBDO dashboard tracking NSE's market share % against BSE (cash market, equity futures, options premium) and MCX (commodity derivatives), auto-updated daily, with rule-based insights validated against news headlines.

**Live architecture (all free):**

```
GitHub Actions (daily 19:30 IST cron)
   └─ pipeline/update.py
        ├─ NSE archives  → Market Activity CSV (cash) + F&O/COM UDIFF bhavcopy
        ├─ BSE bhavcopy  → CM + derivatives UDIFF CSV
        ├─ MCX web API   → daily bhavcopy totals
        └─ Google News / ET Markets RSS → insight validation
   └─ commits JSON to site/data/  →  Cloudflare Pages auto-redeploys
```

The "database" is versioned JSON in git — every historical value is auditable, and Cloudflare Pages serves it globally for free.

## Deploy in ~10 minutes

### 1. Create accounts (both free)
- GitHub: https://github.com/signup
- Cloudflare: https://dash.cloudflare.com/sign-up

### 2. Push this folder to a new GitHub repo
```bash
cd nse-market-share-dashboard
git init && git add -A && git commit -m "initial"
# create a repo named nse-dashboard on github.com, then:
git remote add origin https://github.com/<your-username>/nse-dashboard.git
git branch -M main && git push -u origin main
```

### 3. Allow the workflow to commit
GitHub repo → Settings → Actions → General → Workflow permissions → select **Read and write permissions** → Save.

### 4. Backfill 12 months of history (one-time)
Repo → Actions → "Update market data" → **Run workflow** → set `backfill_days` = `365` → Run.
Takes ~30–45 min (fetches ~250 trading days politely at 1.5s intervals). Check the run log; the data-quality panel on the dashboard will flag any source that failed.

### 5. Connect Cloudflare Pages
1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git**
2. Select your `nse-dashboard` repo
3. Build settings: Framework preset = **None**, Build command = *(empty)*, Build output directory = **`site`**
4. Deploy. Your dashboard is live at `https://<project>.pages.dev`

Every daily data commit from the Action triggers an automatic redeploy. No further maintenance needed.

## What the dashboard shows
- **KPI cards** — latest NSE share % per segment with session-over-session delta
- **Monthly trend** — 12-month NSE share % lines for all four segments
- **Daily trend** — last 30 sessions (catches expiry-day swings)
- **Turnover** — stacked ₹ crore bars, NSE vs rival, per segment (tab switch)
- **Insights** — rule-based signals (DoD/WoW/MoM shifts, 12-month highs/lows, streaks) each cross-linked to matching news headlines from the last 14 days
- **Data quality footer** — flags any day/source that failed to fetch

## Data sources & caveats
| Segment | NSE source | Rival source |
|---|---|---|
| Cash market | NSE archives Market Activity CSV | BSE equity bhavcopy (UDIFF) |
| Equity futures | NSE F&O UDIFF bhavcopy (notional) | BSE derivatives bhavcopy |
| Options | Same, **premium** turnover (industry-standard comparison) | Same |
| Commodity | NSE COM UDIFF bhavcopy | MCX bhavcopy API |
| Debt | NSE business-growth API (best effort — geo-blocked outside India; shown only when reachable) | — |

- NSE's main website geo-blocks non-India IPs; this pipeline deliberately uses the **archives file server**, which is not blocked (verified).
- Options are compared on **premium turnover**, the metric SEBI and analysts use (notional would inflate NSE's share to ~100% and hide the real trend).
- MCX endpoints occasionally change; the fetcher tries multiple known endpoints and any failure appears in the dashboard's data-quality panel rather than silently corrupting shares.
- Exchange holidays produce no files; the pipeline skips them automatically and each daily run re-checks the previous 7 days to self-heal.

## Local test
```bash
pip install -r pipeline/requirements.txt
python pipeline/update.py --date 2026-07-02   # single day
python -m http.server -d site 8000            # view dashboard
```
