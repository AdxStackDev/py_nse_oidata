"""
NSE F&O Participant Analytics - Vercel-ready Flask app.

Design rules that keep this deployable on Vercel's Python runtime:
  * Only two third-party packages (flask, requests). No pandas / numpy / plotly /
    pytz, which are what usually blow the 250 MB function-size limit.
  * The filesystem is read-only except /tmp, so downloaded files are cached in
    /tmp. Optional history shipped inside the project (./data/fao_ddmmyyyy.csv)
    is read first, so those days never need the network.
  * No sessions / flash messages, so responses carry no Set-Cookie and can be
    cached at Vercel's edge (Cache-Control: s-maxage).
  * Slow, frequently-blocked NSE *live* endpoints are NOT called while rendering
    the page. The browser fetches /api/live afterwards.
  * Charts are drawn in the browser (Chart.js) from a JSON payload.

How the participant numbers are calculated (all from NSE's participant-wise OI file)
------------------------------------------------------------------------------------
  Net position       = Long - Short                         (per instrument)
  Call Diff          = Index Call Long - Index Call Short
  Put Diff           = Index Put Long  - Index Put Short
  Bullish option OI  = Call Long + Put Short                (profits if market rises)
  Bearish option OI  = Call Short + Put Long                (profits if market falls)
  Options Net        = Bullish - Bearish = Call Diff - Put Diff
  Futures Bias %     = (Fut Long - Fut Short) / (Fut Long + Fut Short) * 100
  Options Bias %     = Options Net / (Bullish + Bearish) * 100
  Blended Bias %     = simple average of Futures Bias % and Options Bias %
  Contribution %     = participant's Long (or Short) OI / market Long (or Short) OI
  Net contribution % = participant's Net / market one-side OI  (sums to 0 across
                       participants, because every long has a matching short)
"""

from __future__ import annotations

import csv
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template, request

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("nse-dashboard")

app = Flask(__name__)

# ------------------------------------------------------------------ config ---
# Set True to bring back the live option chain (PCR, max pain, support/resistance).
# Everything for it is kept in place and simply switched off.
ENABLE_OPTION_CHAIN = False

# India has no daylight saving, so a fixed +05:30 offset is exact (no pytz needed).
IST = timezone(timedelta(hours=5, minutes=30))

IS_VERCEL = bool(os.getenv("VERCEL"))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")  # read-only history bundled with the app

PARTICIPANT_OI_URL = (
    "https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{}.csv"
)
NSE_HOME_URL = "https://www.nseindia.com"
NSE_INDICES_URL = "https://www.nseindia.com/api/allIndices"
NSE_OPTION_CHAIN_URL = "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
}

HTTP_TIMEOUT = 6
MISS_TTL = 120
LIVE_TTL = 300
LIVE_FAIL_TTL = 60
TREND_DAYS = 5
SIGNAL_THRESHOLD = (
    10.0  # blended bias % beyond which a participant is Bullish / Bearish
)
ROUNDING_TOLERANCE = 3  # NSE rounds each cell, so sums can be off by 1-2 contracts

# (client key in the NSE file, label shown in the UI)
PARTICIPANTS = [("FII", "FII"), ("Pro", "PRO"), ("DII", "DII"), ("Client", "RETAIL")]
LABELS = [label for _, label in PARTICIPANTS]

NSE_HOLIDAYS = {
    date(2026, 1, 15),
    date(2026, 1, 26),
    date(2026, 2, 19),
    date(2026, 3, 3),
    date(2026, 3, 19),
    date(2026, 3, 26),
    date(2026, 3, 31),
    date(2026, 4, 1),
    date(2026, 4, 3),
    date(2026, 4, 14),
    date(2026, 5, 1),
    date(2026, 5, 28),
    date(2026, 6, 26),
    date(2026, 8, 26),
    date(2026, 9, 14),
    date(2026, 10, 2),
    date(2026, 10, 20),
    date(2026, 11, 10),
    date(2026, 11, 24),
    date(2026, 12, 25),
}


