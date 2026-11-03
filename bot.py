"""
Crypto Scanner Bot for KCEX futures traders.

KCEX has no public API, so market data comes from MEXC's public futures API
(same USDT-perpetual pairs, very close prices). The bot scans liquid pairs,
finds trend + momentum setups and volume spikes, and sends signals to Telegram
with entry, stop-loss, take-profits and suggested leverage. Trades are placed
manually on KCEX.

Environment variables (set them in Render, never in code):
  TELEGRAM_BOT_TOKEN   token from @BotFather            (required)
  TELEGRAM_CHAT_ID     your chat / channel id           (required)
  MIN_VOLUME_USDT      min 24h turnover, default 5000000
  SCAN_INTERVAL_MIN    minutes between scans, default 5
  COOLDOWN_MIN         minutes before repeating a symbol, default 120
  MAX_LEVERAGE         leverage cap shown in signals, default 5
  RISK_PERCENT         account risk per trade shown, default 1
"""

import os
import time
import threading
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

# ---------------- settings ----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_VOLUME = float(os.getenv("MIN_VOLUME_USDT", "5000000"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MIN", "5")) * 60
COOLDOWN = int(os.getenv("COOLDOWN_MIN", "120")) * 60
MAX_LEV = int(os.getenv("MAX_LEVERAGE", "5"))
RISK_PCT = float(os.getenv("RISK_PERCENT", "1"))

BASE = "https://contract.mexc.com/api/v1/contract"
EXCLUDE = {"USDC_USDT"}  # stablecoins etc.

# strategy parameters
EMA_FAST, EMA_SLOW, EMA_TREND = 20, 50, 200
RSI_LEN, ATR_LEN = 14, 14
VOL_SPIKE = 2.0          # breakout volume vs 20-candle average
PUMP_PCT = 4.0           # % move in one 15m candle for pump/dump alert
PUMP_VOL = 3.0           # volume multiple for pump/dump alert
SL_ATR = 1.5             # stop distance in ATRs
TP1_R, TP2_R = 1.5, 3.0  # take-profits in R multiples

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")
session = requests.Session()
last_signal = {}


# ---------------- indicators ----------------
def ema(values, length):
    k = 2 / (length + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, length=14):
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    avg_g = sum(gains[1:length + 1]) / length
    avg_l = sum(losses[1:length + 1]) / length
    out = [50.0] * (length + 1)
    for i in range(length + 1, len(closes)):
        avg_g = (avg_g * (length - 1) + gains[i]) / length
        avg_l = (avg_l * (length - 1) + losses[i]) / length
        out.append(100.0 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l))
    return out


def atr(highs, lows, closes, length=14):
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    val = sum(trs[:length]) / length
    for tr in trs[length:]:
        val = (val * (length - 1) + tr) / length
    return val


# ---------------- data ----------------
def get(url, params=None):
    for attempt in range(3):
        try:
            r = session.get(url, params=params, timeout=15)
            r.raise_for_status()
            j = r.json()
            if j.get("success"):
                return j["data"]
        except Exception as e:
            log.warning("request failed (%s): %s", attempt + 1, e)
        time.sleep(2)
    return None


def liquid_symbols():
    data = get(f"{BASE}/ticker") or []
    syms = [
        (t["symbol"], float(t.get("fundingRate") or 0))
        for t in data
        if t["symbol"].endswith("_USDT")
        and t["symbol"] not in EXCLUDE
        and float(t.get("amount24") or 0) >= MIN_VOLUME
    ]
    return syms


def klines(symbol, interval):
    d = get(f"{BASE}/kline/{symbol}", {"interval": interval})
    if not d or len(d.get("close", [])) < EMA_TREND + 5:
        return None
    # drop the still-open candle so signals don't repaint
    return {k: [float(x) for x in d[k][:-1]] for k in ("open", "high", "low", "close", "vol")}


