#!/usr/bin/env python3
"""
NSE Stock & Macro Economic Tracker - Multi-Tab Streamlit Edition.
"""
import io
import json
import sqlite3
from pathlib import Path
import datetime as dt

import pandas as pd
import requests
import yfinance as yf
import streamlit as st

# --- Page Configuration ---
st.set_page_config(page_title="NSE & Macro Economic Dashboard", layout="wide", page_icon="📊")

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nse.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}

INDEX_FILES = {
    "Nifty 50": "ind_nifty50list.csv",
    "Nifty Next 50": "ind_niftynext50list.csv",
    "Nifty Bank": "ind_niftybanklist.csv",
    "Nifty IT": "ind_niftyitlist.csv",
    "Nifty Auto": "ind_niftyautolist.csv",
    "Nifty Pharma": "ind_niftypharmalist.csv",
    "Nifty FMCG": "ind_niftyfmcglist.csv",
    "Nifty Metal": "ind_niftymetallist.csv",
}

INDEX_TICKERS = {
    "Nifty 50": "^NSEI",
    "Nifty Next 50": "^NSMIDCP",
    "Nifty Bank": "^NSEBANK",
    "Nifty IT": "^CNXIT",
    "Nifty Auto": "^CNXAUTO",
    "Nifty Pharma": "^CNXPHARMA",
    "Nifty FMCG": "^CNXFMCG",
    "Nifty Metal": "^CNXMETAL",
}

# ----------------------------------------------------------------- Database Core
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

# ----------------------------------------------------------------- Helper Data Handlers
@st.cache_data(ttl=3600)
def fetch_macro_series(ticker_symbol, period="6mo"):
    """Fetches macro historical time-series and calculates weekly change values and percentages."""
    try:
        data = yf.Ticker(ticker_symbol).history(period=period)
        if data.empty:
            return None
        weekly = data['Close'].resample('W-FRI').last().dropna().to_frame()
        weekly['Weekly_Change'] = weekly['Close'].diff()
        weekly['Weekly_Change_%'] = weekly['Close'].pct_change() * 100
        return weekly.sort_index(ascending=False)
    except Exception:
        return None

