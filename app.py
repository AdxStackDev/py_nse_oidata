from flask import Flask, render_template, request, flash, redirect, url_for
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import datetime
import requests
import os
import logging
import json
from concurrent.futures import ThreadPoolExecutor

# Try to import pytz, fallback to basic timezone handling
try:
    import pytz
    IST = pytz.timezone('Asia/Kolkata')
    TIMEZONE_AVAILABLE = True
except ImportError:
    logging.warning("pytz not available, using basic timezone handling")
    IST = None
    TIMEZONE_AVAILABLE = False

# ====================================================
# 🌐 Flask App Setup
# ====================================================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "super_secret_key_123")

# Configure logging (StreamHandler only for Vercel compatibility)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] - %(message)s",
    handlers=[logging.StreamHandler()]
)

# ====================================================
# 🧩 Helper Functions
# ====================================================

# Helper function to detect Vercel environment
def is_vercel():
    """Check if running on Vercel."""
    return os.getenv('VERCEL') == '1' or os.getenv('VERCEL_ENV') is not None

# ====================================================
# 📁 Config
# ====================================================
BASE_URL = "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{}.csv"
NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"
NSE_HOME_URL = "https://www.nseindia.com"

# Cache directory for CSV files (Vercel compatible)
CACHE_DIR = "/tmp/nse_cache" if is_vercel() else "cache"
METADATA_FILE = os.path.join(CACHE_DIR, "download_metadata.json")

# In-memory cache for CSV data (fallback)
_csv_cache = {}

# Predefined NSE holidays for 2026
NSE_HOLIDAYS = {
    # January 2026
    datetime.date(2026, 1, 15),  # Municipal Corporation Election - Maharashtra
    datetime.date(2026, 1, 26),  # Republic Day
    
    # February 2026
    datetime.date(2026, 2, 19),  # Chhatrapati Shivaji Maharaj Jayanti
    
    # March 2026
    datetime.date(2026, 3, 3),   # Holi
    datetime.date(2026, 3, 19),  # Gudhi Padwa
    datetime.date(2026, 3, 26),  # Ram Navami
    datetime.date(2026, 3, 31),  # Mahavir Jayanti
    
    # April 2026
    datetime.date(2026, 4, 1),   # Annual Bank Closing
    datetime.date(2026, 4, 3),   # Good Friday
    datetime.date(2026, 4, 14),  # Dr. Babasaheb Ambedkar Jayanti
    
    # May 2026
    datetime.date(2026, 5, 1),   # Maharashtra Din / Buddha Pournima
    datetime.date(2026, 5, 28),  # Bakri ID (Id-Uz-Zuha)
    
    # June 2026
    datetime.date(2026, 6, 26),  # Muharram
    
    # August 2026
    datetime.date(2026, 8, 26),  # Id-E-Milad
    
    # September 2026
    datetime.date(2026, 9, 14),  # Ganesh Chaturthi
    
    # October 2026
    datetime.date(2026, 10, 2),  # Mahatma Gandhi Jayanti
    datetime.date(2026, 10, 20), # Dussehra
    
    # November 2026
    datetime.date(2026, 11, 10), # Diwali (Bali Pratipada)
    datetime.date(2026, 11, 24), # Guru Nanak Jayanti
    
    # December 2026
    datetime.date(2026, 12, 25), # Christmas
}

# Thread pool for parallel downloads
executor = ThreadPoolExecutor(max_workers=5)

# ====================================================
# 🧩 Utility Functions
# ====================================================

def get_date_string(date: datetime.date) -> str:
    return date.strftime("%d%m%Y")

def adjust_for_holidays(date: datetime.date) -> datetime.date:
    """Ensure date is not weekend or NSE holiday."""
    while date.weekday() > 4 or date in NSE_HOLIDAYS:
        date -= datetime.timedelta(days=1)
    return date

def ensure_cache_dir():
    """Ensure cache directory exists."""
    if not os.path.exists(CACHE_DIR):
        os.makedirs(CACHE_DIR, exist_ok=True)

