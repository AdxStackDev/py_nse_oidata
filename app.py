from flask import Flask, render_template, request, flash, jsonify
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import datetime
import requests
import os
import logging
import json
from concurrent.futures import ThreadPoolExecutor

try:
    import pytz

    IST = pytz.timezone("Asia/Kolkata")
    TIMEZONE_AVAILABLE = True
except ImportError:
    logging.warning("pytz not available, using basic timezone handling")
    IST = None
    TIMEZONE_AVAILABLE = False

# ====================================================
# Flask App Setup
# ====================================================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key_123")


# Custom Jinja2 filter: format a date with strftime
@app.template_filter("strftime")
def jinja_strftime(fmt):
    """Usage: '{{ "%Y-%m-%d" | strftime }}' returns today's date formatted."""
    return datetime.date.today().strftime(fmt)


# Configure logging (StreamHandler only for Vercel compatibility)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[logging.StreamHandler()],
)

# ====================================================
# Helper Functions
# ====================================================


def is_vercel():
    """Check if running on Vercel."""
    return os.getenv("VERCEL") == "1" or os.getenv("VERCEL_ENV") is not None


# ====================================================
# Config
# ====================================================
BASE_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{}.csv"
NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_HOME_URL = "https://www.nseindia.com"
NSE_QUOTE_URL = "https://www.nseindia.com/api/quote-equity?symbol=NIFTY+50"
NSE_INDICES_URL = "https://www.nseindia.com/api/allIndices"

# Cache directory for CSV files (Vercel compatible)
CACHE_DIR = "/tmp/nse_cache" if is_vercel() else "cache"
METADATA_FILE = os.path.join(CACHE_DIR, "download_metadata.json")

# In-memory cache for CSV data (fallback)
_csv_cache = {}

# In-memory cache for Nifty spot / option chain (short-lived)
_live_cache = {}
LIVE_CACHE_TTL = 300  # 5 minutes

# Predefined NSE holidays for 2026
NSE_HOLIDAYS = {
    datetime.date(2026, 1, 15),
    datetime.date(2026, 1, 26),
    datetime.date(2026, 2, 19),
    datetime.date(2026, 3, 3),
    datetime.date(2026, 3, 19),
    datetime.date(2026, 3, 26),
    datetime.date(2026, 3, 31),
    datetime.date(2026, 4, 1),
    datetime.date(2026, 4, 3),
    datetime.date(2026, 4, 14),
    datetime.date(2026, 5, 1),
    datetime.date(2026, 5, 28),
    datetime.date(2026, 6, 26),
    datetime.date(2026, 8, 26),
    datetime.date(2026, 9, 14),
    datetime.date(2026, 10, 2),
    datetime.date(2026, 10, 20),
    datetime.date(2026, 11, 10),
    datetime.date(2026, 11, 24),
    datetime.date(2026, 12, 25),
}

executor = ThreadPoolExecutor(max_workers=5)

# ====================================================
# Utility Functions
# ====================================================


def get_date_string(date: datetime.date) -> str:
    return date.strftime("%d%m%Y")


def adjust_for_holidays(date: datetime.date) -> datetime.date:
    """Ensure date is not weekend or NSE holiday."""
    while date.weekday() > 4 or date in NSE_HOLIDAYS:
        date -= datetime.timedelta(days=1)
    return date


def ensure_cache_dir():
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)


def get_cache_filename(date: datetime.date) -> str:
    date_str = get_date_string(date)
    return os.path.join(CACHE_DIR, f"nse_fo_{date_str}.csv")