# ------------------------------------------------------------------- cache ---
def _init_cache_dir() -> str | None:
    candidates = ["/tmp/nse_cache"] if IS_VERCEL else [os.path.join(BASE_DIR, "cache")]
    candidates.append(os.path.join(tempfile.gettempdir(), "nse_cache"))
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return None


CACHE_DIR = _init_cache_dir()
_csv_mem: dict[str, dict] = {}
_misses: dict[str, float] = {}
_live: dict[str, tuple[float, dict]] = {}
_live_fail: dict[str, float] = {}


def now_ist() -> datetime:
    return datetime.now(IST)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in NSE_HOLIDAYS


# ------------------------------------------------- participant OI (CSV) data ---
def _to_int(value: str) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def parse_participant_csv(text: str) -> dict | None:
    """
    Parse NSE's participant-wise OI file into {client: {column: int}}.

    Real files have quirks this handles: a quoted title row (written with doubled
    quotes), header names padded with spaces, and CRLF or even CR-CR-LF endings.
    The TOTAL row is kept under the key "TOTAL" so the data can be cross-checked.
    """
    lines = [ln for ln in text.lstrip("﻿").replace("\r", "\n").split("\n") if ln.strip()]
    rows = list(csv.reader(lines))
    header_idx = next(
        (i for i, r in enumerate(rows) if r and r[0].strip().lower() == "client type"),
        None,
    )
    if header_idx is None:
        return None
    header = [h.strip() for h in rows[header_idx]]
    out: dict[str, dict[str, int]] = {}
    for r in rows[header_idx + 1 :]:
        if not r or not r[0].strip():
            continue
        name = r[0].strip()
        out["TOTAL" if name.upper() == "TOTAL" else name] = {
            h: _to_int(v) for h, v in zip(header[1:], r[1:])
        }
    if not all(key in out for key, _ in PARTICIPANTS):
        return None
    return out


def integrity_ok(rec: dict) -> bool | None:
    """True when participants add up to NSE's TOTAL row and every long has a matching short."""
    tot = rec.get("TOTAL")
    if not tot:
        return None
    for col, value in tot.items():
        if (
            abs(sum(rec[k].get(col, 0) for k, _ in PARTICIPANTS) - value)
            > ROUNDING_TOLERANCE
        ):
            return False
    pairs = [
        ("Future Index Long", "Future Index Short"),
        ("Option Index Call Long", "Option Index Call Short"),
        ("Option Index Put Long", "Option Index Put Short"),
    ]
    return all(
        abs(tot.get(a, 0) - tot.get(b, 0)) <= ROUNDING_TOLERANCE for a, b in pairs
    )


def _read_file(directory: str | None, key: str) -> str | None:
    if not directory:
        return None
    try:
        with open(
            os.path.join(directory, f"fao_{key}.csv"), encoding="utf-8", newline=""
        ) as fh:
            return fh.read()
    except OSError:
        return None


def _write_disk(key: str, text: str) -> None:
    if not CACHE_DIR:
        return
    try:
        with open(
            os.path.join(CACHE_DIR, f"fao_{key}.csv"), "w", encoding="utf-8", newline=""
        ) as fh:
            fh.write(text)
    except OSError:
        pass


def _download(key: str) -> str | None:
    url = PARTICIPANT_OI_URL.format(key)
    for attempt in range(2):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            if resp.status_code == 200 and "client type" in resp.text[:600].lower():
                return resp.text
            return None
        except requests.RequestException as exc:
            log.warning(
                "CSV download attempt %d failed for %s: %s", attempt + 1, key, exc
            )
    return None


def fetch_participant_oi(d: date) -> dict | None:
    """Memory -> bundled ./data -> /tmp cache -> NSE, with a short negative cache."""
    key = d.strftime("%d%m%Y")
    if key in _csv_mem:
        return _csv_mem[key]
    if time.time() - _misses.get(key, 0) < MISS_TTL:
        return None
    text = _read_file(DATA_DIR, key) or _read_file(CACHE_DIR, key)
    if text is None:
        text = _download(key)
        if text:
            _write_disk(key, text)
    parsed = parse_participant_csv(text) if text else None
    if parsed:
        _csv_mem[key] = parsed
    else:
        _misses[key] = time.time()
    return parsed


