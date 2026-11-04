"""
Crypto Scanner Bot for KCEX futures traders (signals only, no auto-trading).

KCEX has no public API, so market data comes from MEXC's public futures API
(same USDT-perpetual pairs, very close prices). The bot scans liquid pairs with
the v2 strategy in strategy.py (EMA + MACD + RSI + volume on 15m, 1h/4h trend
filter, ATR stop and targets), sends signals to Telegram, records every signal,
follows it until stop or target, and reports each result plus a daily scorecard.

Environment variables (set them in Render, never in code):
  TELEGRAM_BOT_TOKEN   token from @BotFather            (required)
  TELEGRAM_CHAT_ID     your chat / channel id           (required)
  MIN_VOLUME_USDT      min 24h turnover, default 5000000
  SCAN_INTERVAL_MIN    minutes between scans, default 5
  COOLDOWN_MIN         minutes before repeating a symbol, default 120
  MAX_LEVERAGE         leverage cap shown in signals, default 5
  RISK_PERCENT         account risk per trade shown, default 1
  SIGNAL_LOG           file for the signal journal, default signals.jsonl

Every signal and result is also printed as one "SIGNAL_JSON"/"RESULT_JSON" log
line, so the record survives in Render's logs even if the free instance's disk
is wiped on restart.
"""

import json
import os
import time
import threading
import logging
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

import strategy as S

