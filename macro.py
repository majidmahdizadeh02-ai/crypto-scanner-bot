"""
Macro / fundamental market regime for the scanner (v3).

Every REFRESH_MIN minutes it gathers, from free public sources:
  - Crypto: TOTAL, TOTAL2, BTC dominance, USDT dominance   (CoinGecko, CoinPaprika fallback)
  - S&P 500, Nasdaq, gold, oil, dollar index                (Yahoo Finance chart API)
  - Whale Alert Telegram channel (public web preview)       big exchange flows, stablecoin mints/burns
  - Headlines: CoinDesk RSS, CoinMarketCap headlines, Cointelegraph RSS,
    Google News for Iran/US and for the Fed / US jobs        keyword scoring
  - US economic calendar (ForexFactory weekly JSON)         Fed rate, NFP / unemployment, CPI ...

and turns it into one score from -100 (risk-off) to +100 (risk-on), an Iran/US
tension level, and a "blackout" flag around high-impact US data.

The news part is keyword based and cannot be backtested; treat it as a filter
that avoids the worst moments, not as a prediction.
"""

import json
import logging
import re
import threading
import time
import html as htmlmod
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET

import requests

log = logging.getLogger("macro")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}
REFRESH_MIN = 30
BLACKOUT_MIN = 30
RISK_OFF, RISK_ON = -35, 35

state = {"score": 0, "regime": "NEUTRAL", "tension": 0, "blackout": None, "parts": {},
         "notes": [], "headlines": [], "whales": {}, "events": [], "updated": 0}
_lock = threading.Lock()
_hist = []  # (ts, total, btc_d, usdt_d) snapshots for 4h deltas


def _get(url, **kw):
    r = requests.get(url, headers=UA, timeout=20, allow_redirects=True, **kw)
    r.raise_for_status()
    return r


def clamp(x, lo=-1.0, hi=1.0):
    return max(lo, min(hi, x))


# ---------------- crypto market ----------------
def crypto_global():
    try:
        d = _get("https://api.coingecko.com/api/v3/global").json()["data"]
        total = d["total_market_cap"]["usd"]
        btc_d = d["market_cap_percentage"]["btc"]
        usdt_d = d["market_cap_percentage"].get("usdt", 0)
        chg = d.get("market_cap_change_percentage_24h_usd", 0)
    except Exception as e:
        log.warning("coingecko failed: %s, trying coinpaprika", e)
        d = _get("https://api.coinpaprika.com/v1/global").json()
        total = d["market_cap_usd"]
        btc_d = d["bitcoin_dominance_percentage"]
        chg = d.get("market_cap_change_24h", 0)
        usdt_d = None
        try:
            t = _get("https://api.coinpaprika.com/v1/tickers/usdt-tether").json()
            usdt_d = t["quotes"]["USD"]["market_cap"] / total * 100
        except Exception:
            pass
    now = time.time()
    _hist.append((now, total, btc_d, usdt_d))
    while _hist and now - _hist[0][0] > 6 * 3600:
        _hist.pop(0)
    old = next((h for h in _hist if now - h[0] >= 3.5 * 3600), _hist[0])
    d4 = {
        "total_4h": (total / old[1] - 1) * 100 if old[1] else 0,
        "btc_d_4h": btc_d - old[2],
        "usdt_d_4h": (usdt_d - old[3]) if (usdt_d is not None and old[3] is not None) else 0,
    }
    total2 = total * (1 - btc_d / 100)
    return {"total": total, "total2": total2, "btc_d": btc_d, "usdt_d": usdt_d,
            "total_24h": chg, **d4}


# ---------------- traditional markets ----------------
# MEXC lists USDT perpetuals on the S&P 500, Nasdaq 100, gold and oil, so the same
# public API the scanner already uses gives their 24h change (no rate limits seen).
MEXC_TF = {"spx": "SPX500_USDT", "nasdaq": "NAS100_USDT", "gold": "XAU_USDT", "oil": "USOIL_USDT"}
YAHOO = {"dxy": "DX-Y.NYB"}