def load_sessions(anchor: date, n: int = TREND_DAYS) -> list[tuple[date, dict]]:
    """Newest-first list of up to n (date, data) sessions ending at/before `anchor`."""
    anchor = min(anchor, now_ist().date())
    candidates: list[date] = []
    d = anchor
    while len(candidates) < n + 3 and (anchor - d).days < 30:
        if is_trading_day(d):
            candidates.append(d)
        d -= timedelta(days=1)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(fetch_participant_oi, candidates))
    return [(d, r) for d, r in zip(candidates, results) if r][:n]


# -------------------------------------------------------------- analytics ---
def _g(rec: dict, key: str) -> int:
    return rec.get(key, 0)


def net(rec: dict, long_key: str, short_key: str) -> int:
    return _g(rec, long_key) - _g(rec, short_key)


def pct(part: float, whole: float) -> float:
    """Percentage rounded to 1 dp; 0.0 when the denominator is zero."""
    return round(part / whole * 100, 1) if whole else 0.0


def fmt_signed(n: int) -> str:
    return f"{n:+,}"


def fmt_pct(v: float) -> str:
    return f"{v:+.1f}%"


def _tone(n: float) -> str:
    return "green" if n > 0 else "red" if n < 0 else "gray"


def signal_for(score: float) -> tuple[str, str]:
    if score >= SIGNAL_THRESHOLD:
        return "Bullish", "green"
    if score <= -SIGNAL_THRESHOLD:
        return "Bearish", "red"
    return "Neutral", "gray"


def metrics(rec: dict) -> dict:
    """Every per-participant number the dashboard needs, derived from one CSV row."""
    fl, fs = _g(rec, "Future Index Long"), _g(rec, "Future Index Short")
    cl, cs = _g(rec, "Option Index Call Long"), _g(rec, "Option Index Call Short")
    pl, ps = _g(rec, "Option Index Put Long"), _g(rec, "Option Index Put Short")
    tl, ts = _g(rec, "Total Long Contracts"), _g(rec, "Total Short Contracts")

    bull, bear = cl + ps, cs + pl  # direction-aware option exposure
    fut_bias = pct(fl - fs, fl + fs)
    opt_bias = pct(bull - bear, bull + bear)
    blended = round((fut_bias + opt_bias) / 2, 1)
    label, color = signal_for(blended)
    return {
        "fut_long": fl,
        "fut_short": fs,
        "fut_net": fl - fs,
        "fut_long_pct": pct(fl, fl + fs),
        "fut_bias": fut_bias,
        "call_long": cl,
        "call_short": cs,
        "call_net": cl - cs,
        "put_long": pl,
        "put_short": ps,
        "put_net": pl - ps,
        "bull": bull,
        "bear": bear,
        "opt_net": bull - bear,
        "opt_bull_pct": pct(bull, bull + bear),
        "opt_bias": opt_bias,
        "book_long_pct": pct(tl, tl + ts),
        "blended": blended,
        "signal": label,
        "signal_color": color,
    }


def contribution(cur: dict, long_col: str, short_col: str) -> dict:
    """Each participant's share of the market's long OI, short OI and net position."""
    tot_l = sum(_g(cur[k], long_col) for k, _ in PARTICIPANTS)
    tot_s = sum(_g(cur[k], short_col) for k, _ in PARTICIPANTS)
    return {
        "long_pct": [pct(_g(cur[k], long_col), tot_l) for k, _ in PARTICIPANTS],
        "short_pct": [pct(_g(cur[k], short_col), tot_s) for k, _ in PARTICIPANTS],
        "net_pct": [
            pct(net(cur[k], long_col, short_col), tot_l) for k, _ in PARTICIPANTS
        ],
        "total": tot_l,
    }