def load_download_metadata() -> dict:
    try:
        if os.path.exists(METADATA_FILE):
            with open(METADATA_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load metadata: {e}")
    return {}


def save_download_metadata(metadata: dict):
    try:
        ensure_cache_dir()
        with open(METADATA_FILE, "w") as f:
            json.dump(metadata, f)
    except Exception as e:
        logging.warning(f"Failed to save metadata: {e}")


def get_ist_time():
    """Get current time in IST with fallback."""
    if TIMEZONE_AVAILABLE and IST:
        return datetime.datetime.now(IST)
    else:
        utc_now = datetime.datetime.utcnow()
        ist_offset = datetime.timedelta(hours=5, minutes=30)
        return utc_now + ist_offset


def is_market_open() -> bool:
    """Check if NSE market is currently open."""
    now = get_ist_time()
    if now.weekday() > 4:
        return False
    if now.date() in NSE_HOLIDAYS:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= t <= (15 * 60 + 30)


def is_download_needed(date: datetime.date) -> bool:
    now_ist = get_ist_time()
    target_date_str = get_date_string(date)
    if now_ist.hour < 19:
        return False
    cache_file = get_cache_filename(date)
    if os.path.exists(cache_file):
        metadata = load_download_metadata()
        if target_date_str in metadata:
            try:
                download_time = datetime.datetime.fromisoformat(
                    metadata[target_date_str]["timestamp"]
                )
                if download_time.date() == now_ist.date() and download_time.hour >= 19:
                    return False
            except Exception:
                pass
    return True


def download_and_cache_csv(date: datetime.date, max_retries: int = 3) -> str:
    date_str = get_date_string(date)
    cache_file = get_cache_filename(date)

    if not is_download_needed(date):
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    return f.read()
            except Exception:
                pass
        if date_str in _csv_cache:
            return _csv_cache[date_str]

    url = BASE_URL.format(date_str)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.nseindia.com",
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.ok and response.text.strip():
                content = response.text
                try:
                    ensure_cache_dir()
                    with open(cache_file, "w") as f:
                        f.write(content)
                    metadata = load_download_metadata()
                    metadata[date_str] = {
                        "timestamp": get_ist_time().isoformat(),
                        "file_size": len(content),
                        "status": "success",
                    }
                    save_download_metadata(metadata)
                except Exception:
                    pass
                _csv_cache[date_str] = content
                return content
        except Exception as e:
            logging.error(f"Download attempt {attempt + 1} failed for {url}: {e}")
        if attempt < max_retries - 1:
            import time

            time.sleep(2**attempt)

    if os.path.exists(cache_file):
        try:
            with open(cache_file, "r") as f:
                return f.read()
        except Exception:
            pass
    if date_str in _csv_cache:
        return _csv_cache[date_str]
    return None


def cleanup_old_cache_files(days_to_keep: int = 7):
    try:
        if not os.path.exists(CACHE_DIR):
            return
        cutoff_date = datetime.date.today() - datetime.timedelta(days=days_to_keep)
        for filename in os.listdir(CACHE_DIR):
            if filename.startswith("nse_fo_") and filename.endswith(".csv"):
                try:
                    date_str = filename.replace("nse_fo_", "").replace(".csv", "")
                    file_date = datetime.datetime.strptime(date_str, "%d%m%Y").date()
                    if file_date < cutoff_date:
                        os.remove(os.path.join(CACHE_DIR, filename))
                except Exception:
                    pass
    except Exception:
        pass


# ====================================================
# Live Market Data (Nifty Spot + Option Chain)
# ====================================================


def _nse_session_headers():
    """Return headers with a fresh NSE session cookie."""
    s = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
    }
    try:
        s.get(NSE_HOME_URL, headers=headers, timeout=8)
    except Exception:
        pass
    return s, headers


def get_nifty_spot() -> dict:
    """
    Fetch Nifty 50 spot price from NSE indices API.
    Returns dict with price, change, pct_change or None on failure.
    Cached for LIVE_CACHE_TTL seconds.
    """
    cache_key = "nifty_spot"
    now_ts = datetime.datetime.utcnow().timestamp()

    if cache_key in _live_cache:
        entry = _live_cache[cache_key]
        if now_ts - entry["ts"] < LIVE_CACHE_TTL:
            return entry["data"]

    try:
        s, headers = _nse_session_headers()
        resp = s.get(NSE_INDICES_URL, headers=headers, timeout=8)
        if resp.ok:
            data = resp.json()
            for item in data.get("data", []):
                if item.get("index") == "NIFTY 50":
                    result = {
                        "price": item.get("last", 0),
                        "change": item.get("variation", 0),
                        "pct_change": item.get("percentChange", 0),
                    }
                    _live_cache[cache_key] = {"ts": now_ts, "data": result}
                    return result
    except Exception as e:
        logging.warning(f"Nifty spot fetch failed: {e}")
    return None