def get_cache_filename(date: datetime.date) -> str:
    """Get cache filename for a specific date."""
    date_str = get_date_string(date)
    return os.path.join(CACHE_DIR, f"nse_fo_{date_str}.csv")

def load_download_metadata() -> dict:
    """Load download metadata from file."""
    try:
        if os.path.exists(METADATA_FILE):
            with open(METADATA_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.warning(f"Failed to load metadata: {e}")
    return {}

def save_download_metadata(metadata: dict):
    """Save download metadata to file."""
    try:
        ensure_cache_dir()
        with open(METADATA_FILE, 'w') as f:
            json.dump(metadata, f)
    except Exception as e:
        logging.warning(f"Failed to save metadata: {e}")

def get_ist_time():
    """Get current time in IST with fallback."""
    if TIMEZONE_AVAILABLE and IST:
        return datetime.datetime.now(IST)
    else:
        # Fallback: assume UTC+5:30 offset
        utc_now = datetime.datetime.utcnow()
        ist_offset = datetime.timedelta(hours=5, minutes=30)
        return utc_now + ist_offset

def is_download_needed(date: datetime.date) -> bool:
    """
    Check if download is needed for the given date.
    Rules:
    - Download only after 7 PM IST
    - Download only once per day
    - Skip if valid file already exists for today
    """
    now_ist = get_ist_time()
    target_date_str = get_date_string(date)
    
    # Check if it's after 7 PM IST
    if now_ist.hour < 19:  # Before 7 PM
        logging.info(f"Before 7 PM IST ({now_ist.hour}:00), skipping download")
        return False
    
    # Check if file already exists
    cache_file = get_cache_filename(date)
    if os.path.exists(cache_file):
        # Check metadata for download timestamp
        metadata = load_download_metadata()
        if target_date_str in metadata:
            try:
                download_time = datetime.datetime.fromisoformat(metadata[target_date_str]['timestamp'])
                
                # If downloaded today after 7 PM, skip
                if (download_time.date() == now_ist.date() and 
                    download_time.hour >= 19):
                    logging.info(f"File already downloaded today after 7 PM for {target_date_str}")
                    return False
            except Exception as e:
                logging.warning(f"Error parsing download timestamp: {e}")
    
    return True

def download_and_cache_csv(date: datetime.date, max_retries: int = 3) -> str:
    """
    Download NSE OI CSV file and cache it with smart caching logic.
    Returns the CSV content or None if failed.
    """
    date_str = get_date_string(date)
    cache_file = get_cache_filename(date)
    
    # Check if download is needed
    if not is_download_needed(date):
        # Try to load from cache file
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r') as f:
                    content = f.read()
                logging.info(f"✅ Loaded cached data for {date_str}")
                return content
            except Exception as e:
                logging.warning(f"Failed to read cache file: {e}")
        
        # Fallback to in-memory cache
        if date_str in _csv_cache:
            logging.info(f"✅ Loaded from memory cache for {date_str}")
            return _csv_cache[date_str]
    
    # Download fresh data
    url = BASE_URL.format(date_str)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://www.nseindia.com"
    }

    logging.info(f"📥 Downloading fresh CSV: {url}")
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=15)
            if response.ok and response.text.strip():
                content = response.text
                
                # Save to cache file
                try:
                    ensure_cache_dir()
                    with open(cache_file, 'w') as f:
                        f.write(content)
                    
                    # Update metadata
                    metadata = load_download_metadata()
                    metadata[date_str] = {
                        'timestamp': get_ist_time().isoformat(),
                        'file_size': len(content),
                        'status': 'success'
                    }
                    save_download_metadata(metadata)
                    
                    logging.info(f"✅ Downloaded and cached data for {date_str}")
                except Exception as e:
                    logging.warning(f"Failed to save cache file: {e}")
                
                # Also store in memory cache as fallback
                _csv_cache[date_str] = content
                return content
            else:
                logging.warning(f"Download failed for {url}: {response.status_code}")
                
        except Exception as e:
            logging.error(f"Download attempt {attempt + 1} failed for {url}: {e}")
            
        if attempt < max_retries - 1:
            import time
            time.sleep(2 ** attempt)  # Exponential backoff
    
    # If all attempts failed, try to load from cache as fallback
    if os.path.exists(cache_file):
        try:
            with open(cache_file, 'r') as f:
                content = f.read()
            logging.info(f"⚠️ Using stale cached data for {date_str}")
            return content
        except Exception as e:
            logging.warning(f"Failed to read fallback cache: {e}")
    
    # Final fallback to memory cache
    if date_str in _csv_cache:
        logging.info(f"⚠️ Using stale memory cache for {date_str}")
        return _csv_cache[date_str]
    
    logging.error(f"❌ All download attempts failed for {date_str}")
    return None