def build_activity(curr: dict, prev: dict) -> dict:
    """Day-over-day change in net Futures / CE / PE for every participant."""
    instruments = [
        ("Future", "Future Index Long", "Future Index Short"),
        ("CE", "Option Index Call Long", "Option Index Call Short"),
        ("PE", "Option Index Put Long", "Option Index Put Short"),
    ]
    rules = {
        "Future": ("Bought Futures", "Bullish", "Sold Futures", "Bearish", 2),
        "CE": ("Bought Calls", "Bullish", "Sold Calls", "Bearish", 1),
        "PE": ("Bought Puts", "Bearish", "Sold Puts", "Bullish", 1),
    }
    colors = {"Bullish": "green", "Bearish": "red"}

    table, cards, score = [], [], 0
    for key, label in PARTICIPANTS:
        rows, fut_change = [], 0
        for name, lk, sk in instruments:
            change = net(curr[key], lk, sk) - net(prev[key], lk, sk)
            up_act, up_tr, dn_act, dn_tr, weight = rules[name]
            if change > 0:
                activity, trend = up_act, up_tr
            elif change < 0:
                activity, trend = dn_act, dn_tr
            else:
                activity, trend = "No change", "Neutral"
            color = colors.get(trend, "gray")
            if key == "FII" and color != "gray":
                score += weight if color == "green" else -weight
            if name == "Future":
                fut_change = change
            rows.append(
                {
                    "instrument": name,
                    "change": fmt_signed(change),
                    "activity": activity,
                    "trend": trend,
                    "color": color,
                }
            )
        table.append({"name": label, "rows": rows})
        cards.append(
            {
                "name": label,
                "net_futures": fmt_signed(fut_change),
                "color": _tone(fut_change),
            }
        )

    fii = next(c for c in cards if c["name"] == "FII")
    dii = next(c for c in cards if c["name"] == "DII")
    divergence = None
    if {fii["color"], dii["color"]} == {"green", "red"}:
        divergence = {
            "fii_net": fii["net_futures"],
            "dii_net": dii["net_futures"],
            "fii_color": fii["color"],
            "dii_color": dii["color"],
        }

    if score >= 2:
        trend, color = "BULLISH", "green"
    elif score <= -2:
        trend, color = "BEARISH", "red"
    else:
        trend, color = "NEUTRAL", "gray"
    return {
        "data": table,
        "summary_cards": cards,
        "divergence": divergence,
        "overall_trend": trend,
        "overall_color": color,
        "score": score,
    }


# ------------------------------------------------------ OI Changes tab ---
# (display name, long column, short column, is_put)
# For puts the colours are directional: adding long puts is bearish (red), adding short puts is bullish (green).
OI_INSTRUMENTS = [
    ("Index Futures", "Future Index Long", "Future Index Short", False),
    ("Index Calls", "Option Index Call Long", "Option Index Call Short", False),
    ("Index Puts", "Option Index Put Long", "Option Index Put Short", True),
    ("Stock Futures", "Future Stock Long", "Future Stock Short", False),
    ("Stock Calls", "Option Stock Call Long", "Option Stock Call Short", False),
    ("Stock Puts", "Option Stock Put Long", "Option Stock Put Short", True),
]


def inr(n: int, signed: bool = False) -> str:
    """Indian digit grouping (12,34,567), the way traders here read open interest."""
    sign = "-" if n < 0 else ("+" if signed and n > 0 else "")
    s = str(abs(n))
    if len(s) > 3:
        head, tail, parts = s[:-3], s[-3:], []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts + [tail])
    return sign + s


def _dir_tone(value: int, bullish_when_positive: bool) -> str:
    if value == 0:
        return "gray"
    return "green" if (value > 0) == bullish_when_positive else "red"