def get_option_chain_summary() -> dict:
    """
    Fetch Nifty option chain and return:
      - pcr: Put-Call Ratio
      - max_pain: strike with min total pain
      - top_ce: list of top 5 CE OI strikes (resistance)
      - top_pe: list of top 5 PE OI strikes (support)
    Cached for LIVE_CACHE_TTL seconds.
    """
    cache_key = "option_chain"
    now_ts = datetime.datetime.utcnow().timestamp()

    if cache_key in _live_cache:
        entry = _live_cache[cache_key]
        if now_ts - entry["ts"] < LIVE_CACHE_TTL:
            return entry["data"]

    try:
        s, headers = _nse_session_headers()
        resp = s.get(NSE_OPTION_CHAIN_URL, headers=headers, timeout=10)
        if not resp.ok:
            return None

        raw = resp.json()
        records = raw.get("records", {}).get("data", [])
        if not records:
            return None

        # Spot price from API
        spot = raw.get("records", {}).get("underlyingValue", 0)

        # Only consider strikes within ±10% of spot
        lower = spot * 0.90
        upper = spot * 1.10

        ce_oi = {}
        pe_oi = {}
        total_ce_oi = 0
        total_pe_oi = 0

        for rec in records:
            strike = rec.get("strikePrice", 0)
            if not (lower <= strike <= upper):
                continue
            if "CE" in rec:
                oi = rec["CE"].get("openInterest", 0)
                ce_oi[strike] = oi
                total_ce_oi += oi
            if "PE" in rec:
                oi = rec["PE"].get("openInterest", 0)
                pe_oi[strike] = oi
                total_pe_oi += oi

        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 0

        # Top 5 CE strikes by OI (resistance)
        top_ce = sorted(ce_oi.items(), key=lambda x: x[1], reverse=True)[:5]
        # Top 5 PE strikes by OI (support)
        top_pe = sorted(pe_oi.items(), key=lambda x: x[1], reverse=True)[:5]

        # Max Pain: strike where total pain of all option writers is minimum
        all_strikes = sorted(set(list(ce_oi.keys()) + list(pe_oi.keys())))
        max_pain_strike = None
        min_pain = float("inf")
        for s_price in all_strikes:
            pain = 0
            for k, oi in ce_oi.items():
                pain += max(0, s_price - k) * oi
            for k, oi in pe_oi.items():
                pain += max(0, k - s_price) * oi
            if pain < min_pain:
                min_pain = pain
                max_pain_strike = s_price

        result = {
            "pcr": pcr,
            "pcr_signal": "Oversold/Bullish"
            if pcr >= 1.2
            else ("Overbought/Bearish" if pcr <= 0.8 else "Neutral"),
            "pcr_color": "green" if pcr >= 1.2 else ("red" if pcr <= 0.8 else "gray"),
            "max_pain": max_pain_strike,
            "spot": spot,
            "top_ce": [{"strike": s, "oi": f"{o:,.0f}"} for s, o in top_ce],
            "top_pe": [{"strike": s, "oi": f"{o:,.0f}"} for s, o in top_pe],
            "total_ce_oi": total_ce_oi,
            "total_pe_oi": total_pe_oi,
        }
        _live_cache[cache_key] = {"ts": now_ts, "data": result}
        return result

    except Exception as e:
        logging.warning(f"Option chain fetch failed: {e}")
    return None


# ====================================================
# Data Processing Functions
# ====================================================


def load_data(date: datetime.date) -> pd.DataFrame:
    csv_content = download_and_cache_csv(date)
    if not csv_content:
        return None

    from io import StringIO

    base_cols = [
        "Client Type",
        "Future Index Long",
        "Future Index Short",
        "Option Index Call Long",
        "Option Index Put Long",
        "Option Index Call Short",
        "Option Index Put Short",
    ]
    extra_cols = [
        "Future Stock Long",
        "Future Stock Short",
        "Option Stock Call Long",
        "Option Stock Put Long",
        "Option Stock Call Short",
        "Option Stock Put Short",
        "Total Long Contracts",
        "Total Short Contracts",
    ]

    try:
        # Read all columns first so we can strip whitespace from headers
        df = pd.read_csv(StringIO(csv_content), skiprows=1)
        df.columns = df.columns.str.strip()  # ← fix trailing-space column names
        df["Client Type"] = df["Client Type"].str.strip()

        # Keep only the columns we need; fill any missing extra cols with 0
        keep = base_cols + extra_cols
        for col in extra_cols:
            if col not in df.columns:
                df[col] = 0

        df = df[[c for c in keep if c in df.columns]]

        # Coerce all numeric columns (handles any stray commas / whitespace)
        for col in keep[1:]:
            if col in df.columns:
                df[col] = (
                    pd.to_numeric(
                        df[col].astype(str).str.replace(",", "").str.strip(),
                        errors="coerce",
                    )
                    .fillna(0)
                    .astype(int)
                )

        df = df[df["Client Type"] != "TOTAL"].reset_index(drop=True)
        df = df.dropna(subset=base_cols[1:])  # drop rows where core cols are NaN
        return df

    except Exception as e:
        logging.error(f"Error reading CSV data: {e}")
        return None


def fetch_last_n_days_data(end_date: datetime.date, n: int = 5) -> dict:
    data_map = {}
    current_date = end_date
    count = 0
    while count < n:
        current_date = adjust_for_holidays(current_date)
        df = load_data(current_date)
        if df is not None:
            data_map[current_date] = df
            count += 1
        current_date -= datetime.timedelta(days=1)
    return data_map


def calculate_net_sentiment(df):
    df["Call Diff"] = df["Option Index Call Long"] - df["Option Index Call Short"]
    df["Put Diff"] = df["Option Index Put Long"] - df["Option Index Put Short"]
    # Net Sentiment = Call Diff + Put Diff
    # Positive = net long bias (Bullish), Negative = net short bias (Bearish)
    df["Net Sentiment"] = df["Call Diff"] + df["Put Diff"]
    return df