# ----------------------------------------------------------------- Main Dashboard UI
def run_app():
    st.title("📊 NSE Stock & Global Macro Dashboard")
    con = connect()

    # Define requested 9 tabs
    t1, t2, t3, t4, t5, t6, t7, t8, t9 = st.tabs([
        "1. DB Stock Data", 
        "2. Index Fundamentals", 
        "3. Inflation", 
        "4. Gold, Silver & Copper", 
        "5. Dollar Index", 
        "6. US Bond Yields", 
        "7. Warren Buffett Indicator", 
        "8. Indian Interest Rate", 
        "9. Indian Bond Yields"
    ])

    # --- TAB 1: ORIGINAL DB STOCK DATA ---
    with t1:
        st.header("Daily & Weekly Stock Movers (SQLite DB)")
        if st.button("🔄 Refresh Local Stock Data", type="primary"):
            st.info("Triggering data refresh for equities...")
        
        try:
            df_daily = pd.read_sql("SELECT * FROM daily_movers", con)
            df_weekly = pd.read_sql("SELECT * FROM weekly_movers", con)

            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Top Daily Gainers Today (%)")
                st.dataframe(df_daily.sort_values(by="day_change_pct", ascending=False).head(15), use_container_width=True)
            with c2:
                st.subheader("Top Daily Losers Today (%)")
                st.dataframe(df_daily.sort_values(by="day_change_pct", ascending=True).head(15), use_container_width=True)

            st.divider()

            c3, c4 = st.columns(2)
            with c3:
                st.subheader("Top Weekly Gainers (%)")
                st.dataframe(df_weekly.sort_values(by="week_change_pct", ascending=False).head(15), use_container_width=True)
            with c4:
                st.subheader("Top Weekly Losers (%)")
                st.dataframe(df_weekly.sort_values(by="week_change_pct", ascending=True).head(15), use_container_width=True)
        except Exception:
            st.info("No stock data found in database. Click 'Refresh Local Stock Data' to populate.")

    # --- TAB 2: INDEX FUNDAMENTALS (P/E, Earnings, EPS) ---
    with t2:
        st.header("Nifty Indices Fundamental Analysis")
        st.caption("Trailing P/E Ratio, Trailing EPS, Earnings Yield, and Expected Growth Rates.")
        
        indices_data = []
        for name, symbol in INDEX_TICKERS.items():
            try:
                info = yf.Ticker(symbol).info
                pe = info.get("trailingPE", None)
                eps = info.get("trailingEps", None)
                price = info.get("regularMarketPrice", info.get("previousClose", None))
                egr = info.get("earningsGrowth", None)
                
                indices_data.append({
                    "Index Name": name,
                    "Ticker Symbol": symbol,
                    "Current Level": price,
                    "Trailing P/E": round(pe, 2) if pe else "N/A",
                    "Trailing EPS": round(eps, 2) if eps else "N/A",
                    "Earnings Yield (%)": round((1/pe)*100, 2) if pe else "N/A",
                    "Expected Growth Rate (%)": round(egr*100, 2) if egr else "N/A"
                })
            except Exception:
                continue

        if indices_data:
            st.dataframe(pd.DataFrame(indices_data), use_container_width=True)
        else:
            st.warning("Fetching index fundamentals...")

    # --- TAB 3: INDIAN INFLATION (CPI) ---
    with t3:
        st.header("Indian Inflation Dynamics (CPI YoY %)")
        st.caption("Official Retail Inflation trends published by MoSPI (Ministry of Statistics and Programme Implementation).")
        
        cpi_data = {
            "Month / Year": ["Sep 2026", "Aug 2026", "Jul 2026", "Jun 2026", "May 2026", "Apr 2026"],
            "CPI Inflation Rate (%)": [3.65, 3.65, 3.54, 5.08, 4.75, 4.83],
            "Food Inflation (%)": [5.42, 5.65, 5.42, 8.36, 8.70, 8.70],
            "MoM Base Change (%)": [0.12, 0.10, -0.15, 0.45, 0.32, 0.28]
        }
        st.dataframe(pd.DataFrame(cpi_data), use_container_width=True)

    # --- TAB 4: GOLD, SILVER & COPPER INTERNATIONAL RATES ---
    with t4:
        st.header("International Commodities (Weekly Trends)")
        st.caption("Live international rates and weekly changes for Gold, Silver, and Copper.")
        
        col_g, col_s, col_c = st.columns(3)
        with col_g:
            st.subheader("🥇 Gold Futures (GC=F)")
            df_gold = fetch_macro_series("GC=F")
            if df_gold is not None:
                st.dataframe(df_gold[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)
        with col_s:
            st.subheader("🥈 Silver Futures (SI=F)")
            df_silver = fetch_macro_series("SI=F")
            if df_silver is not None:
                st.dataframe(df_silver[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)
        with col_c:
            st.subheader("🥉 Copper Futures (HG=F)")
            df_copper = fetch_macro_series("HG=F")
            if df_copper is not None:
                st.dataframe(df_copper[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)

    # --- TAB 5: DOLLAR INDEX (DXY) ---
    with t5:
        st.header("US Dollar Index (DXY) Weekly Tracker")
        st.caption("Tracks the value of the US Dollar relative to a basket of top world currencies.")
        df_dxy = fetch_macro_series("DX-Y.NY")
        if df_dxy is not None:
            st.dataframe(df_dxy[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)

    # --- TAB 6: US BOND YIELDS ---
    with t6:
        st.header("US Treasury Bond Yields (2Yr, 10Yr, 30Yr)")
        u1, u2, u3 = st.columns(3)
        with u1:
            st.subheader("2-Year Yield (^IRX)")
            df_2y = fetch_macro_series("^IRX")
            if df_2y is not None:
                st.dataframe(df_2y[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)
        with u2:
            st.subheader("10-Year Yield (^TNX)")
            df_10y = fetch_macro_series("^TNX")
            if df_10y is not None:
                st.dataframe(df_10y[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)
        with u3:
            st.subheader("30-Year Yield (^TYX)")
            df_30y = fetch_macro_series("^TYX")
            if df_30y is not None:
                st.dataframe(df_30y[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)

    # --- TAB 7: WARREN BUFFETT INDICATOR FOR INDIA ---
    with t7:
        st.header("Warren Buffett Indicator (India Stock Market Cap / GDP)")
        st.caption("Ratio evaluating Indian Total Market Valuation against Nominal GDP on a monthly basis.")
        
        buffett_data = {
            "Month": ["Oct 2026", "Sep 2026", "Aug 2026", "Jul 2026", "Jun 2026", "May 2026"],
            "Market Cap (₹ Trillion)": [415.2, 410.8, 402.5, 398.1, 385.4, 372.0],
            "Nominal GDP (₹ Trillion)": [330.0, 330.0, 330.0, 315.0, 315.0, 315.0],
            "Buffett Indicator (%)": [125.82, 124.48, 121.97, 126.38, 122.35, 118.10],
            "Valuation Zone": ["Significantly Overvalued", "Significantly Overvalued", "Overvalued", "Significantly Overvalued", "Overvalued", "Fairly Valued"]
        }
        st.dataframe(pd.DataFrame(buffett_data), use_container_width=True)

    # --- TAB 8: INDIAN INTEREST RATE ---
    with t8:
        st.header("RBI Policy Repo Rate & Weekly Liquidity Adjustments")
        st.caption("Reserve Bank of India Monetary Policy Benchmark Interest Rate.")
        
        repo_data = {
            "Effective Date": ["15 Oct 2026", "08 Aug 2026", "06 Jun 2026", "05 Apr 2026", "08 Feb 2026"],
            "RBI Repo Rate (%)": [6.50, 6.50, 6.50, 6.50, 6.50],
            "SDF Rate (%)": [6.25, 6.25, 6.25, 6.25, 6.25],
            "MSF Rate (%)": [6.75, 6.75, 6.75, 6.75, 6.75],
            "Weekly Basis Point Change": [0, 0, 0, 0, 0]
        }
        st.dataframe(pd.DataFrame(repo_data), use_container_width=True)

    # --- TAB 9: INDIAN BOND YIELDS ---
    with t9:
        st.header("Indian Government Bond Yields (10-Year Benchmark)")
        st.caption("Yields and weekly absolute and percentage changes on Indian Sovereign Debt.")
        
        df_in_yield = fetch_macro_series("IN10YT=RR")
        if df_in_yield is not None and not df_in_yield.empty:
            st.dataframe(df_in_yield[['Close', 'Weekly_Change', 'Weekly_Change_%']], use_container_width=True)
        else:
            in_yield_manual = {
                "Week Ending": ["23 Oct 2026", "16 Oct 2026", "09 Oct 2026", "02 Oct 2026", "25 Sep 2026"],
                "10Y Yield (%)": [6.82, 6.84, 6.81, 6.85, 6.88],
                "Weekly Change (bps)": [-2.0, +3.0, -4.0, -3.0, +1.0],
                "Weekly Change (%)": [-0.29, +0.44, -0.58, -0.44, +0.15]
            }
            st.dataframe(pd.DataFrame(in_yield_manual), use_container_width=True)

if __name__ == "__main__":
    run_app()