def build_oi_changes(sessions: list[tuple[date, dict]]) -> dict:
    """
    Per instrument and participant: change in longs, change in shorts, net buy/sell today,
    and the net position carried today / 1 session ago / 2 sessions ago.
    """
    cur, prev = sessions[0][1], sessions[1][1]
    history = [s[1] for s in sessions[:3]]  # today, prev, prev-2
    col_dates = [s[0].strftime("%d %b") for s in sessions[:3]]

    blocks = []
    for name, lcol, scol, is_put in OI_INSTRUMENTS:
        # Colour logic: bullish_when_positive is flipped for puts.
        long_bull, short_bull = (not is_put), is_put
        rows, tot_net, tot_pos = [], 0, [0, 0, 0]
        for key, label in PARTICIPANTS:
            d_long = _g(cur[key], lcol) - _g(prev[key], lcol)
            d_short = _g(cur[key], scol) - _g(prev[key], scol)
            d_net = d_long - d_short
            tot_net += d_net
            pos = []
            for i in range(3):
                if i < len(history):
                    v = net(history[i][key], lcol, scol)
                    tot_pos[i] += v
                    pos.append({"t": inr(v), "c": _dir_tone(v, long_bull)})
                else:
                    pos.append({"t": "—", "c": "gray"})
            rows.append(
                {
                    "name": label,
                    "long": {
                        "label": "Added Longs"
                        if d_long > 0
                        else "Closed Longs"
                        if d_long < 0
                        else "No change",
                        "t": inr(d_long),
                        "c": _dir_tone(d_long, long_bull),
                    },
                    "short": {
                        "label": "Added Shorts"
                        if d_short > 0
                        else "Closed Shorts"
                        if d_short < 0
                        else "No change",
                        "t": inr(d_short),
                        "c": _dir_tone(d_short, short_bull),
                    },
                    "net": {
                        "label": "Bought Net"
                        if d_net > 0
                        else "Sold Net"
                        if d_net < 0
                        else "No change",
                        "t": inr(d_net),
                        "c": _dir_tone(d_net, long_bull),
                    },
                    "pos": pos,
                }
            )
        blocks.append(
            {
                "name": name,
                "rows": rows,
                "total_net": inr(tot_net) if tot_net else "–",
                "total_pos": [inr(v) if v else "–" for v in tot_pos],
            }
        )

    # Right-hand panel: the same net buy/sell numbers, grouped by participant.
    by_participant = []
    for p_idx, (_, label) in enumerate(PARTICIPANTS):
        by_participant.append(
            {
                "name": label,
                "rows": [
                    {
                        "label": b["rows"][p_idx]["net"]["label"],
                        "c": b["rows"][p_idx]["net"]["c"],
                        "instrument": b["name"],
                        "t": b["rows"][p_idx]["net"]["t"],
                    }
                    for b in blocks
                ],
            }
        )
    return {
        "blocks": blocks,
        "by_participant": by_participant,
        "col_dates": col_dates,
        "date": sessions[0][0].strftime("%d %b %Y"),
        "prev_date": sessions[1][0].strftime("%d %b %Y"),
    }