# ---------------- strategy ----------------
def analyze(symbol, funding):
    m15 = klines(symbol, "Min15")
    h1 = klines(symbol, "Min60")
    if not m15 or not h1:
        return None

    c, h, l, v = m15["close"], m15["high"], m15["low"], m15["vol"]
    price = c[-1]
    trend = ema(h1["close"], EMA_TREND)[-1]
    fast, slow = ema(c, EMA_FAST), ema(c, EMA_SLOW)
    r = rsi(c, RSI_LEN)
    a = atr(h, l, c, ATR_LEN)
    avg_vol = sum(v[-21:-1]) / 20 or 1e-9
    vol_x = v[-1] / avg_vol
    hi20, lo20 = max(h[-21:-1]), min(l[-21:-1])
    change = (c[-1] - c[-2]) / c[-2] * 100

    side, reason = None, ""
    uptrend = price > trend and fast[-1] > slow[-1]
    downtrend = price < trend and fast[-1] < slow[-1]

    if uptrend and c[-1] > hi20 and vol_x >= VOL_SPIKE and r[-1] < 75:
        side, reason = "LONG", f"Breakout above 20-candle high, volume x{vol_x:.1f}"
    elif uptrend and r[-2] < 40 <= r[-1]:
        side, reason = "LONG", "Pullback in uptrend, RSI turning up from 40"
    elif downtrend and c[-1] < lo20 and vol_x >= VOL_SPIKE and r[-1] > 25:
        side, reason = "SHORT", f"Breakdown below 20-candle low, volume x{vol_x:.1f}"
    elif downtrend and r[-2] > 60 >= r[-1]:
        side, reason = "SHORT", "Bounce in downtrend, RSI turning down from 60"
    elif abs(change) >= PUMP_PCT and vol_x >= PUMP_VOL:
        return {"type": "ALERT", "symbol": symbol, "price": price, "change": change, "vol_x": vol_x}

    if not side:
        return None

    stop_dist = SL_ATR * a
    if side == "LONG":
        sl, tp1, tp2 = price - stop_dist, price + stop_dist * TP1_R, price + stop_dist * TP2_R
    else:
        sl, tp1, tp2 = price + stop_dist, price - stop_dist * TP1_R, price - stop_dist * TP2_R
    stop_pct = stop_dist / price * 100
    # leverage so a stop-out loses ~RISK_PCT of the margin-sized position, capped
    lev = max(1, min(MAX_LEV, int(10 / stop_pct))) if stop_pct > 0 else 1

    return {
        "type": "SIGNAL", "symbol": symbol, "side": side, "reason": reason, "price": price,
        "sl": sl, "tp1": tp1, "tp2": tp2, "stop_pct": stop_pct, "lev": lev,
        "rsi": r[-1], "funding": funding * 100,
    }


# ---------------- telegram ----------------
def fmt(x):
    return f"{x:.8f}".rstrip("0").rstrip(".") if x < 1 else f"{x:,.4f}".rstrip("0").rstrip(".")


def send(text):
    if not TOKEN or not CHAT_ID:
        log.info("Telegram not configured. Message:\n%s", text)
        return
    try:
        session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                     json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                           "disable_web_page_preview": True}, timeout=15)
    except Exception as e:
        log.error("telegram send failed: %s", e)


def message(s):
    pair = s["symbol"].replace("_", "/")
    if s["type"] == "ALERT":
        arrow = "🚀 Pump" if s["change"] > 0 else "🔻 Dump"
        return (f"{arrow} <b>{pair}</b> (KCEX futures)\n"
                f"15m move: {s['change']:+.2f}%  |  Volume x{s['vol_x']:.1f}\n"
                f"Price: {fmt(s['price'])}\n⚠️ Watch only, no setup yet.")
    icon = "🟢" if s["side"] == "LONG" else "🔴"
    return (f"{icon} <b>{s['side']} {pair}</b> (KCEX futures, 15m)\n"
            f"📌 {s['reason']}\n\n"
            f"Entry: {fmt(s['price'])}\n"
            f"Stop-loss: {fmt(s['sl'])} ({s['stop_pct']:.2f}%)\n"
            f"TP1: {fmt(s['tp1'])} ({TP1_R}R, close 50% & move SL to entry)\n"
            f"TP2: {fmt(s['tp2'])} ({TP2_R}R)\n\n"
            f"Leverage: max {s['lev']}x, isolated margin\n"
            f"Risk: {RISK_PCT}% of account per trade\n"
            f"RSI {s['rsi']:.0f}  |  Funding {s['funding']:+.4f}%\n"
            f"Not financial advice.")


# ---------------- main loop ----------------
def scan_once():
    syms = liquid_symbols()
    log.info("scanning %d symbols", len(syms))
    found = 0
    for sym, funding in syms:
        if time.time() - last_signal.get(sym, 0) < COOLDOWN:
            continue
        try:
            s = analyze(sym, funding)
        except Exception as e:
            log.warning("%s analyze error: %s", sym, e)
            continue
        if s:
            send(message(s))
            last_signal[sym] = time.time()
            found += 1
        time.sleep(0.15)  # stay well under rate limits
    log.info("scan done, %d signals", found)


def scanner_loop():
    send("✅ Crypto Scanner started (KCEX futures, 15m + 1h trend).")
    while True:
        start = time.time()
        try:
            scan_once()
        except Exception as e:
            log.error("scan failed: %s", e)
        time.sleep(max(30, SCAN_INTERVAL - (time.time() - start)))


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=scanner_loop, daemon=True).start()
    # Render web services need an open port; this also serves as a health check
    port = int(os.getenv("PORT", "10000"))
    log.info("health server on port %d", port)
    HTTPServer(("0.0.0.0", port), Health).serve_forever()