# ====================================================
# Activity Table Logic
# ====================================================


def get_latest_activity_data(target_date: datetime.date = None):
    """
    Returns activity data dict. target_date defaults to latest available.
    Also includes participant summary cards and divergence alert.
    """
    curr_date = target_date or datetime.date.today()
    attempts = 0
    curr_df = None

    while attempts < 10:
        curr_date = adjust_for_holidays(curr_date)
        curr_df = load_data(curr_date)
        if curr_df is not None and not curr_df.empty:
            break
        curr_date -= datetime.timedelta(days=1)
        attempts += 1

    if curr_df is None:
        return None

    prev_date = curr_date - datetime.timedelta(days=1)
    prev_df = None
    attempts = 0
    while attempts < 10:
        prev_date = adjust_for_holidays(prev_date)
        prev_df = load_data(prev_date)
        if prev_df is not None and not prev_df.empty:
            break
        prev_date -= datetime.timedelta(days=1)
        attempts += 1

    if prev_df is None:
        return None

    def get_net(df, client, type_long, type_short):
        row = df[df["Client Type"] == client]
        if row.empty:
            return 0
        return int(row[type_long].values[0] - row[type_short].values[0])

    participants = ["FII", "Pro", "DII", "Client"]
    display_names = {"FII": "FII", "Pro": "PRO", "DII": "DII", "Client": "RETAIL"}

    results = []
    overall_score = 0
    summary_cards = []  # net futures position per participant

    for p in participants:
        p_data = {"name": display_names[p], "rows": []}

        instruments = [
            ("Future", "Future Index Long", "Future Index Short"),
            ("CE", "Option Index Call Long", "Option Index Call Short"),
            ("PE", "Option Index Put Long", "Option Index Put Short"),
        ]

        net_future_change = None

        for instr_name, col_long, col_short in instruments:
            net_curr = get_net(curr_df, p, col_long, col_short)
            net_prev = get_net(prev_df, p, col_long, col_short)
            change = net_curr - net_prev

            activity = trend = trend_color = ""

            if instr_name == "Future":
                net_future_change = change
                if change > 0:
                    activity, trend, trend_color = "Bought Futures", "Bullish", "green"
                    if p == "FII":
                        overall_score += 2
                elif change < 0:
                    activity, trend, trend_color = "Sold Futures", "Bearish", "red"
                    if p == "FII":
                        overall_score -= 2
                else:
                    activity, trend, trend_color = "No Change", "Neutral", "gray"
            elif instr_name == "CE":
                if change > 0:
                    activity, trend, trend_color = "Bought Calls", "Bullish", "green"
                    if p == "FII":
                        overall_score += 1
                elif change < 0:
                    activity, trend, trend_color = "Sold Calls", "Bearish", "red"
                    if p == "FII":
                        overall_score -= 1
                else:
                    activity, trend, trend_color = "No Change", "Neutral", "gray"
            elif instr_name == "PE":
                if change > 0:
                    activity, trend, trend_color = "Bought Puts", "Bearish", "red"
                    if p == "FII":
                        overall_score -= 1
                elif change < 0:
                    activity, trend, trend_color = "Sold Puts", "Bullish", "green"
                    if p == "FII":
                        overall_score += 1
                else:
                    activity, trend, trend_color = "No Change", "Neutral", "gray"

            p_data["rows"].append(
                {
                    "instrument": instr_name,
                    "change": f"{change:,.0f}",
                    "change_raw": change,
                    "activity": activity,
                    "trend": trend,
                    "color": trend_color,
                }
            )

        results.append(p_data)

        # Summary card
        summary_cards.append(
            {
                "name": display_names[p],
                "net_futures": f"{net_future_change:+,.0f}"
                if net_future_change is not None
                else "—",
                "color": "green" if (net_future_change or 0) > 0 else "red",
            }
        )

    # FII/DII divergence detection
    fii_future = next((c for c in summary_cards if c["name"] == "FII"), None)
    dii_future = next((c for c in summary_cards if c["name"] == "DII"), None)
    divergence = None
    if fii_future and dii_future and fii_future["color"] != dii_future["color"]:
        divergence = {
            "fii_color": fii_future["color"],
            "dii_color": dii_future["color"],
            "fii_net": fii_future["net_futures"],
            "dii_net": dii_future["net_futures"],
        }

    overall_trend = "NEUTRAL"
    overall_color = "gray"
    if overall_score >= 2:
        overall_trend, overall_color = "BULLISH", "green"
    elif overall_score <= -2:
        overall_trend, overall_color = "BEARISH", "red"

    # Build 5-day trend table
    trend_table = build_5day_trend(curr_date)

    return {
        "date": curr_date.strftime("%d/%m/%Y"),
        "data_date": curr_date.strftime("%Y-%m-%d"),  # for date picker
        "data": results,
        "overall_trend": overall_trend,
        "overall_color": overall_color,
        "summary_cards": summary_cards,
        "divergence": divergence,
        "trend_table": trend_table,
    }