def cleanup_old_cache_files(days_to_keep: int = 7):
    """Clean up cache files older than specified days."""
    try:
        if not os.path.exists(CACHE_DIR):
            return
            
        cutoff_date = datetime.date.today() - datetime.timedelta(days=days_to_keep)
        
        for filename in os.listdir(CACHE_DIR):
            if filename.startswith("nse_fo_") and filename.endswith(".csv"):
                try:
                    # Extract date from filename
                    date_str = filename.replace("nse_fo_", "").replace(".csv", "")
                    file_date = datetime.datetime.strptime(date_str, "%d%m%Y").date()
                    
                    if file_date < cutoff_date:
                        file_path = os.path.join(CACHE_DIR, filename)
                        os.remove(file_path)
                        logging.info(f"🗑️ Cleaned up old cache file: {filename}")
                        
                except Exception as e:
                    logging.warning(f"Failed to process cache file {filename}: {e}")
                    
    except Exception as e:
        logging.warning(f"Cache cleanup failed: {e}")

def get_cache_status() -> dict:
    """Get current cache status for debugging."""
    try:
        metadata = load_download_metadata()
        cache_files = []
        
        if os.path.exists(CACHE_DIR):
            for filename in os.listdir(CACHE_DIR):
                if filename.startswith("nse_fo_") and filename.endswith(".csv"):
                    file_path = os.path.join(CACHE_DIR, filename)
                    stat = os.stat(file_path)
                    cache_files.append({
                        'filename': filename,
                        'size': stat.st_size,
                        'modified': datetime.datetime.fromtimestamp(stat.st_mtime).isoformat()
                    })
        
        return {
            'cache_dir': CACHE_DIR,
            'metadata': metadata,
            'cache_files': cache_files,
            'memory_cache_keys': list(_csv_cache.keys())
        }
    except Exception as e:
        return {'error': str(e)}



# ====================================================
# 📊 Data Processing Functions
# ====================================================
def load_data(date: datetime.date) -> pd.DataFrame:
    """Load OI data for a single date using smart caching."""
    csv_content = download_and_cache_csv(date)
    if not csv_content:
        return None
    
    # Base columns (always present)
    base_cols = [
        "Client Type", "Future Index Long", "Future Index Short",
        "Option Index Call Long", "Option Index Put Long",
        "Option Index Call Short", "Option Index Put Short"
    ]
    
    # Optional stock columns (may not exist in older data)
    stock_cols = ["Future Stock Long", "Future Stock Short"]
    
    try:
        # Read CSV from string content
        from io import StringIO
        # Try to load with all columns including stock columns
        df = pd.read_csv(StringIO(csv_content), skiprows=1, usecols=base_cols + stock_cols)
    except ValueError:
        # If stock columns don't exist, load without them and add as zeros
        from io import StringIO
        df = pd.read_csv(StringIO(csv_content), skiprows=1, usecols=base_cols)
        df["Future Stock Long"] = 0
        df["Future Stock Short"] = 0
    except Exception as e:
        logging.error(f"Error reading CSV data: {e}")
        return None
    
    # Remove TOTAL rows and clean data
    df = df.dropna()
    df = df[df["Client Type"] != "TOTAL"]  # Filter out TOTAL rows
    
    return df