# ---------------- settings ----------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
MIN_VOLUME = float(os.getenv("MIN_VOLUME_USDT", "5000000"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL_MIN", "5")) * 60
COOLDOWN = int(os.getenv("COOLDOWN_MIN", "120")) * 60
MAX_LEV = int(os.getenv("MAX_LEVERAGE", "5"))
RISK_PCT = float(os.getenv("RISK_PERCENT", "1"))
SIGNAL_LOG = os.getenv("SIGNAL_LOG", "signals.jsonl")

BASE = "https://contract.mexc.com/api/v1/contract"
EXCLUDE = {"USDC_USDT"}
PUMP_PCT, PUMP_VOL = 4.0, 3.0
MAX_HOLD_BARS = 96  # follow a signal for 24h of 15m candles, then close it

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scanner")
session = requests.Session()
last_signal = {}
open_signals = []   # signals still being followed
lock = threading.Lock()


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
    return [
        (t["symbol"], float(t.get("fundingRate") or 0))
        for t in data
        if t["symbol"].endswith("_USDT") and t["symbol"] not in EXCLUDE
        and float(t.get("amount24") or 0) >= MIN_VOLUME
    ]


def klines(symbol, interval, need):
    d = get(f"{BASE}/kline/{symbol}", {"interval": interval})
    if not d or len(d.get("close", [])) < need + 1:
        return None
    # drop the still-open candle so signals don't repaint
    k = {key: [float(x) for x in d[key][:-1]] for key in ("open", "high", "low", "close", "vol")}
    k["time"] = [int(t) for t in d["time"][:-1]]
    return k


# ---------------- journal ----------------
def journal(kind, rec):
    rec = dict(rec, kind=kind)
    log.info("%s_JSON %s", kind, json.dumps(rec, ensure_ascii=False))
    try:
        with open(SIGNAL_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("journal write failed: %s", e)


def load_journal():
    """Rebuild open signals after a restart (works while the disk survives)."""
    if not os.path.exists(SIGNAL_LOG):
        return
    sigs, done = {}, set()
    with open(SIGNAL_LOG, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "SIGNAL":
                sigs[r["id"]] = r
            elif r.get("kind") == "RESULT":
                done.add(r["id"])
    open_signals.extend(s for i, s in sigs.items() if i not in done)
    log.info("journal loaded, %d open signals", len(open_signals))


def stats(since_ts=0):
    if not os.path.exists(SIGNAL_LOG):
        return None
    res = []
    with open(SIGNAL_LOG, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "RESULT" and r.get("signal_ts", 0) >= since_ts:
                res.append(r)
    if not res:
        return None
    wins = [r for r in res if r["R"] > 0]
    return {"n": len(res), "wins": len(wins), "losses": len(res) - len(wins),
            "total_R": sum(r["R"] for r in res)}


# ---------------- strategy ----------------
def analyze(symbol, funding):
    m15 = klines(symbol, "Min15", S.EMA_TREND + 10)
    h1 = klines(symbol, "Min60", S.EMA_TREND + 5)
    h4 = klines(symbol, "Hour4", 55)
    if not m15 or not h1 or not h4:
        return None
    E, H = S.prep_entry(m15), S.prep_htf(h1, h4)
    i = len(m15["close"]) - 1
    bias = S.htf_bias(H, len(h1["close"]) - 1, len(h4["close"]) - 1)
    sig = S.signal_at(E, i, bias)
    if sig:
        lev = max(1, min(MAX_LEV, int(10 / sig["stop_pct"]))) if sig["stop_pct"] > 0 else 1
        candle_close = m15["time"][i] + 900
        return dict(sig, type="SIGNAL", symbol=symbol, lev=lev, funding=funding * 100,
                    ts=candle_close, id=f"{symbol}-{candle_close}")
    c, v = E["c"], E["v"]
    change = (c[i] - c[i - 1]) / c[i - 1] * 100
    vol_x = v[i] / (E["vavg"][i] or 1e-12)
    if abs(change) >= PUMP_PCT and vol_x >= PUMP_VOL:
        return {"type": "ALERT", "symbol": symbol, "price": c[i], "change": change, "vol_x": vol_x}
    return None


def follow_up():
    """Check every open signal against 15m candles since its entry."""
    with lock:
        pending = list(open_signals)
    for s in pending:
        k = klines(s["symbol"], "Min15", 2)
        if not k:
            continue
        idx = [n for n, t in enumerate(k["time"]) if t >= s["ts"]]
        if not idx:
            continue
        a = idx[0]
        out, r, bars = S.resolve(s, k["high"][a:], k["low"][a:], k["close"][a:],
                                 max_bars=MAX_HOLD_BARS)
        if out == "OPEN":
            continue
        rec = {"id": s["id"], "symbol": s["symbol"], "side": s["side"], "result": out,
               "R": round(r, 2), "bars": bars, "signal_ts": s["ts"], "closed_at": int(time.time())}
        journal("RESULT", rec)
        send(result_message(s, rec))
        with lock:
            open_signals[:] = [x for x in open_signals if x["id"] != s["id"]]
        time.sleep(0.15)


# ---------------- telegram ----------------
FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def fa(x):
    return str(x).translate(FA)


def fmt(x):
    s = f"{x:.8f}".rstrip("0").rstrip(".") if x < 1 else f"{x:,.4f}".rstrip("0").rstrip(".")
    return s


def send(text):
    if not TOKEN or not CHAT_ID:
        log.warning("TELEGRAM NOT CONFIGURED - message NOT delivered:\n%s", text)
        return
    try:
        r = session.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                         json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                               "disable_web_page_preview": True}, timeout=15)
        if not r.ok:
            log.error("telegram send failed: %s %s", r.status_code, r.text[:200])
    except Exception as e:
        log.error("telegram send failed: %s", e)


def message(s):
    pair = s["symbol"].replace("_", "/")
    if s["type"] == "ALERT":
        arrow = "🚀 پامپ" if s["change"] > 0 else "🔻 دامپ"
        return (f"{arrow} <b>{pair}</b> (فیوچرز KCEX)\n"
                f"حرکت ۱۵ دقیقه: {fa(format(s['change'], '+.2f'))}%  |  "
                f"حجم {fa(format(s['vol_x'], '.1f'))} برابر\n"
                f"قیمت: {fmt(s['price'])}\n⚠️ فقط زیر نظر بگیر، هنوز ستاپ ورود نیست.")
    icon = "🟢" if s["side"] == "LONG" else "🔴"
    side_fa = "لانگ (خرید)" if s["side"] == "LONG" else "شورت (فروش)"
    trend = "صعودی" if s["side"] == "LONG" else "نزولی"
    return (f"{icon} <b>{side_fa} {pair}</b> (فیوچرز KCEX، تایم ۱۵ دقیقه)\n"
            f"📌 روند ۱ و ۴ ساعته {trend} + کراس MACD + RSI {fa(format(s['rsi'], '.0f'))} "
            f"+ حجم {fa(format(s['vol_x'], '.1f'))} برابر\n\n"
            f"ورود: {fmt(s['price'])}\n"
            f"حد ضرر: {fmt(s['sl'])} ({fa(format(s['stop_pct'], '.2f'))}%، "
            f"{fa(S.SL_ATR)} برابر ATR)\n"
            f"تارگت ۱: {fmt(s['tp1'])} ({fa(S.TP1_R)}R، نصف پوزیشن را ببند و حد ضرر را به نقطه ورود ببر)\n"
            f"تارگت ۲: {fmt(s['tp2'])} ({fa(S.TP2_R)}R)\n\n"
            f"اهرم: حداکثر {fa(s['lev'])}x، مارجین Isolated\n"
            f"ریسک: {fa(RISK_PCT)}% حساب در هر معامله\n"
            f"فاندینگ {fa(format(s['funding'], '+.4f'))}%\n"
            f"نتیجه این سیگنال خودکار ثبت و گزارش می‌شود. این توصیه مالی نیست.")


def result_message(s, rec):
    pair = s["symbol"].replace("_", "/")
    label = {"SL": "❌ حد ضرر خورد", "TP1": "✅ تارگت ۱ خورد (بقیه سر به سر بسته شد)",
             "TP2": "✅✅ تارگت ۲ خورد", "TIME": "⏱ بعد از ۲۴ ساعت بسته شد"}[rec["result"]]
    hours = rec["bars"] * 15 / 60
    return (f"{label}\n<b>{pair}</b> {'لانگ' if s['side'] == 'LONG' else 'شورت'} از {fmt(s['price'])}\n"
            f"نتیجه: {fa(format(rec['R'], '+.2f'))}R (بعد از کارمزد)، بعد از {fa(format(hours, '.1f'))} ساعت")


def daily_report():
    st = stats(time.time() - 86400)
    if not st:
        return
    send(f"📊 کارنامه ۲۴ ساعت گذشته\n"
         f"بسته‌شده: {fa(st['n'])}  |  سود: {fa(st['wins'])}  |  ضرر: {fa(st['losses'])}\n"
         f"جمع: {fa(format(st['total_R'], '+.2f'))}R  |  "
         f"باز: {fa(len(open_signals))}")


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
            if s["type"] == "SIGNAL":
                if any(o["id"] == s["id"] for o in open_signals):
                    continue
                journal("SIGNAL", {k: s[k] for k in ("id", "symbol", "side", "price", "sl", "tp1",
                                                     "tp2", "stop_pct", "rsi", "vol_x", "ts")})
                with lock:
                    open_signals.append(s)
            send(message(s))
            last_signal[sym] = time.time()
            found += 1
        time.sleep(0.15)
    log.info("scan done, %d signals, %d open", found, len(open_signals))


def scanner_loop():
    if not TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing: signals will only appear in logs!")
    load_journal()
    send("✅ اسکنر کریپتو نسخه ۲ شروع به کار کرد (فیوچرز KCEX، ورود ۱۵ دقیقه + روند ۱ و ۴ ساعته).")
    last_day = datetime.now(timezone.utc).date()
    while True:
        start = time.time()
        try:
            follow_up()
            scan_once()
        except Exception as e:
            log.error("scan failed: %s", e)
        today = datetime.now(timezone.utc).date()
        if today != last_day:
            last_day = today
            daily_report()
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
    port = int(os.getenv("PORT", "10000"))
    log.info("health server on port %d", port)
    HTTPServer(("0.0.0.0", port), Health).serve_forever()