# ====================================================
# 5-Day Trend Table
# ====================================================


def build_5day_trend(end_date: datetime.date) -> list:
    """
    Returns list of dicts: date, FII, PRO, DII, RETAIL net sentiment per day.
    """
    data_map = fetch_last_n_days_data(end_date, n=5)
    sorted_dates = sorted(data_map.keys())
    rows = []
    for d in sorted_dates:
        df = calculate_net_sentiment(data_map[d].copy())
        row = {"date": d.strftime("%d %b")}
        for client, label in [
            ("FII", "FII"),
            ("Pro", "PRO"),
            ("DII", "DII"),
            ("Client", "RETAIL"),
        ]:
            r = df[df["Client Type"] == client]
            val = int(r["Net Sentiment"].values[0]) if not r.empty else 0
            row[label] = {
                "value": f"{val:+,.0f}",
                "color": "green" if val >= 0 else "red",
            }
        rows.append(row)
    return rows


# ====================================================
# Chart Generation
# ====================================================


def generate_advanced_charts(current_date: datetime.date):
    data_map = fetch_last_n_days_data(current_date, n=5)
    sorted_dates = sorted(data_map.keys())

    if len(sorted_dates) < 1:
        raise Exception("Not enough data available.")

    today_df = data_map[sorted_dates[-1]].copy()
    today_df = calculate_net_sentiment(today_df)
    prev_df = data_map[sorted_dates[-2]].copy() if len(sorted_dates) >= 2 else today_df

    layout_args = dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#888",
        margin=dict(l=20, r=20, t=40, b=20),
    )

    charts = {}

    def get_colors(values):
        return ["#86efac" if v >= 0 else "#fca5a5" for v in values]

    # ── CHART 1: Long/Short Ratio per Participant ─────────────────────────────
    # Uses Total Long Contracts / Total Short Contracts
    ls_df = today_df.copy()
    ls_df["LS_Ratio"] = ls_df["Total Long Contracts"] / ls_df[
        "Total Short Contracts"
    ].replace(0, 1)
    bar_colors = ["#059669" if v >= 1 else "#dc2626" for v in ls_df["LS_Ratio"]]

    fig_ls = go.Figure()
    fig_ls.add_trace(
        go.Bar(
            x=ls_df["Client Type"],
            y=ls_df["LS_Ratio"].round(3),
            marker_color=bar_colors,
            text=ls_df["LS_Ratio"].round(3),
            textposition="auto",  # 'outside' clips small bars near 0
            name="L/S Ratio",
        )
    )
    fig_ls.add_hline(
        y=1.0,
        line_dash="dash",
        line_color="#6366f1",
        annotation_text="Neutral (1.0)",
        annotation_position="top right",
    )
    fig_ls.update_layout(
        title="Long/Short Ratio by Participant (Full Book)",
        yaxis_title="Ratio",
        showlegend=False,
        **layout_args,
        height=320,
    )
    charts["ls_ratio"] = fig_ls.to_html(include_plotlyjs="cdn", full_html=False)

    # ── CHART 2: FII 5-Day Stacked Area ──────────────────────────────────────
    dates_str = [d.strftime("%d %b") for d in sorted_dates]
    fii_fut, fii_ce, fii_pe = [], [], []
    for d in sorted_dates:
        df_d = calculate_net_sentiment(data_map[d].copy())
        fii_row = df_d[df_d["Client Type"] == "FII"]
        if not fii_row.empty:
            fii_fut.append(
                int(
                    fii_row["Future Index Long"].values[0]
                    - fii_row["Future Index Short"].values[0]
                )
            )
            fii_ce.append(
                int(fii_row["Call Diff"].values[0])
                if "Call Diff" in fii_row.columns
                else 0
            )
            fii_pe.append(
                int(fii_row["Put Diff"].values[0])
                if "Put Diff" in fii_row.columns
                else 0
            )
        else:
            fii_fut.append(0)
            fii_ce.append(0)
            fii_pe.append(0)

    fig_fii = go.Figure()
    fig_fii.add_trace(
        go.Scatter(
            x=dates_str,
            y=fii_fut,
            name="Net Futures",
            fill="tozeroy",
            mode="lines+markers",
            line=dict(color="#3b82f6", width=2),
            fillcolor="rgba(59,130,246,0.15)",
        )
    )
    fig_fii.add_trace(
        go.Scatter(
            x=dates_str,
            y=fii_ce,
            name="Net Calls (CE)",
            fill="tozeroy",
            mode="lines+markers",
            line=dict(color="#10b981", width=2),
            fillcolor="rgba(16,185,129,0.15)",
        )
    )
    fig_fii.add_trace(
        go.Scatter(
            x=dates_str,
            y=fii_pe,
            name="Net Puts (PE)",
            fill="tozeroy",
            mode="lines+markers",
            line=dict(color="#ef4444", width=2),
            fillcolor="rgba(239,68,68,0.15)",
        )
    )
    fig_fii.add_hline(y=0, line_dash="dot", line_color="#94a3b8")
    fig_fii.update_layout(
        title="FII 5-Day Net Position (Futures + CE + PE)",
        yaxis_title="Net Contracts",
        **layout_args,
        height=360,
        legend=dict(orientation="h", y=-0.15),
    )
    charts["fii_5day_area"] = fig_fii.to_html(include_plotlyjs="cdn", full_html=False)

    # ── CHART 3: OI Distribution Donuts (Futures / Calls / Puts) ─────────────
    fig_donuts = make_subplots(
        rows=1,
        cols=3,
        specs=[[{"type": "domain"}, {"type": "domain"}, {"type": "domain"}]],
        subplot_titles=("Index Futures Long", "Index Call Long", "Index Put Long"),
    )
    donut_colors = ["#3b82f6", "#8b5cf6", "#10b981", "#f59e0b"]

    for col_idx, (col, title) in enumerate(
        [
            ("Future Index Long", "Index Futures"),
            ("Option Index Call Long", "Index Calls"),
            ("Option Index Put Long", "Index Puts"),
        ],
        start=1,
    ):
        fig_donuts.add_trace(
            go.Pie(
                labels=today_df["Client Type"].tolist(),
                values=today_df[col].tolist(),
                hole=0.5,
                name=title,
                marker=dict(colors=donut_colors),
                textinfo="percent+label",
                textfont_size=10,
            ),
            row=1,
            col=col_idx,
        )

    fig_donuts.update_layout(
        title="OI Distribution — Who Controls What?",
        showlegend=False,
        **layout_args,
        height=340,
    )
    charts["oi_donuts"] = fig_donuts.to_html(include_plotlyjs="cdn", full_html=False)

    # ── CHART 4: Stock vs Index Futures (FII) over 5 days ────────────────────
    fii_idx_net, fii_stk_net = [], []
    for d in sorted_dates:
        df_d = data_map[d]
        fii_r = df_d[df_d["Client Type"] == "FII"]
        if not fii_r.empty:
            idx_net = int(fii_r["Future Index Long"].values[0]) - int(
                fii_r["Future Index Short"].values[0]
            )
            stk_long = (
                int(fii_r["Future Stock Long"].values[0])
                if "Future Stock Long" in fii_r.columns
                else 0
            )
            stk_short = (
                int(fii_r["Future Stock Short"].values[0])
                if "Future Stock Short" in fii_r.columns
                else 0
            )
            stk_net = stk_long - stk_short
        else:
            idx_net = stk_net = 0
        fii_idx_net.append(idx_net)
        fii_stk_net.append(stk_net)

    idx_colors = ["#059669" if v >= 0 else "#dc2626" for v in fii_idx_net]
    stk_colors = ["#3b82f6" if v >= 0 else "#f97316" for v in fii_stk_net]

    fig_stk = make_subplots(
        rows=1,
        cols=2,
        subplot_titles=("FII Net Index Futures", "FII Net Stock Futures"),
    )
    fig_stk.add_trace(
        go.Bar(
            x=dates_str, y=fii_idx_net, marker_color=idx_colors, name="Index Futures"
        ),
        row=1,
        col=1,
    )
    fig_stk.add_trace(
        go.Bar(
            x=dates_str, y=fii_stk_net, marker_color=stk_colors, name="Stock Futures"
        ),
        row=1,
        col=2,
    )
    for col_idx in [1, 2]:
        fig_stk.add_hline(
            y=0, line_dash="dot", line_color="#94a3b8", row=1, col=col_idx
        )
    fig_stk.update_layout(
        title="FII: Stock vs Index Futures (5-Day Rotation View)",
        showlegend=False,
        **layout_args,
        height=320,
    )
    charts["fii_stock_vs_index"] = fig_stk.to_html(
        include_plotlyjs="cdn", full_html=False
    )

    # ── CHART 5: Participant Aggression Heatmap (5-day rolling ΔSentiment) ───
    # Skip the first date (delta is always 0 — no prior day), show d2..d5
    aggr_data = {label: [] for label in ["FII", "PRO", "DII", "RETAIL"]}
    participant_map = {"FII": "FII", "Pro": "PRO", "DII": "DII", "Client": "RETAIL"}

    prev_sentiment = {}
    aggr_dates = []  # dates that have a real delta (d2 onwards)

    for i, d in enumerate(sorted_dates):
        df_d = calculate_net_sentiment(data_map[d].copy())
        curr_sentiment = {}
        for p_key, p_label in participant_map.items():
            row = df_d[df_d["Client Type"] == p_key]
            curr_sentiment[p_label] = (
                int(row["Net Sentiment"].values[0]) if not row.empty else 0
            )

        if i > 0:  # skip day 0 — no delta possible
            aggr_dates.append(d.strftime("%d %b"))
            for p_label in ["FII", "PRO", "DII", "RETAIL"]:
                aggr_data[p_label].append(
                    curr_sentiment[p_label] - prev_sentiment[p_label]
                )

        prev_sentiment = curr_sentiment

    participants_order = ["FII", "PRO", "DII", "RETAIL"]
    z_matrix = [aggr_data[p] for p in participants_order]
    text_matrix = [[f"{v:+,.0f}" for v in row] for row in z_matrix]

    fig_aggr = go.Figure(
        data=go.Heatmap(
            z=z_matrix,
            x=aggr_dates,  # 4 real dates (d2–d5), no wasted zero column
            y=participants_order,
            colorscale=[
                [0.0, "#dc2626"],
                [0.5, "#f8fafc"],
                [1.0, "#059669"],
            ],
            zmid=0,
            text=text_matrix,
            texttemplate="%{text}",
            textfont={"size": 11},
            colorbar=dict(title="Δ Sentiment"),
        )
    )
    fig_aggr.update_layout(
        title="Participant Aggression Heatmap (Day-over-Day Sentiment Change)",
        **layout_args,
        height=280,
    )
    charts["aggr_heatmap"] = fig_aggr.to_html(include_plotlyjs="cdn", full_html=False)

    # ── EXISTING: Call & Put Diff table ──────────────────────────────────────
    step8_df = today_df.copy()

    fig_tbl1 = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=[
                        "Client Type",
                        "Call Diff (Long−Short)",
                        "Put Diff (Long−Short)",
                    ],
                    fill_color="#d1d5db",
                    align="center",
                    font=dict(color="black", size=11),
                ),
                cells=dict(
                    values=[
                        step8_df["Client Type"],
                        step8_df["Call Diff"],
                        step8_df["Put Diff"],
                    ],
                    fill_color=[
                        ["#f3f4f6"] * len(step8_df),
                        get_colors(step8_df["Call Diff"]),
                        get_colors(step8_df["Put Diff"]),
                    ],
                    align="center",
                    font=dict(color="black", size=10),
                    height=28,
                ),
            )
        ]
    )
    fig_tbl1.update_layout(
        title="Call & Put Diff per Client", margin=dict(l=0, r=0, t=25, b=0), height=250
    )
    charts["step8_table1"] = fig_tbl1.to_html(include_plotlyjs="cdn", full_html=False)

    fig_tbl2 = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=["Client Type", "Net Sentiment (Call+Put Diff)"],
                    fill_color="#d1d5db",
                    align="center",
                    font=dict(color="black", size=11),
                ),
                cells=dict(
                    values=[step8_df["Client Type"], step8_df["Net Sentiment"]],
                    fill_color=[
                        ["#f3f4f6"] * len(step8_df),
                        get_colors(step8_df["Net Sentiment"]),
                    ],
                    align="center",
                    font=dict(color="black", size=10),
                    height=28,
                ),
            )
        ]
    )
    fig_tbl2.update_layout(
        title="Net Sentiment (Call+Put Diff)",
        margin=dict(l=0, r=0, t=25, b=0),
        height=250,
    )
    charts["step8_table2"] = fig_tbl2.to_html(include_plotlyjs="cdn", full_html=False)

    # ── EXISTING: Side-by-side Call/Put bars ─────────────────────────────────
    fig_sb = make_subplots(rows=1, cols=2, subplot_titles=("Call Diff", "Put Diff"))
    fig_sb.add_trace(
        go.Bar(
            x=step8_df["Client Type"],
            y=step8_df["Call Diff"],
            marker_color=[
                "#059669" if v >= 0 else "#dc2626" for v in step8_df["Call Diff"]
            ],
            name="Call Diff",
        ),
        row=1,
        col=1,
    )
    fig_sb.add_trace(
        go.Bar(
            x=step8_df["Client Type"],
            y=step8_df["Put Diff"],
            marker_color=[
                "#059669" if v >= 0 else "#dc2626" for v in step8_df["Put Diff"]
            ],
            name="Put Diff",
        ),
        row=1,
        col=2,
    )
    fig_sb.update_layout(
        title="Call & Put Diff per Client Type", showlegend=False, **layout_args
    )
    charts["step8_charts"] = fig_sb.to_html(include_plotlyjs="cdn", full_html=False)

    # ── EXISTING: Position Intensity Heatmap ─────────────────────────────────
    heatmap_data = today_df.set_index("Client Type")[
        [
            "Future Index Long",
            "Future Index Short",
            "Option Index Call Long",
            "Option Index Put Long",
        ]
    ]
    fig_heat = go.Figure(
        data=go.Heatmap(
            z=heatmap_data.values,
            x=heatmap_data.columns,
            y=heatmap_data.index,
            colorscale="Viridis",
            text=heatmap_data.values,
            texttemplate="%{text:,.0f}",
            textfont={"size": 12},
            colorbar=dict(title="OI Value"),
        )
    )
    fig_heat.update_layout(
        title=dict(
            text="Position Intensity Heatmap", font=dict(size=18, color="#3b82f6")
        ),
        **layout_args,
        height=500,
    )
    charts["position_heat"] = fig_heat.to_html(include_plotlyjs="cdn", full_html=False)

    return charts