def fetch_last_n_days_data(end_date: datetime.date, n: int = 5) -> dict:
    """Fetch data for the last n trading days."""
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
    """Calculate Net Call and Net Put differences."""
    df["Call Diff"] = df["Option Index Call Long"] - df["Option Index Call Short"]
    df["Put Diff"] = df["Option Index Put Long"] - df["Option Index Put Short"]
    df["Net Sentiment"] = df["Call Diff"] - df["Put Diff"] # Positive = Bullish, Negative = Bearish
    return df

# ====================================================
# 📊 Activity Table Logic
# ====================================================
def get_latest_activity_data():
    """
    Finds the latest available data date and the previous trading day.
    Calculates Day-over-Day change in Net OI.
    Returns a dict with date and participant activity.
    """
    # 1. Find Latest Date with Data
    curr_date = datetime.date.today()
    attempts = 0
    curr_df = None
    
    while attempts < 10: # Look back up to 10 days
        # Skip weekends/holidays first
        curr_date = adjust_for_holidays(curr_date)
        
        # Try to load data
        curr_df = load_data(curr_date)
        if curr_df is not None and not curr_df.empty:
            break # Found valid data!
        
        # If not found, go back one day
        curr_date -= datetime.timedelta(days=1)
        attempts += 1
        
    if curr_df is None:
        return None # No data found recently

    # 2. Find Previous Trading Day
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
        return None # Can't calculate change without previous data

    # 3. Calculate Changes
    # Helper to get Net OI
    def get_net(df, client, type_long, type_short):
        row = df[df["Client Type"] == client]
        if row.empty: return 0
        return int(row[type_long].values[0] - row[type_short].values[0])

    participants = ["FII", "Pro", "DII", "Client"] # Note: CSV uses 'Client' for Retail
    display_names = {"FII": "FII", "Pro": "PRO", "DII": "DII", "Client": "RETAIL"}
    
    results = []
    
    overall_score = 0 # Simple score to determine overall trend

    for p in participants:
        p_data = {"name": display_names[p], "rows": []}
        
        # Instruments to analyze
        instruments = [
            ("Future", "Future Index Long", "Future Index Short"),
            ("CE", "Option Index Call Long", "Option Index Call Short"),
            ("PE", "Option Index Put Long", "Option Index Put Short")
        ]
        
        for instr_name, col_long, col_short in instruments:
            net_curr = get_net(curr_df, p, col_long, col_short)
            net_prev = get_net(prev_df, p, col_long, col_short)
            change = net_curr - net_prev
            
            activity = ""
            trend = ""
            trend_color = "" # green/red
            
            if instr_name == "Future":
                if change > 0: 
                    activity = "Bought Futures"
                    trend = "Bullish"
                    trend_color = "green"
                    if p == "FII": overall_score += 2
                else: 
                    activity = "Sold Futures"
                    trend = "Bearish"
                    trend_color = "red"
                    if p == "FII": overall_score -= 2
            elif instr_name == "CE":
                if change > 0: 
                    activity = "Bought Calls"
                    trend = "Bullish"
                    trend_color = "green"
                    if p == "FII": overall_score += 1
                else: 
                    activity = "Sold Calls"
                    trend = "Bearish"
                    trend_color = "red"
                    if p == "FII": overall_score -= 1
            elif instr_name == "PE":
                if change > 0: 
                    activity = "Bought Puts"
                    trend = "Bearish"
                    trend_color = "red"
                    if p == "FII": overall_score -= 1
                else: 
                    activity = "Sold Puts"
                    trend = "Bullish"
                    trend_color = "green"
                    if p == "FII": overall_score += 1
            
            p_data["rows"].append({
                "instrument": instr_name,
                "change": f"{change:,.0f}",
                "activity": activity,
                "trend": trend,
                "color": trend_color
            })
        results.append(p_data)

    overall_trend = "NEUTRAL"
    overall_color = "gray"
    if overall_score >= 2:
        overall_trend = "BULLISH"
        overall_color = "green"
    elif overall_score <= -2:
        overall_trend = "BEARISH"
        overall_color = "red"

    return {
        "date": curr_date.strftime("%d/%m/%Y"),
        "data": results,
        "overall_trend": overall_trend,
        "overall_color": overall_color
    }