def build_dashboard(anchor: date) -> dict | None:
    """Everything the page needs, computed from at most TREND_DAYS sessions."""
    sessions = load_sessions(anchor)
    if len(sessions) < 2:
        return None
    chron = list(reversed(sessions))  # oldest -> newest for time series
    cur_date, cur = sessions[0]
    prev = sessions[1][1]
    dates = [d.strftime("%d %b") for d, _ in chron]

    activity = build_activity(cur, prev)
    m = {lbl: metrics(cur[k]) for k, lbl in PARTICIPANTS}
    col = lambda field: [m[lbl][field] for lbl in LABELS]  # one value per participant

    # --- market-wide totals for the % normalisation of Call / Put Diff -------
    call_tot = sum(m[l]["call_long"] for l in LABELS)
    put_tot = sum(m[l]["put_long"] for l in LABELS)
    call_pct = [pct(v, call_tot) for v in col("call_net")]
    put_pct = [pct(v, put_tot) for v in col("put_net")]

    # --- time series (oldest -> newest) --------------------------------------
    day_m = [{lbl: metrics(r[k]) for k, lbl in PARTICIPANTS} for _, r in chron]
    fii_idx = [d["FII"]["fut_net"] for d in day_m]
    fii_stk = [
        net(r["FII"], "Future Stock Long", "Future Stock Short") for _, r in chron
    ]

    trend_table = [
        {
            "date": dt,
            **{
                lbl: {
                    "value": fmt_pct(d[lbl]["blended"]),
                    "color": _tone(d[lbl]["blended"]),
                }
                for lbl in LABELS
            },
        }
        for dt, d in zip(dates, day_m)
    ]
    aggr_rows = [
        {
            "name": lbl,
            "values": [
                round(day_m[i][lbl]["blended"] - day_m[i - 1][lbl]["blended"], 1)
                for i in range(1, len(chron))
            ],
        }
        for lbl in LABELS
    ]

    # --- tables ---------------------------------------------------------------
    cell = lambda v, text: {"t": text, "c": _tone(v)}
    sent_table = [
        {
            "name": lbl,
            "fut_net": cell(x["fut_net"], fmt_signed(x["fut_net"])),
            "fut_bias": cell(x["fut_bias"], fmt_pct(x["fut_bias"])),
            "call": cell(x["call_net"], fmt_signed(x["call_net"])),
            "put": cell(
                -x["put_net"], fmt_signed(x["put_net"])
            ),  # net long puts = bearish -> red
            "opt_net": cell(x["opt_net"], fmt_signed(x["opt_net"])),
            "opt_bias": cell(x["opt_bias"], fmt_pct(x["opt_bias"])),
            "blended": cell(x["blended"], fmt_pct(x["blended"])),
            "signal": x["signal"],
            "signal_color": x["signal_color"],
        }
        for lbl, x in m.items()
    ]

    heat_cols = [
        ("Fut Long", "Future Index Long"),
        ("Fut Short", "Future Index Short"),
        ("Call Long", "Option Index Call Long"),
        ("Call Short", "Option Index Call Short"),
        ("Put Long", "Option Index Put Long"),
        ("Put Short", "Option Index Put Short"),
    ]

    # --- KPI cards ------------------------------------------------------------
    fii, fii_card = (
        m["FII"],
        next(c for c in activity["summary_cards"] if c["name"] == "FII"),
    )
    kpis = [
        {
            "label": "Overall Bias",
            "value": activity["overall_trend"],
            "sub": f"FII-weighted day-over-day score {activity['score']:+d}",
            "color": activity["overall_color"],
        },
        {
            "label": "FII Net Futures Δ",
            "value": fii_card["net_futures"],
            "sub": f"Net now {fmt_signed(fii['fut_net'])} contracts",
            "color": fii_card["color"],
        },
        {
            "label": "FII Index Futures Long %",
            "value": f"{fii['fut_long_pct']:.1f}%",
            "sub": f"Long {fii['fut_long']:,} · Short {fii['fut_short']:,}",
            "color": "green"
            if fii["fut_long_pct"] > 50
            else "red"
            if fii["fut_long_pct"] < 50
            else "gray",
        },
        {
            "label": "FII Blended Bias",
            "value": fmt_pct(fii["blended"]),
            "sub": f"Futures {fmt_pct(fii['fut_bias'])} · Options {fmt_pct(fii['opt_bias'])}",
            "color": fii["signal_color"],
        },
    ]

    payload = {
        "labels": LABELS,
        "long_pct": {
            "Full book": col("book_long_pct"),
            "Index futures": col("fut_long_pct"),
            "Index options (bullish OI)": col("opt_bull_pct"),
        },
        "contrib": {
            "Index Futures": contribution(
                cur, "Future Index Long", "Future Index Short"
            ),
            "Index Calls": contribution(
                cur, "Option Index Call Long", "Option Index Call Short"
            ),
            "Index Puts": contribution(
                cur, "Option Index Put Long", "Option Index Put Short"
            ),
        },
        "diff": {
            "call": col("call_net"),
            "put": col("put_net"),
            "opt_net": col("opt_net"),
            "call_pct": call_pct,
            "put_pct": put_pct,
            "net_pct": [round(c - p, 1) for c, p in zip(call_pct, put_pct)],
        },
        "bias": {
            "fut": col("fut_bias"),
            "opt": col("opt_bias"),
            "blended": col("blended"),
        },
        "fii_trend": {
            "dates": dates,
            "futures": fii_idx,
            "calls": [d["FII"]["call_net"] for d in day_m],
            "puts": [d["FII"]["put_net"] for d in day_m],
        },
        "fii_rotation": {"dates": dates, "index": fii_idx, "stock": fii_stk},
        "aggr": {"dates": dates[1:], "rows": aggr_rows},
        "heat": {
            "columns": [c for c, _ in heat_cols],
            "rows": [
                {"name": lbl, "values": [_g(cur[k], c) for _, c in heat_cols]}
                for k, lbl in PARTICIPANTS
            ],
        },
        "threshold": SIGNAL_THRESHOLD,
    }
    return {
        "date": cur_date.strftime("%d %b %Y"),
        "data_date_iso": cur_date.isoformat(),
        "prev_date": sessions[1][0].strftime("%d %b %Y"),
        "data_ok": integrity_ok(cur),
        "activity": activity,
        "kpis": kpis,
        "trend_table": trend_table,
        "sent_table": sent_table,
        "metrics": m,
        "payload": payload,
        "oi_changes": build_oi_changes(sessions),
    }