def mexc_tf(sym):
    d = _get(f"https://contract.mexc.com/api/v1/contract/ticker", params={"symbol": sym}).json()["data"]
    return {"price": float(d["lastPrice"]), "chg_1d": float(d["riseFallRate"]) * 100}


def yahoo(sym):
    j = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}",
             params={"range": "5d", "interval": "1h"}).json()["chart"]["result"][0]
    closes = [c for c in j["indicators"]["quote"][0]["close"] if c is not None]
    price = j["meta"].get("regularMarketPrice") or closes[-1]
    day_ref = closes[-8] if len(closes) > 8 else closes[0]
    return {"price": price, "chg_1d": (price / day_ref - 1) * 100}


def tradfi():
    out = {}
    for k, s in MEXC_TF.items():
        try:
            out[k] = mexc_tf(s)
        except Exception as e:
            log.warning("mexc %s failed: %s", s, e)
    for k, s in YAHOO.items():
        try:
            out[k] = yahoo(s)
        except Exception as e:
            log.warning("yahoo %s failed: %s", s, e)
    return out


# ---------------- whale alert ----------------
WHALE_RE = re.compile(r"([\d,\.]+)\s*[\$#](\w+)\s*\(([\d,]+)\s*USD\)\s*(.*)", re.I)
STABLES = {"USDT", "USDC", "DAI", "FDUSD", "PYUSD", "USDE"}


def _is_exchange(side):
    s = side.lower()
    return bool(s.strip()) and "unknown" not in s and "treasury" not in s


def whales(hours=4):
    page = _get("https://t.me/s/whale_alert_io").text
    blocks = page.split('class="tgme_widget_message_wrap')[1:]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    agg = {"to_ex": 0.0, "from_ex": 0.0, "mint": 0.0, "burn": 0.0, "n": 0, "top": [], "news": []}
    for b in blocks:
        tm = re.search(r'datetime="([^"]+)"', b)
        tx = re.search(r'tgme_widget_message_text[^>]*>(.*?)</div>', b, re.S)
        if not tm or not tx:
            continue
        try:
            when = datetime.fromisoformat(tm.group(1))
        except ValueError:
            continue
        if when < cutoff:
            continue
        text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", tx.group(1)))
        text = " ".join(w for w in text.split() if w not in ("🚨", "💵", "🔥", "📈", "📉"))
        m = WHALE_RE.search(text)
        if not m:
            if "Read Analysis" in text:
                agg["news"].append(text.replace("Read Analysis", "").strip())
            continue
        coin, usd, rest = m.group(2).upper(), float(m.group(3).replace(",", "")), m.group(4)
        rest = rest.replace("Details", "").strip()
        agg["n"] += 1
        low = rest.lower()
        frm, _, to = low.partition(" to ")
        frm = frm.replace("transferred from", "").strip()
        if coin in STABLES:
            if "minted" in low or ("treasury" in frm and _is_exchange(to)):
                agg["mint"] += usd
            elif "burned" in low or "treasury" in to:
                agg["burn"] += usd
        else:
            if _is_exchange(to) and not _is_exchange(frm):
                agg["to_ex"] += usd
            elif _is_exchange(frm) and not _is_exchange(to):
                agg["from_ex"] += usd
        agg["top"].append((usd, f"{m.group(1)} {coin} (${usd/1e6:,.0f}M) {rest}"[:140]))
    agg["top"] = [t for _, t in sorted(agg["top"], reverse=True)[:3]]
    return agg


# ---------------- headlines ----------------
FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "iran_us": "https://news.google.com/rss/search?q=Iran+United+States+when:6h&hl=en-US&gl=US&ceid=US:en",
    "us_macro": "https://news.google.com/rss/search?q=Federal+Reserve+OR+%22jobs+report%22+OR+inflation+when:1d&hl=en-US&gl=US&ceid=US:en",
}
BULL = ["surge", "rally", "soar", "jump", "record high", "all-time high", "approve", "approval", "inflow",
        "rate cut", "cuts rates", "dovish", "adoption", "etf inflows", "bullish", "rebound", "gain"]