# ====================================================
# Flask Routes
# ====================================================


@app.route("/", methods=["GET"])
def index():
    try:
        cleanup_old_cache_files(days_to_keep=7)
    except Exception:
        pass

    # Date from query param (for date picker)
    date_param = request.args.get("date")
    target_date = None
    if date_param:
        try:
            target_date = datetime.date.fromisoformat(date_param)
        except ValueError:
            flash("Invalid date format.", "warning")

    charts = None
    activity_data = None
    nifty_spot = None
    option_chain = None

    try:
        base_date = target_date or adjust_for_holidays(datetime.date.today())
        charts = generate_advanced_charts(base_date)
        activity_data = get_latest_activity_data(target_date)
    except Exception as e:
        logging.error(f"Index load error: {e}")
        flash(f"Error loading market data: {str(e)}", "error")

    # Live data — non-blocking, fail gracefully
    try:
        nifty_spot = get_nifty_spot()
    except Exception:
        pass

    try:
        option_chain = get_option_chain_summary()
    except Exception:
        pass

    return render_template(
        "index.html",
        charts=charts,
        activity_data=activity_data,
        nifty_spot=nifty_spot,
        option_chain=option_chain,
        selected_date=date_param or "",
        current_year=datetime.date.today().year,
        current_month=datetime.date.today().strftime("%B"),
    )


