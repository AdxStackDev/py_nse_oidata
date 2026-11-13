<div align="center">

# 📈 NSE F&O Analytics Dashboard

[![Python](https://img.shields.io/badge/Python-3.12+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-2.0+-000000?style=for-the-badge&logo=flask&logoColor=white)](https://flask.palletsprojects.com/)
[![TailwindCSS](https://img.shields.io/badge/Tailwind_CSS-3.0+-38B2AC?style=for-the-badge&logo=tailwind-css&logoColor=white)](https://tailwindcss.com/)
[![Vercel](https://img.shields.io/badge/Vercel-Deployed-000000?style=for-the-badge&logo=vercel&logoColor=white)](https://adxnse.vercel.app/)
[![License](https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge)](LICENSE)

**Real-time Futures & Options Analysis · Institutional Sentiment Tracking · Live Option Chain**

[Live Demo 🚀](https://adxnse.vercel.app/) · [Report Bug 🐛](https://github.com/AdxStackDev/py_pd_csv1/issues)

<img src="assets/dashboard.png" width="800" alt="Dashboard" style="border-radius:10px; margin-top:16px; box-shadow:0 10px 15px -3px rgba(0,0,0,0.1);">

</div>

---

## 📊 Overview

**NSE F&O Analytics Dashboard** is a web application for traders and analysts that provides institutional-grade insights into the NSE derivatives market. It fetches daily participant-wise OI data published by NSE, processes it in Python, and renders interactive charts and tables through a clean Flask + Tailwind UI.

Data is sourced from NSE's official CSV files (published after 7 PM IST each trading day). The app caches data locally and handles Vercel's serverless read-only filesystem via `/tmp`.

---

## ✨ Features

### Institutional Activity
- **FII DII F&O Activity Table** — Day-over-day change in net positions (Futures, CE, PE) for FII, PRO, DII, and RETAIL with Bullish / Bearish / Neutral signal badges
- **Net Futures Change Card** — At-a-glance net futures position change for all 4 participants, displayed above the main table
- **FII / DII Divergence Alert** — Automatically surfaces when FII and DII are on opposite sides of the futures market

### Today's OI Analysis
- **Long / Short Ratio** — Full-book L/S ratio per participant using Total Long / Total Short contracts
- **OI Distribution Donuts** — Share of Index Futures, Calls, and Puts OI controlled by each participant
- **Call & Put Difference Tables** — Net (Long − Short) per participant for calls and puts separately
- **Net Sentiment Table** — Call Diff + Put Diff combined per participant
- **Participant Activity Comparison** — Side-by-side bar chart of Call Diff vs Put Diff

### Multi-Day Trend Analysis
- **FII 5-Day Net Position Area Chart** — Absolute net Futures, CE, PE positions across last 5 sessions — shows whether FII is hedging or going directional
- **FII Stock vs Index Futures** — 5-day rotation view showing whether FII is rotating from index to individual stocks
- **5-Day Net Sentiment Table** — Net sentiment per participant for all 5 recent sessions
- **Participant Aggression Heatmap** — Day-over-day change in net sentiment (green = improving bullish momentum, red = deteriorating)

### Absolute OI View
- **Position Intensity Heatmap** — Raw OI values across Future Index Long/Short, Call Long, Put Long for all participants

### Live Data (Option Chain)
- **Put-Call Ratio (PCR)** — With Bullish / Neutral / Bearish signal (≥1.2 bullish, ≤0.8 bearish)
- **Max Pain Strike** — Computed as the strike that minimises total loss for option writers
- **Top CE / PE OI Strikes** — Top 5 resistance (CE) and support (PE) levels by open interest

### UI & UX
- **Date Picker** — Browse any historical trading date
- **Nifty Spot Price** — Live from NSE indices API (cached 5 min)
- **Market Status Pill** — Live open/closed indicator based on IST time
- **Accordion Sections** — Charts 5–14 collapsible to keep the page clean
- **Auto-Refresh** — Page reloads every 5 minutes during market hours
- **JSON API Endpoints** — `/api/activity`, `/api/sentiment`, `/api/nifty`, `/api/option-chain`
- **SEBI-compliant Disclaimer** — Included on every page

---

## 📸 Screenshots

<div align="center">

### Dashboard
<img src="assets/dashboard.png" width="800" alt="Dashboard" style="border-radius:10px; margin-bottom:16px;">

### Call vs Put Difference
<img src="assets/call_put_difference_graph.png" width="800" alt="Call Put Difference" style="border-radius:10px; margin-bottom:16px;">

### OI Position Heatmap
<img src="assets/oi_position_heat_map.png" width="800" alt="OI Heatmap" style="border-radius:10px; margin-bottom:16px;">

### Client Participation
<img src="assets/client_call_put_difference.png" width="800" alt="Client Participation" style="border-radius:10px; margin-bottom:16px;">

### Net Sentiment
<img src="assets/net_sentiment.png" width="800" alt="Net Sentiment" style="border-radius:10px; margin-bottom:16px;">

### Option Chain
<img src="assets/option_chain.png" width="800" alt="Option Chain" style="border-radius:10px; margin-bottom:16px;">

</div>

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.12, Flask |
| Data Processing | Pandas |
| Charts | Plotly (server-rendered HTML) |
| Frontend | Tailwind CSS (CDN), Jinja2, Vanilla JS |
| Deployment | Vercel (serverless) |
| Data Source | NSE Archives (official CSV), NSE Option Chain API |

---

## 📡 API Endpoints

| Endpoint | Description |
|---|---|
| `GET /` | Main dashboard (optional `?date=YYYY-MM-DD`) |
| `GET /api/activity` | FII/DII activity JSON (optional `?date=`) |
| `GET /api/sentiment` | Net sentiment summary JSON |
| `GET /api/nifty` | Nifty 50 spot price JSON |
| `GET /api/option-chain` | PCR, Max Pain, support/resistance JSON |
| `GET /cache-status` | Cache debug info (debug mode only) |

---

## 🧮 Key Calculations

| Metric | Formula |
|---|---|
| Net Futures Change | `(Future Index Long − Future Index Short)_today − same_yesterday` |
| Call Diff | `Option Index Call Long − Option Index Call Short` |
| Put Diff | `Option Index Put Long − Option Index Put Short` |
| Net Sentiment | `Call Diff + Put Diff` |
| L/S Ratio | `Total Long Contracts ÷ Total Short Contracts` |
| PCR | `Total PE OI ÷ Total CE OI` (within ±10% of spot) |
| Max Pain | Strike minimising `Σ(max(0, S−K)×CE_OI) + Σ(max(0, K−S)×PE_OI)` |
| Aggression Δ | `Net Sentiment_today − Net Sentiment_yesterday` |

---

## 🚀 Getting Started

### Prerequisites
- Python 3.12+
- Git

### Local Setup

```bash
git clone https://github.com/AdxStackDev/py_pd_csv1.git
cd py_pd_csv1
pip install -r requirements.txt
python app.py
```

Open `http://localhost:5001`

> **Note:** NSE publishes the daily OI CSV after 7 PM IST. Before that, the app uses the most recent cached file.

### Vercel Deployment

The app is fully compatible with Vercel's Python serverless runtime. `vercel.json` is already configured. Push to your repo and connect to Vercel — no additional setup required.

```json
{
  "builds": [{ "src": "app.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "app.py" }]
}
```

---

## 📁 Project Structure

```
nse/
├── app.py                  # Flask app — routes, data processing, chart generation
├── storage.py              # Cache utilities
├── requirements.txt
├── vercel.json
├── templates/
│   ├── base.html           # Tailwind config, shared layout
│   ├── index.html          # Main dashboard template (accordion layout)
│   └── disclaimer.html     # SEBI-compliant disclaimer
├── assets/                 # Screenshots for README
└── cache/                  # Local CSV cache (gitignored on Vercel, uses /tmp)
```

---

## ⚠️ Disclaimer

This application is for **educational and informational purposes only**. It does not provide investment advice. Trading in securities markets involves substantial risk of loss. This platform is not registered with SEBI as an Investment Adviser or Research Analyst. Always consult a SEBI-registered financial advisor before making investment decisions.

---

<div align="center">
  <sub>Built by <a href="https://github.com/AdxStackDev">Aditya (AdxStackDev)</a></sub>
</div>