BEAR = ["plunge", "crash", "slump", "tumble", "sell-off", "selloff", "hack", "exploit", "lawsuit", "sue",
        "ban", "outflow", "liquidat", "hawkish", "rate hike", "raises rates", "bearish", "fraud",
        "bankrupt", "recession", "drop", "falls", "sink"]
TENSION_UP = ["strike", "attack", "missile", "war", "military", "sanction", "hormuz", "retaliat",
              "escalat", "drone", "bomb", "threat", "seize", "clash", "nuclear"]
TENSION_DOWN = ["talks", "deal", "ceasefire", "agreement", "negotiat", "de-escalat", "diplomac", "truce"]


def _rss(url, hours=12):
    root = ET.fromstring(_get(url).content)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        pub = it.findtext("pubDate")
        try:
            when = parsedate_to_datetime(pub) if pub else None
        except Exception:
            when = None
        if when and when.tzinfo and when < cutoff:
            continue
        out.append(title)
    return out[:40]


def _cmc_headlines():
    page = _get("https://coinmarketcap.com/headlines/news/").text
    titles = re.findall(r'"title":"([^"]{20,200})"', page)
    seen, out = set(), []
    for t in titles:
        t = htmlmod.unescape(t)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:30]


def _kw(text, words):
    t = text.lower()
    return sum(1 for w in words if w in t)


def headlines():
    res = {"crypto": [], "iran_us": [], "us_macro": []}
    for name, url in FEEDS.items():
        try:
            items = _rss(url)
        except Exception as e:
            log.warning("feed %s failed: %s", name, e)
            continue
        key = name if name in res else "crypto"
        res[key] += items
    try:
        res["crypto"] += _cmc_headlines()
    except Exception as e:
        log.warning("cmc headlines failed: %s", e)
    crypto_s = sum(_kw(t, BULL) - _kw(t, BEAR) for t in res["crypto"])
    macro_s = sum(_kw(t, BULL) - _kw(t, BEAR) for t in res["us_macro"])
    n_ir = max(len(res["iran_us"]), 1)
    hot = sum(1 for t in res["iran_us"] if _kw(t, TENSION_UP) > _kw(t, TENSION_DOWN))
    calm = sum(1 for t in res["iran_us"] if _kw(t, TENSION_DOWN) > _kw(t, TENSION_UP))
    ten = (hot - calm) / n_ir     # -1..1 share of escalation headlines
    return {"crypto_sent": crypto_s, "macro_sent": macro_s, "tension_raw": ten,
            "n_crypto": len(res["crypto"]), "n_iran": len(res["iran_us"]),
            "sample_iran": res["iran_us"][:3], "sample_crypto": res["crypto"][:3]}


# ---------------- economic calendar ----------------
WATCH = ["fomc", "federal funds", "interest rate", "non-farm", "nonfarm", "unemployment", "cpi",
         "pce", "gdp", "powell", "ppi", "retail sales", "jobless"]


_cal = {"ts": 0, "data": None}


def calendar():
    # the feed updates rarely and rate-limits hard, so fetch it at most every 6 hours
    if _cal["data"] is None or time.time() - _cal["ts"] > 6 * 3600:
        try:
            _cal["data"] = _get("https://nfs.faireconomy.media/ff_calendar_thisweek.json").json()
            _cal["ts"] = time.time()
        except Exception:
            if _cal["data"] is None:
                raise
    data = _cal["data"]
    out = []
    for e in data:
        if e.get("country") != "USD" or e.get("impact") != "High":
            continue
        try:
            when = datetime.fromisoformat(e["date"])
        except Exception:
            continue
        out.append({"title": e["title"], "when": when.astimezone(timezone.utc),
                    "forecast": e.get("forecast", ""), "previous": e.get("previous", ""),
                    "key": any(w in e["title"].lower() for w in WATCH)})
    return sorted(out, key=lambda x: x["when"])