# ====================================================
# JSON API Endpoints
# ====================================================


@app.route("/api/activity")
def api_activity():
    """JSON endpoint: latest FII/DII activity data."""
    date_param = request.args.get("date")
    target_date = None
    if date_param:
        try:
            target_date = datetime.date.fromisoformat(date_param)
        except ValueError:
            return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400

    data = get_latest_activity_data(target_date)
    if data is None:
        return jsonify({"error": "No data available."}), 503
    return jsonify(data)


@app.route("/api/sentiment")
def api_sentiment():
    """JSON endpoint: latest net sentiment summary."""
    today = adjust_for_holidays(datetime.date.today())
    data_map = fetch_last_n_days_data(today, n=1)
    if not data_map:
        return jsonify({"error": "No data available."}), 503
    df = calculate_net_sentiment(list(data_map.values())[0].copy())
    result = {}
    for _, row in df.iterrows():
        result[row["Client Type"]] = {
            "call_diff": int(row["Call Diff"]),
            "put_diff": int(row["Put Diff"]),
            "net_sentiment": int(row["Net Sentiment"]),
        }
    return jsonify({"date": list(data_map.keys())[0].isoformat(), "data": result})


@app.route("/api/nifty")
def api_nifty():
    """JSON endpoint: Nifty spot price."""
    data = get_nifty_spot()
    if data is None:
        return jsonify({"error": "Unable to fetch Nifty spot price."}), 503
    return jsonify(data)


@app.route("/api/option-chain")
def api_option_chain():
    """JSON endpoint: option chain summary (PCR, max pain, support/resistance)."""
    data = get_option_chain_summary()
    if data is None:
        return jsonify({"error": "Unable to fetch option chain data."}), 503
    return jsonify(data)


@app.route("/cache-status")
def cache_status():
    if not app.debug and not is_vercel():
        return "Cache status only available in debug mode", 403
    try:
        from storage import get_cache_status as _gcs

        status = _gcs()
    except Exception:
        status = {"cache_dir": CACHE_DIR, "memory_keys": list(_csv_cache.keys())}
    return f"<pre>{json.dumps(status, indent=2, default=str)}</pre>"


# ====================================================
# Run App
# ====================================================
if __name__ == "__main__":
    app.run(debug=True, port=5001)
