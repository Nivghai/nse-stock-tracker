#!/usr/bin/env python3
"""
NSE daily / weekly mover tracker.

Run once after market close (after ~4:00 PM IST):
    python nse_tracker.py

It will
  1. download the list of all NSE equities + index constituents,
  2. download recent closing prices (Yahoo Finance) into a local SQLite DB (nse.db),
  3. compute day % and week % change for every stock and index,
  4. write report.html (open it in any browser).

Options:
    python nse_tracker.py --no-fetch      rebuild report from the DB only
    python nse_tracker.py --period 3mo    pull a longer price history
"""
import argparse
import datetime as dt
import io
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nse.db"
REPORT_PATH = HERE / "report.html"
TEMPLATE_PATH = HERE / "report_template.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}

EQUITY_LIST_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
INDEX_LIST_BASE = "https://nsearchives.nseindia.com/content/indices/"

# Index name -> constituent CSV published by NSE
INDEX_FILES = {
    "Nifty 50": "ind_nifty50list.csv",
    "Nifty Next 50": "ind_niftynext50list.csv",
    "Nifty 100": "ind_nifty100list.csv",
    "Nifty 200": "ind_nifty200list.csv",
    "Nifty 500": "ind_nifty500list.csv",
    "Nifty Midcap 150": "ind_niftymidcap150list.csv",
    "Nifty Smallcap 250": "ind_niftysmallcap250list.csv",
    "Nifty Bank": "ind_niftybanklist.csv",
    "Nifty Financial Services": "ind_niftyfinancelist.csv",
    "Nifty IT": "ind_niftyitlist.csv",
    "Nifty Auto": "ind_niftyautolist.csv",
    "Nifty Pharma": "ind_niftypharmalist.csv",
    "Nifty FMCG": "ind_niftyfmcglist.csv",
    "Nifty Metal": "ind_niftymetallist.csv",
    "Nifty Energy": "ind_niftyenergylist.csv",
    "Nifty Realty": "ind_niftyrealtylist.csv",
}

# Index name -> Yahoo Finance ticker for the index level itself.
# Any ticker Yahoo doesn't return is skipped; the index still appears
# in the report with its constituent counts.
INDEX_TICKERS = {
    "Nifty 50": "^NSEI",
    "Nifty Next 50": "^NSMIDCP",
    "Nifty 500": "^CRSLDX",
    "Nifty Bank": "^NSEBANK",
    "Nifty IT": "^CNXIT",
    "Nifty Auto": "^CNXAUTO",
    "Nifty Pharma": "^CNXPHARMA",
    "Nifty FMCG": "^CNXFMCG",
    "Nifty Metal": "^CNXMETAL",
    "Nifty Energy": "^CNXENERGY",
    "Nifty Realty": "^CNXREALTY",
}


# ----------------------------------------------------------------- database

VIEWS = """
DROP VIEW IF EXISTS daily_movers;
CREATE VIEW daily_movers AS
SELECT p.symbol, s.name,
       y.close AS yesterday_close, p.close AS today_close,
       ROUND((p.close / y.close - 1) * 100, 2) AS day_change_pct
FROM prices p
JOIN stocks s ON s.symbol = p.symbol
JOIN prices y ON y.symbol = p.symbol
 AND y.date = (SELECT MAX(date) FROM prices WHERE symbol = p.symbol AND date < p.date)
WHERE p.date = (SELECT MAX(date) FROM prices);

DROP VIEW IF EXISTS weekly_movers;
CREATE VIEW weekly_movers AS
SELECT p.symbol, s.name,
       b.close AS last_week_close, p.close AS latest_close,
       ROUND((p.close / b.close - 1) * 100, 2) AS week_change_pct
FROM prices p
JOIN stocks s ON s.symbol = p.symbol
JOIN prices b ON b.symbol = p.symbol
 AND b.date = (SELECT MAX(date) FROM prices WHERE symbol = p.symbol AND date <
      date((SELECT MAX(date) FROM prices),
           '-' || ((CAST(strftime('%w', (SELECT MAX(date) FROM prices)) AS INTEGER) + 6) % 7) || ' days'))
WHERE p.date = (SELECT MAX(date) FROM prices);
"""