# ---------------- scoring ----------------
def compute():
    parts, notes = {}, []
    cg = tf = wh = hl = None
    events = []
    try:
        cg = crypto_global()
    except Exception as e:
        log.warning("crypto global failed: %s", e)
    try:
        tf = tradfi()
    except Exception as e:
        log.warning("tradfi failed: %s", e)
    try:
        wh = whales()
    except Exception as e:
        log.warning("whale alert failed: %s", e)
    try:
        hl = headlines()
    except Exception as e:
        log.warning("headlines failed: %s", e)
    try:
        events = calendar()
    except Exception as e:
        log.warning("calendar failed: %s", e)

    if cg:
        parts["total"] = clamp(cg["total_24h"] / 4) * 20 + clamp(cg["total_4h"] / 2) * 10
        parts["usdt_d"] = -clamp(cg["usdt_d_4h"] / 0.3) * 15
        parts["btc_d"] = -clamp(cg["btc_d_4h"] / 0.5) * 5        # BTC.D rising = alts weaker
    if tf:
        if "spx" in tf:
            parts["spx"] = clamp(tf["spx"]["chg_1d"] / 1.5) * 10
        if "nasdaq" in tf:
            parts["nasdaq"] = clamp(tf["nasdaq"]["chg_1d"] / 2) * 10
        if "gold" in tf:
            parts["gold"] = -clamp(max(0, tf["gold"]["chg_1d"]) / 2) * 5   # fear bid
        if "oil" in tf:
            parts["oil"] = -clamp(max(0, tf["oil"]["chg_1d"]) / 4) * 5     # supply shock
        if "dxy" in tf:
            parts["dxy"] = -clamp(tf["dxy"]["chg_1d"] / 0.7) * 5
    if wh and wh["n"]:
        net_ex = (wh["from_ex"] - wh["to_ex"]) / 3e8
        net_st = (wh["mint"] - wh["burn"]) / 5e8
        parts["whales"] = clamp(net_ex) * 8 + clamp(net_st) * 5
    if hl and wh and wh.get("news"):
        hl["crypto_sent"] += sum(_kw(t, BULL) - _kw(t, BEAR) for t in wh["news"])
    if hl:
        parts["news"] = clamp(hl["crypto_sent"] / 8) * 10 + clamp(hl["macro_sent"] / 6) * 5
    tension = 0
    if hl:
        tension = int(clamp(hl["tension_raw"] / 0.6, 0, 1) * 100) if hl.get("n_iran", 0) >= 5 else 0
        parts["iran_us"] = -tension / 100 * 15
    if tf and "oil" in tf and tf["oil"]["chg_1d"] > 4:
        tension = max(tension, 60)

    score = int(round(clamp(sum(parts.values()) / 100) * 100))
    regime = "RISK_OFF" if score <= RISK_OFF else "RISK_ON" if score >= RISK_ON else "NEUTRAL"

    now = datetime.now(timezone.utc)
    blackout = None
    for e in events:
        if e["key"] and abs((e["when"] - now).total_seconds()) <= BLACKOUT_MIN * 60:
            blackout = e["title"]
    with _lock:
        state.update(score=score, regime=regime, tension=tension, blackout=blackout,
                     parts={k: round(v, 1) for k, v in parts.items()},
                     cg=cg, tf=tf, whales=wh or {}, hl=hl or {},
                     events=[e for e in events if e["when"] > now - timedelta(hours=1)][:6],
                     updated=time.time())
    log.info("MACRO_JSON %s", json.dumps({"score": score, "regime": regime, "tension": tension,
                                          "blackout": blackout, "parts": state["parts"]}))
    return state


def loop():
    while True:
        try:
            compute()
        except Exception as e:
            log.error("macro compute failed: %s", e)
        time.sleep(REFRESH_MIN * 60)


def check_blackout():
    """Re-check the calendar window between refreshes (cheap, uses cached events)."""
    now = datetime.now(timezone.utc)
    with _lock:
        for e in state.get("events", []):
            if e["key"] and abs((e["when"] - now).total_seconds()) <= BLACKOUT_MIN * 60:
                return e["title"]
    return None