# ====================================================
# 📈 Focused Chart Generation (Compare Page)
# ====================================================


# ====================================================
# 📈 Chart Generation (Advanced)
# ====================================================
def generate_advanced_charts(current_date: datetime.date):
    """Generate all charts for the 7-step analysis. Returns dict of HTML strings."""
    
    # 1. Fetch Data (Current vs Previous for Change, and Last 5 Days for Trend)
    data_map = fetch_last_n_days_data(current_date, n=5)
    sorted_dates = sorted(data_map.keys())
    
    if len(sorted_dates) < 1:
        raise Exception("Not enough data available.")
        
    today_df = data_map[sorted_dates[-1]].copy()
    today_df = calculate_net_sentiment(today_df)
    
    prev_df = data_map[sorted_dates[-2]].copy() if len(sorted_dates) >= 2 else today_df
    
    # Common Layout Settings
    layout_args = dict(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font_color="#888",
        margin=dict(l=20, r=20, t=40, b=20)
    )

    charts = {}

    # --- Step 1: Data Preparation and Overview ---
    # Bar Chart: Raw Values
    fig_raw = go.Figure()
    for col in ["Option Index Call Long", "Option Index Put Long"]:
        fig_raw.add_trace(go.Bar(x=today_df["Client Type"], y=today_df[col], name=col))
    fig_raw.update_layout(title="Raw Call vs Put OI", barmode='group', **layout_args)
    charts['step1_raw'] = fig_raw.to_html(include_plotlyjs='cdn', full_html=False)

    # Donut Chart: Share of Total OI
    total_oi = today_df[["Option Index Call Long", "Option Index Put Long"]].sum(axis=1)
    fig_donut = px.pie(values=total_oi, names=today_df["Client Type"], title="Participant Market Share", hole=0.4)
    fig_donut.update_layout(**layout_args)
    charts['step1_donut'] = fig_donut.to_html(include_plotlyjs='cdn', full_html=False)

    # --- Step 2: Call-Put Difference Analysis ---
    # Horizontal Bar: Net Diff
    fig_hbar = px.bar(today_df, x="Net Sentiment", y="Client Type", orientation='h', 
                      title="Net Sentiment (Call Diff - Put Diff)", color="Net Sentiment",
                      color_continuous_scale=px.colors.diverging.RdBu)
    fig_hbar.update_layout(**layout_args)
    charts['step2_hbar'] = fig_hbar.to_html(include_plotlyjs='cdn', full_html=False)

    # Stacked Bar: Call Diff vs Put Diff
    fig_stack = go.Figure()
    fig_stack.add_trace(go.Bar(x=today_df["Client Type"], y=today_df["Call Diff"], name="Net Call"))
    fig_stack.add_trace(go.Bar(x=today_df["Client Type"], y=today_df["Put Diff"], name="Net Put"))
    fig_stack.update_layout(title="Net Call vs Net Put Exposure", barmode='relative', **layout_args)
    charts['step2_stack'] = fig_stack.to_html(include_plotlyjs='cdn', full_html=False)

    # --- Step 3: Total Market Call-Put Difference ---
    # Column Chart
    total_calls = today_df["Option Index Call Long"].sum()
    total_puts = today_df["Option Index Put Long"].sum()
    fig_mkt = go.Figure(data=[go.Bar(x=["Total Calls", "Total Puts"], y=[total_calls, total_puts], 
                                     marker_color=['#10B981', '#EF4444'])])
    fig_mkt.update_layout(title="Total Market Open Interest", **layout_args)
    charts['step3_col'] = fig_mkt.to_html(include_plotlyjs='cdn', full_html=False)

    # --- Step 4: FII & Pro Specific ---
    # Radar Chart
    categories = ["Future Index Long", "Future Index Short", "Option Index Call Long", "Option Index Put Long"]
    fig_radar = go.Figure()
    for client in ["FII", "PRO"]:
        client_data = today_df[today_df["Client Type"] == client]
        if not client_data.empty:
            values = client_data[categories].values.flatten().tolist()
            fig_radar.add_trace(go.Scatterpolar(r=values, theta=categories, fill='toself', name=client))
    fig_radar.update_layout(title="FII vs PRO Positioning", polar=dict(radialaxis=dict(visible=True)), **layout_args)
    charts['step4_radar'] = fig_radar.to_html(include_plotlyjs='cdn', full_html=False)

    # --- Step 5: Trend Analysis Over Time ---
    # Line Chart
    trend_data = []
    for d in sorted_dates:
        day_df = calculate_net_sentiment(data_map[d].copy())
        for client in ["FII", "PRO", "Client", "DII"]:
            row = day_df[day_df["Client Type"] == client]
            if not row.empty:
                trend_data.append({
                    "Date": d, 
                    "Client": client, 
                    "Net Sentiment": row["Net Sentiment"].values[0]
                })
    
    trend_df = pd.DataFrame(trend_data)
    fig_trend = px.line(trend_df, x="Date", y="Net Sentiment", color="Client", markers=True, title="5-Day Net Sentiment Trend")
    fig_trend.update_layout(**layout_args)
    charts['step5_trend'] = fig_trend.to_html(include_plotlyjs='cdn', full_html=False)

    # --- Step 6: Advanced Combinational Visualization ---
    # Scatter Plot: Net Sentiment vs Change in OI
    # Calculate change from yesterday
    merged = pd.merge(today_df, prev_df, on="Client Type", suffixes=("", "_prev"))
    merged["OI Change"] = (merged["Option Index Call Long"] + merged["Option Index Put Long"]) - \
                          (merged["Option Index Call Long_prev"] + merged["Option Index Put Long_prev"])
    
    fig_scatter = px.scatter(merged, x="Net Sentiment", y="OI Change", color="Client Type", size="Option Index Call Long",
                             title="Sentiment vs OI Change", hover_data=["Client Type"])
    fig_scatter.add_vline(x=0, line_dash="dash", line_color="gray")
    fig_scatter.add_hline(y=0, line_dash="dash", line_color="gray")
    fig_scatter.update_layout(**layout_args)
    charts['step6_scatter'] = fig_scatter.to_html(include_plotlyjs='cdn', full_html=False)

    # Heatmap: Participant vs Position Type Intensity
    # Normalize data for heatmap
    heatmap_data = today_df.set_index("Client Type")[["Future Index Long", "Future Index Short", "Option Index Call Long", "Option Index Put Long"]]
    fig_heat = px.imshow(heatmap_data, text_auto=True, aspect="auto", title="Position Intensity Heatmap",
                         color_continuous_scale="Viridis")
    fig_heat.update_layout(**layout_args)
    charts['step6_heat'] = fig_heat.to_html(include_plotlyjs='cdn', full_html=False)

    # --- Step 8: Requested Custom Tables & Charts ---
    # Use the data as-is (it already contains TOTAL rows from NSE data)
    step8_df = today_df.copy()

    # Helper for Color Formatting (Green/Red)
    def get_colors(values):
        return ['#86efac' if v >= 0 else '#fca5a5' for v in values] # Light Green / Light Red

    # 1. Table: Call Diff vs Put Diff
    fig_tbl1 = go.Figure(data=[go.Table(
        header=dict(values=["Client Type", "Call Diff (Long-Short)", "Put Diff (Long-Short)"],
                    fill_color='#d1d5db', align='center', font=dict(color='black', size=11)),
        cells=dict(values=[step8_df["Client Type"], step8_df["Call Diff"], step8_df["Put Diff"]],
                   fill_color=[['#f3f4f6']*len(step8_df), get_colors(step8_df["Call Diff"]), get_colors(step8_df["Put Diff"])],
                   align='center', font=dict(color='black', size=10), height=28)
    )])
    fig_tbl1.update_layout(title="Call & Put Diff per Client", margin=dict(l=0, r=0, t=25, b=0), height=250)
    charts['step8_table1'] = fig_tbl1.to_html(include_plotlyjs='cdn', full_html=False)

    # 2. Table: Net Sentiment
    fig_tbl2 = go.Figure(data=[go.Table(
        header=dict(values=["Client Type", "Call-Put Diff (Call-Put)"],
                    fill_color='#d1d5db', align='center', font=dict(color='black', size=11)),
        cells=dict(values=[step8_df["Client Type"], step8_df["Net Sentiment"]],
                   fill_color=[['#f3f4f6']*len(step8_df), get_colors(step8_df["Net Sentiment"])],
                   align='center', font=dict(color='black', size=10), height=28)
    )])
    fig_tbl2.update_layout(title="Net Sentiment Diff", margin=dict(l=0, r=0, t=25, b=0), height=250)
    charts['step8_table2'] = fig_tbl2.to_html(include_plotlyjs='cdn', full_html=False)

    # 3. Charts: Side-by-Side Diff Bars
    fig_sb = make_subplots(rows=1, cols=2, subplot_titles=("Call Diff", "Put Diff"))
    
    # Call Diff Bar
    colors_call = ['#059669' if v >= 0 else '#dc2626' for v in step8_df["Call Diff"]]
    fig_sb.add_trace(go.Bar(x=step8_df["Client Type"], y=step8_df["Call Diff"], marker_color=colors_call, name="Call Diff"), row=1, col=1)
    
    # Put Diff Bar
    colors_put = ['#059669' if v >= 0 else '#dc2626' for v in step8_df["Put Diff"]]
    fig_sb.add_trace(go.Bar(x=step8_df["Client Type"], y=step8_df["Put Diff"], marker_color=colors_put, name="Put Diff"), row=1, col=2)

    fig_sb.update_layout(title="Call & Put Diff per Client Type", showlegend=False, **layout_args)
    charts['step8_charts'] = fig_sb.to_html(include_plotlyjs='cdn', full_html=False)

    # ============================================
    # 6. Position Intensity Heatmap (Moved from Compare)
    # ============================================
    heatmap_data = today_df.set_index("Client Type")[["Future Index Long", "Future Index Short", 
                                                        "Option Index Call Long", "Option Index Put Long"]]
    
    fig_heat = go.Figure(data=go.Heatmap(
        z=heatmap_data.values,
        x=heatmap_data.columns,
        y=heatmap_data.index,
        colorscale='Viridis',
        text=heatmap_data.values,
        texttemplate='%{text:,.0f}',
        textfont={"size": 12},
        colorbar=dict(title="OI Value")
    ))
    
    fig_heat.update_layout(
        title=dict(text="Position Intensity Heatmap", font=dict(size=20, color='#3b82f6')),
        **layout_args,
        height=500
    )
    charts['position_heat'] = fig_heat.to_html(include_plotlyjs='cdn', full_html=False)

    return charts