def connect():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS prices (
            symbol TEXT NOT NULL, date TEXT NOT NULL, close REAL NOT NULL,
            PRIMARY KEY (symbol, date));
        CREATE TABLE IF NOT EXISTS stocks (
            symbol TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE IF NOT EXISTS constituents (
            index_name TEXT NOT NULL, symbol TEXT NOT NULL,
            PRIMARY KEY (index_name, symbol));
        """
    )
    con.executescript(VIEWS)
    return con


# ----------------------------------------------------------------- NSE lists
def get_csv(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip() for c in df.columns]
    return df


def refresh_lists(con):
    try:
        eq = get_csv(EQUITY_LIST_URL)
        eq = eq[eq["SERIES"].astype(str).str.strip() == "EQ"]
        rows = [(s.strip(), n.strip()) for s, n in zip(eq["SYMBOL"], eq["NAME OF COMPANY"])]
        con.executemany("INSERT OR REPLACE INTO stocks VALUES (?,?)", rows)
        print(f"  {len(rows)} NSE equities")
    except Exception as e:
        print(f"  ! could not refresh equity list ({e}); using what is in the DB")

    for index_name, fname in INDEX_FILES.items():
        try:
            df = get_csv(INDEX_LIST_BASE + fname)
            syms = [s.strip() for s in df["Symbol"].dropna()]
            con.execute("DELETE FROM constituents WHERE index_name=?", (index_name,))
            con.executemany("INSERT OR REPLACE INTO constituents VALUES (?,?)",
                            [(index_name, s) for s in syms])
            # make sure constituents exist in the stock table even if not in EQUITY_L
            if "Company Name" in df.columns:
                con.executemany("INSERT OR IGNORE INTO stocks VALUES (?,?)",
                                [(s.strip(), n.strip()) for s, n in zip(df["Symbol"], df["Company Name"])])
            print(f"  {index_name}: {len(syms)} constituents")
        except Exception as e:
            print(f"  ! {index_name}: skipped ({e})")
    con.commit()


# -------------------------------------------------------------------- prices
def download_closes(ticker_map, period):
    """ticker_map: {yahoo_ticker: db_symbol}. Returns long DataFrame."""
    tickers = list(ticker_map)
    frames = []
    for i in range(0, len(tickers), 150):
        chunk = tickers[i:i + 150]
        try:
            df = yf.download(chunk, period=period, interval="1d", auto_adjust=False,
                             progress=False, threads=True)
        except Exception as e:
            print(f"  ! batch {i // 150 + 1} failed: {e}")
            continue
        if df is None or df.empty:
            continue
        close = df["Close"]
        if isinstance(close, pd.Series):
            close = close.to_frame(chunk[0])
        idx = pd.to_datetime(close.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        close.index = idx.normalize()
        close.index.name = "date"
        long = close.reset_index().melt(id_vars="date", var_name="ticker", value_name="close")
        frames.append(long.dropna())
        print(f"  prices: {min(i + 150, len(tickers))}/{len(tickers)}")
    if not frames:
        return pd.DataFrame(columns=["date", "symbol", "close"])
    out = pd.concat(frames)
    out["symbol"] = out["ticker"].map(ticker_map)
    return out[["date", "symbol", "close"]]


def refresh_prices(con, period):
    stock_syms = [r[0] for r in con.execute("SELECT symbol FROM stocks")]
    ticker_map = {f"{s}.NS": s for s in stock_syms}
    ticker_map.update({t: t for t in INDEX_TICKERS.values()})
    long = download_closes(ticker_map, period)
    if long.empty:
        print("  ! no prices downloaded")
        return
    con.executemany(
        "INSERT OR REPLACE INTO prices VALUES (?,?,?)",
        [(r.symbol, r.date.strftime("%Y-%m-%d"), float(r.close)) for r in long.itertuples()],
    )
    con.commit()
    print(f"  stored {len(long)} price rows")


# ------------------------------------------------------------------ compute
def compute(con):
    px = pd.read_sql(
        "SELECT date, symbol, close FROM prices "
        "WHERE date >= date((SELECT MAX(date) FROM prices), '-40 day')",
        con, parse_dates=["date"])
    if px.empty:
        sys.exit("No prices in the database yet. Run without --no-fetch first.")
    wide = px.pivot(index="date", columns="symbol", values="close").sort_index()

    latest = wide.index[-1]
    monday = latest - pd.Timedelta(days=latest.weekday())
    last = wide.loc[latest]
    prev = wide[wide.index < latest].ffill().iloc[-1]
    before_week = wide[wide.index < monday]
    base = before_week.ffill().iloc[-1] if len(before_week) else pd.Series(dtype=float)

    res = pd.DataFrame({"c": last})
    res["d"] = (last / prev - 1) * 100
    res["w"] = (last / base.reindex(last.index) - 1) * 100
    res = res.dropna(subset=["c"])  # not traded on the latest date -> excluded
    return res, latest, before_week.index[-1] if len(before_week) else None


def num(x):
    return None if pd.isna(x) else round(float(x), 2)


def build_payload(con):
    res, latest, week_base_date = compute(con)

    names = dict(con.execute("SELECT symbol, name FROM stocks"))
    members = {}
    for idx, sym in con.execute("SELECT index_name, symbol FROM constituents"):
        members.setdefault(sym, []).append(idx)

    stock_rows = []
    for sym, r in res.iterrows():
        if sym.startswith("^"):
            continue
        stock_rows.append({"s": sym, "n": names.get(sym, sym), "c": num(r.c),
                           "d": num(r.d), "w": num(r.w), "ix": sorted(members.get(sym, []))})

    index_rows = []
    for name in INDEX_FILES:
        t = INDEX_TICKERS.get(name)
        lvl = res.loc[t] if t in res.index else None
        count = sum(1 for r in stock_rows if name in r["ix"])
        index_rows.append({"n": name, "m": count,
                           "c": num(lvl.c) if lvl is not None else None,
                           "d": num(lvl.d) if lvl is not None else None,
                           "w": num(lvl.w) if lvl is not None else None})

    return {
        "asof": latest.strftime("%a %d %b %Y"),
        "weekBase": week_base_date.strftime("%a %d %b %Y") if week_base_date is not None else None,
        "generated": dt.datetime.now().strftime("%d %b %Y %H:%M"),
        "stocks": stock_rows,
        "indices": index_rows,
    }


def write_report(payload):
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    REPORT_PATH.write_text(html.replace("__DATA__", data), encoding="utf-8")
    print(f"\nReport written: {REPORT_PATH}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="skip downloads, rebuild report only")
    ap.add_argument("--period", default="1mo", help="price history to pull (yfinance period, default 1mo)")
    args = ap.parse_args()

    con = connect()
    if not args.no_fetch:
        print("Refreshing NSE stock and index lists...")
        refresh_lists(con)
        print("Downloading prices...")
        refresh_prices(con, args.period)
    write_report(build_payload(con))


if __name__ == "__main__":
    main()
