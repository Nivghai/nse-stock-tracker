#!/usr/bin/env python3
"""
NSE daily / weekly mover tracker - Complete Streamlit Cloud Edition.
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
import streamlit as st
import streamlit.components.v1 as components

# --- Streamlit Page Configuration ---
st.set_page_config(page_title="NSE Stock Tracker", layout="wide", page_icon="📈")

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

# ----------------------------------------------------------------- Database Initialization
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


# ----------------------------------------------------------------- NSE Stock & Index Fetching
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
        st.write(f"✅ Downloaded {len(rows)} NSE stock symbols")
    except Exception as e:
        st.warning(f"Could not refresh equity list ({e}); using existing DB records.")

    for index_name, fname in INDEX_FILES.items():
        try:
            df = get_csv(INDEX_LIST_BASE + fname)
            syms = [s.strip() for s in df["Symbol"].dropna()]
            con.execute("DELETE FROM constituents WHERE index_name=?", (index_name,))
            con.executemany("INSERT OR REPLACE INTO constituents VALUES (?,?)",
                            [(index_name, s) for s in syms])
            if "Company Name" in df.columns:
                con.executemany("INSERT OR IGNORE INTO stocks VALUES (?,?)",
                                [(s.strip(), n.strip()) for s, n in zip(df["Symbol"], df["Company Name"])])
        except Exception:
            pass
    con.commit()


# -------------------------------------------------------------------- Price History Downloads
def download_closes(ticker_map, period):
    tickers = list(ticker_map)
    frames = []
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    for i in range(0, len(tickers), 150):
        chunk = tickers[i:i + 150]
        try:
            df = yf.download(chunk, period=period, interval="1d", auto_adjust=False,
                             progress=False, threads=True)
        except Exception:
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
        
        pct = min(i + 150, len(tickers)) / len(tickers)
        progress_bar.progress(pct)
        status_text.text(f"Downloading prices: {min(i + 150, len(tickers))}/{len(tickers)} symbols...")
        
    progress_bar.empty()
    status_text.empty()
    
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
        st.error("No prices returned from Yahoo Finance.")
        return
    con.executemany(
        "INSERT OR REPLACE INTO prices VALUES (?,?,?)",
        [(r.symbol, r.date.strftime("%Y-%m-%d"), float(r.close)) for r in long.itertuples()],
    )
    con.commit()
    st.success(f"Stored {len(long)} price rows in database.")


# ------------------------------------------------------------------ Market Calculations
def compute(con):
    px = pd.read_sql(
        "SELECT date, symbol, close FROM prices "
        "WHERE date >= date((SELECT MAX(date) FROM prices), '-40 day')",
        con, parse_dates=["date"])
    if px.empty:
        return None, None, None
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
    res = res.dropna(subset=["c"])
    return res, latest, before_week.index[-1] if len(before_week) else None


def num(x):
    return None if pd.isna(x) else round(float(x), 2)


def build_payload(con):
    res, latest, week_base_date = compute(con)
    if res is None:
        return None

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
    if not payload or not TEMPLATE_PATH.exists():
        return
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    REPORT_PATH.write_text(html.replace("__DATA__", data), encoding="utf-8")


# ------------------------------------------------------------------ Main UI Application
def run_app():
    st.title("📈 NSE Stock & Index Mover Tracker")
    st.markdown("Track daily and weekly movers across all NSE equities and indices.")

    con = connect()

    # Main Page Refresh Controls
    col_select, col_btn = st.columns([1, 2])
    with col_select:
        period = st.selectbox("Price History Fetch Period", ["1mo", "3mo", "6mo"], index=0)
    with col_btn:
        st.write("")
        st.write("")
        if st.button("🔄 Fetch Latest Market Data", type="primary", use_container_width=True):
            with st.spinner("Fetching stock list and downloading daily prices..."):
                refresh_lists(con)
                refresh_prices(con, period)
                payload = build_payload(con)
                write_report(payload)
                st.success("Market Data Updated Successfully!")
                st.rerun()

    st.divider()

    # Dashboard Tabs
    tab1, tab2, tab3 = st.tabs(["📊 Interactive Views", "📰 HTML Report View", "ℹ️ Database Summary"])

    with tab1:
        try:
            df_daily = pd.read_sql("SELECT * FROM daily_movers", con)
            df_weekly = pd.read_sql("SELECT * FROM weekly_movers", con)

            st.subheader("Top Daily Movers")
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("**Top Gainers Today (%)**")
                st.dataframe(df_daily.sort_values(by="day_change_pct", ascending=False).head(15), use_container_width=True)
            with col2:
                st.markdown("**Top Losers Today (%)**")
                st.dataframe(df_daily.sort_values(by="day_change_pct", ascending=True).head(15), use_container_width=True)

            st.divider()

            st.subheader("Top Weekly Movers")
            col3, col4 = st.columns(2)
            with col3:
                st.markdown("**Top Gainers This Week (%)**")
                st.dataframe(df_weekly.sort_values(by="week_change_pct", ascending=False).head(15), use_container_width=True)
            with col4:
                st.markdown("**Top Losers This Week (%)**")
                st.dataframe(df_weekly.sort_values(by="week_change_pct", ascending=True).head(15), use_container_width=True)

        except Exception:
            st.info("No market data found in database. Click **'🔄 Fetch Latest Market Data'** above to populate data.")

    with tab2:
        if REPORT_PATH.exists():
            report_html = REPORT_PATH.read_text(encoding="utf-8")
            components.html(report_html, height=800, scrolling=True)
        else:
            st.info("HTML Report not generated yet. Click 'Fetch Latest Market Data' first.")

    with tab3:
        try:
            total_stocks = con.execute("SELECT COUNT(*) FROM stocks").fetchone()[0]
            total_prices = con.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            st.metric("Tracked Equities", total_stocks)
            st.metric("Total Price Rows in DB", total_prices)
        except Exception:
            st.write("Database is uninitialized.")

if __name__ == "__main__":
    run_app()