# ====================================================
# 🌍 Flask Routes
# ====================================================
@app.route("/", methods=["GET", "POST"])
def index():
    # Initialize cache system and cleanup old files
    try:
        cleanup_old_cache_files(days_to_keep=7)
    except Exception as e:
        logging.warning(f"Cache cleanup failed: {e}")
    
    # Simplified Index for Dashboard
    charts = None
    activity_data = None
    try:
        # Auto-load latest available data
        today = datetime.date.today()
        target_date = adjust_for_holidays(today)
        
        # Generate Charts
        charts = generate_advanced_charts(target_date)
        
        # Generate Activity Table Data
        activity_data = get_latest_activity_data()
        
    except Exception as e:
        logging.error(f"Index Auto-Load Error: {e}")
        flash(f"Error loading market data: {str(e)}", "error")
    
    return render_template("index.html", charts=charts, activity_data=activity_data)

@app.route("/cache-status")
def cache_status():
    """Debug route to check cache status."""
    if not app.debug and not is_vercel():
        return "Cache status only available in debug mode", 403
    
    status = get_cache_status()
    return f"<pre>{json.dumps(status, indent=2)}</pre>"

# ====================================================
# 🚀 Run App
# ====================================================
if __name__ == "__main__":
    app.run(debug=True, port=5001)