# ---------------- decisions ----------------
GOLD = {"XAU_USDT", "XAUT_USDT"}
SILVER = {"SILVER_USDT"}
METALS = GOLD | SILVER
NON_CRYPTO_SUFFIX = ("STOCK_USDT",)
NON_CRYPTO = {"USOIL_USDT", "UKOIL_USDT", "NAS100_USDT", "SPX500_USDT", "SPX_USDT"}


def asset_class(symbol):
    if symbol in METALS:
        return "metal"
    if symbol in NON_CRYPTO or symbol.endswith(NON_CRYPTO_SUFFIX):
        return "tradfi"
    return "crypto"


def allow(symbol, side):
    """Return (allowed, reason). Applies blackout, regime and tension filters."""
    if not state["updated"]:
        return True, ""
    b = check_blackout()
    if b:
        return False, f"blackout: {b}"
    cls, reg, ten = asset_class(symbol), state["regime"], state["tension"]
    if cls == "crypto":
        if side == "LONG" and (reg == "RISK_OFF" or (ten >= 70 and state["score"] < 0)):
            return False, "risk-off / tension"
        if side == "SHORT" and reg == "RISK_ON":
            return False, "risk-on"
    elif cls == "metal":
        if side == "SHORT" and (ten >= 50 or reg == "RISK_OFF"):
            return False, "safe-haven bid"
    return True, ""


REG_FA = {"RISK_ON": "🟢 ریسک‌پذیر", "RISK_OFF": "🔴 ریسک‌گریز", "NEUTRAL": "⚪️ خنثی"}


def fa(x):
    return str(x).translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹"))


def mood_line():
    if not state["updated"]:
        return ""
    s = f"🌐 حال بازار: {REG_FA[state['regime']]} (امتیاز {fa(format(state['score'], '+d'))})"
    if state["tension"] >= 40:
        s += f"  |  تنش ایران و آمریکا: {fa(state['tension'])}٪"
    return s


def brief():
    st = state
    if not st["updated"]:
        return None
    L = ["🧭 <b>خلاصهٔ بازار</b>", mood_line(), ""]
    cg, tf, wh, hl = st.get("cg"), st.get("tf") or {}, st.get("whales") or {}, st.get("hl") or {}
    if cg:
        L.append(f"TOTAL: {fa(format(cg['total']/1e12, '.2f'))}T ({fa(format(cg['total_24h'], '+.1f'))}٪ ۲۴س)  |  "
                 f"TOTAL2: {fa(format(cg['total2']/1e12, '.2f'))}T")
        line = f"دامیننس BTC: {fa(format(cg['btc_d'], '.1f'))}٪"
        if cg.get("usdt_d") is not None:
            line += f"  |  دامیننس USDT: {fa(format(cg['usdt_d'], '.2f'))}٪"
        L.append(line)
    names = {"spx": "S&P500", "nasdaq": "نزدک", "gold": "طلا", "oil": "نفت", "dxy": "دلار"}
    tl = [f"{names[k]} {fa(format(v['chg_1d'], '+.1f'))}٪" for k, v in tf.items()]
    if tl:
        L.append("  |  ".join(tl))
    if wh.get("n"):
        L.append(f"🐋 نهنگ‌ها (۴ ساعت): به صرافی ${fa(format(wh['to_ex']/1e6, ',.0f'))}M، "
                 f"از صرافی ${fa(format(wh['from_ex']/1e6, ',.0f'))}M، "
                 f"ضرب استیبل ${fa(format(wh['mint']/1e6, ',.0f'))}M، سوزاندن ${fa(format(wh['burn']/1e6, ',.0f'))}M")
    if hl.get("sample_iran"):
        L.append("🇮🇷🇺🇸 " + hl["sample_iran"][0][:110])
    if st.get("events"):
        e = st["events"][0]
        tehran = e["when"].astimezone(timezone(timedelta(hours=3, minutes=30)))
        L.append(f"📅 خبر مهم بعدی آمریکا: {e['title']} ساعت {fa(tehran.strftime('%H:%M'))} تهران "
                 f"(پیش‌بینی {e['forecast'] or '-'}، قبلی {e['previous'] or '-'})")
    L.append("\nاین فیلتر کمکی است، تضمین سود نیست.")
    return "\n".join(L)