# -------------------------------------------------------- live NSE (cached) ---
def _nse_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        s.get(NSE_HOME_URL, timeout=4)
    except requests.RequestException:
        pass
    return s


def _cached(key: str, fn):
    now = time.time()
    hit = _live.get(key)
    if hit and now - hit[0] < LIVE_TTL:
        return hit[1]
    if now - _live_fail.get(key, 0) < LIVE_FAIL_TTL:
        return hit[1] if hit else None
    try:
        value = fn()
    except Exception as exc:
        log.warning("%s fetch failed: %s", key, exc)
        _live_fail[key] = now
        return hit[1] if hit else None
    if value is not None:
        _live[key] = (now, value)
    return value


def _fetch_nifty() -> dict | None:
    resp = _nse_session().get(NSE_INDICES_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("index") == "NIFTY 50":
            return {
                "price": item.get("last", 0),
                "change": item.get("variation", 0),
                "pct_change": item.get("percentChange", 0),
            }
    return None


def _fetch_option_chain() -> dict | None:
    resp = _nse_session().get(NSE_OPTION_CHAIN_URL, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    records = resp.json().get("records", {})
    spot = records.get("underlyingValue") or 0
    expiries = records.get("expiryDates") or []
    nearest = expiries[0] if expiries else None

    ce, pe = {}, {}
    for rec in records.get("data", []):
        if nearest and rec.get("expiryDate") != nearest:
            continue
        strike = rec.get("strikePrice")
        if strike is None or (spot and not spot * 0.9 <= strike <= spot * 1.1):
            continue
        if "CE" in rec:
            ce[strike] = rec["CE"].get("openInterest", 0)
        if "PE" in rec:
            pe[strike] = rec["PE"].get("openInterest", 0)
    total_ce, total_pe = sum(ce.values()), sum(pe.values())
    if not total_ce:
        return None
    pcr = round(total_pe / total_ce, 2)

    def pain(price: float) -> float:
        return sum(max(0, price - k) * oi for k, oi in ce.items()) + sum(
            max(0, k - price) * oi for k, oi in pe.items()
        )

    max_pain = min(sorted(set(ce) | set(pe)), key=pain)
    top = lambda d: [
        {"strike": k, "oi": v} for k, v in sorted(d.items(), key=lambda x: -x[1])[:5]
    ]
    return {
        "pcr": pcr,
        "pcr_signal": "Bullish"
        if pcr >= 1.2
        else "Bearish"
        if pcr <= 0.8
        else "Neutral",
        "pcr_color": "green" if pcr >= 1.2 else "red" if pcr <= 0.8 else "gray",
        "max_pain": max_pain,
        "spot": spot,
        "expiry": nearest,
        "top_ce": top(ce),
        "top_pe": top(pe),
        "total_ce_oi": total_ce,
        "total_pe_oi": total_pe,
    }


def get_nifty_spot() -> dict | None:
    return _cached("nifty", _fetch_nifty)


def get_option_chain_summary() -> dict | None:
    if not ENABLE_OPTION_CHAIN:
        return None
    return _cached("option_chain", _fetch_option_chain)


# ---------------------------------------------------------------- routes ---
@app.after_request
def cache_headers(resp):
    if (
        request.method == "GET"
        and resp.status_code == 200
        and "Cache-Control" not in resp.headers
    ):
        ttl = (
            60
            if request.path in ("/api/live", "/api/nifty", "/api/option-chain")
            else 120
        )
        resp.headers["Cache-Control"] = (
            f"public, s-maxage={ttl}, stale-while-revalidate=300"
        )
    return resp


@app.route("/")
def index():
    today = now_ist().date()
    raw = request.args.get("date", "").strip()
    anchor, notice = today, None
    if raw:
        try:
            anchor = date.fromisoformat(raw)
            if anchor > today:
                anchor, notice = (
                    today,
                    "Future dates have no data - showing the latest session.",
                )
        except ValueError:
            notice = "Invalid date - showing the latest session."

    dash = None
    try:
        dash = build_dashboard(anchor)
    except Exception:
        log.exception("Dashboard build failed")
        notice = "Something went wrong while loading market data."

    if dash and raw and notice is None and dash["data_date_iso"] != raw:
        notice = f"No F&O data for {raw}. Showing the nearest earlier session ({dash['date']})."

    return render_template(
        "index.html",
        dash=dash,
        notice=notice,
        selected_date=raw,
        today=today.isoformat(),
        auto_refresh=not raw,
        show_option_chain=ENABLE_OPTION_CHAIN,
        current_year=today.year,
        current_month=today.strftime("%B"),
    )


def _parse_date_arg():
    raw = request.args.get("date")
    if not raw:
        return now_ist().date(), None
    try:
        return date.fromisoformat(raw), None
    except ValueError:
        return None, (jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400)


@app.route("/api/activity")
def api_activity():
    anchor, err = _parse_date_arg()
    if err:
        return err
    dash = build_dashboard(anchor)
    if not dash:
        return jsonify({"error": "No data available."}), 503
    return jsonify(
        {"date": dash["date"], **dash["activity"], "trend_table": dash["trend_table"]}
    )


@app.route("/api/sentiment")
def api_sentiment():
    """Per-participant positions, bias % and signal for the chosen (or latest) session."""
    anchor, err = _parse_date_arg()
    if err:
        return err
    dash = build_dashboard(anchor)
    if not dash:
        return jsonify({"error": "No data available."}), 503
    return jsonify(
        {
            "date": dash["data_date_iso"],
            "data_ok": dash["data_ok"],
            "signal_threshold_pct": SIGNAL_THRESHOLD,
            "data": dash["metrics"],
            "contribution": dash["payload"]["contrib"],
        }
    )


@app.route("/api/live")
def api_live():
    """Nifty spot (and the option chain when enabled), fetched in parallel."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        nifty_f = pool.submit(get_nifty_spot)
        chain_f = pool.submit(get_option_chain_summary)
        nifty, chain = nifty_f.result(), chain_f.result()
    return jsonify(
        {
            "nifty": nifty,
            "option_chain": chain,
            "option_chain_enabled": ENABLE_OPTION_CHAIN,
            "as_of": now_ist().strftime("%H:%M:%S"),
        }
    )


@app.route("/api/nifty")
def api_nifty():
    data = get_nifty_spot()
    return (
        (jsonify(data), 200)
        if data
        else (jsonify({"error": "Unable to fetch Nifty spot."}), 503)
    )


@app.route("/api/option-chain")
def api_option_chain():
    if not ENABLE_OPTION_CHAIN:
        return jsonify({"error": "Option chain is currently disabled."}), 404
    data = get_option_chain_summary()
    return (
        (jsonify(data), 200)
        if data
        else (jsonify({"error": "Unable to fetch option chain."}), 503)
    )


@app.route("/api/health")
def api_health():
    return jsonify(
        {
            "status": "ok",
            "time_ist": now_ist().isoformat(),
            "cache_dir": CACHE_DIR,
            "bundled_history": os.path.isdir(DATA_DIR),
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5001)
