"""
Signal logic shared by the live bot (bot.py) and the backtest (backtest.py),
so the backtest tests exactly what the bot sends.

v2 strategy (15m entries, 1h + 4h trend filter):
  Trend (must pass):  1h close > EMA200(1h) and EMA50(1h) > EMA200(1h)
                      and 4h close > EMA50(4h)            (mirror for shorts)
  Entry confluence on the closed 15m candle, all required:
    - EMA20 > EMA50 and close > EMA20
    - MACD(12,26,9) histogram crossed above 0 within the last 3 candles and is rising
    - RSI(14) between 50 and 70  (shorts: 30..50), so momentum but not exhausted
    - volume >= 1.5x the 20-candle average
    - ATR(14) between 0.3% and 5% of price (skip dead or crazy markets)
  Stop = 1.5 x ATR, TP1 = 1.5R (close half, stop to entry), TP2 = 3R.
"""

EMA_FAST, EMA_SLOW, EMA_TREND = 20, 50, 200
RSI_LEN, ATR_LEN = 14, 14
VOL_MULT = 1.5
SL_ATR = 1.5
TP1_R, TP2_R = 1.5, 3.0
MIN_ATR_PCT, MAX_ATR_PCT = 0.3, 5.0
MACD_LOOKBACK = 3


# ---------------- indicators (full series) ----------------
def ema(values, length):
    k = 2 / (length + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def rsi(closes, length=14):
    out = [50.0] * len(closes)
    if len(closes) <= length:
        return out
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag = sum(gains[:length]) / length
    al = sum(losses[:length]) / length
    for i in range(length, len(closes)):
        if i > length:
            ag = (ag * (length - 1) + gains[i - 1]) / length
            al = (al * (length - 1) + losses[i - 1]) / length
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(highs, lows, closes, length=14):
    trs = [highs[0] - lows[0]] + [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, len(closes))
    ]
    out = [trs[0]] * len(trs)
    if len(trs) < length:
        return out
    val = sum(trs[:length]) / length
    out[length - 1] = val
    for i in range(length, len(trs)):
        val = (val * (length - 1) + trs[i]) / length
        out[i] = val
    return out


def macd_hist(closes, fast=12, slow=26, sig=9):
    f, s = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(f, s)]
    signal = ema(line, sig)
    return [a - b for a, b in zip(line, signal)]


def sma(values, length):
    out, acc = [], 0.0
    for i, v in enumerate(values):
        acc += v
        if i >= length:
            acc -= values[i - length]
        out.append(acc / min(i + 1, length))
    return out


# ---------------- precompute ----------------
def prep_entry(k):
    """k: dict of lists open/high/low/close/vol for closed 15m candles."""
    c, h, l, v = k["close"], k["high"], k["low"], k["vol"]
    return {
        "c": c, "h": h, "l": l, "v": v,
        "ema_f": ema(c, EMA_FAST), "ema_s": ema(c, EMA_SLOW),
        "rsi": rsi(c, RSI_LEN), "atr": atr(h, l, c, ATR_LEN),
        "hist": macd_hist(c),
        "vavg": [0.0] + sma(v, 20)[:-1],  # average of the 20 candles BEFORE i
    }


def prep_htf(h1, h4):
    return {
        "h1c": h1["close"], "h1_e50": ema(h1["close"], 50), "h1_e200": ema(h1["close"], EMA_TREND),
        "h4c": h4["close"], "h4_e50": ema(h4["close"], 50),
    }


def htf_bias(H, j1, j4):
    """j1/j4: index of the last CLOSED 1h / 4h candle. Returns 1, -1 or 0."""
    if j1 < EMA_TREND or j4 < 50:
        return 0
    c1, e50, e200 = H["h1c"][j1], H["h1_e50"][j1], H["h1_e200"][j1]
    c4, e4 = H["h4c"][j4], H["h4_e50"][j4]
    if c1 > e200 and e50 > e200 and c4 > e4:
        return 1
    if c1 < e200 and e50 < e200 and c4 < e4:
        return -1
    return 0


def signal_at(E, i, bias):
    """Evaluate the closed 15m candle i. Returns a signal dict or None."""
    if i < EMA_TREND or bias == 0:
        return None
    c, price = E["c"], E["c"][i]
    a = E["atr"][i]
    atr_pct = a / price * 100
    if not (MIN_ATR_PCT <= atr_pct <= MAX_ATR_PCT):
        return None
    hist = E["hist"]
    vol_x = E["v"][i] / (E["vavg"][i] or 1e-12)
    r = E["rsi"][i]
    recent = range(i - MACD_LOOKBACK + 1, i + 1)

    if bias == 1:
        ok = (E["ema_f"][i] > E["ema_s"][i] and price > E["ema_f"][i]
              and any(hist[k - 1] <= 0 < hist[k] for k in recent) and hist[i] > hist[i - 1]
              and 50 <= r <= 70 and vol_x >= VOL_MULT)
        side = "LONG"
    else:
        ok = (E["ema_f"][i] < E["ema_s"][i] and price < E["ema_f"][i]
              and any(hist[k - 1] >= 0 > hist[k] for k in recent) and hist[i] < hist[i - 1]
              and 30 <= r <= 50 and vol_x >= VOL_MULT)
        side = "SHORT"
    if not ok:
        return None

    d = SL_ATR * a
    sgn = 1 if side == "LONG" else -1
    return {
        "side": side, "price": price,
        "sl": price - sgn * d, "tp1": price + sgn * d * TP1_R, "tp2": price + sgn * d * TP2_R,
        "stop_pct": d / price * 100, "rsi": r, "vol_x": vol_x, "atr_pct": atr_pct,
    }


# ---------------- outcome of a signal on later candles ----------------
def resolve(sig, highs, lows, closes, fee_pct=0.12, max_bars=96):
    """Walk later candles (lists start at the first candle AFTER entry).
    Plan: half off at TP1 and stop to entry, rest at TP2. A candle touching both
    stop and target counts as stop (conservative). Unresolved after max_bars:
    closed at that candle's close. Returns (result, R after fees, bars)."""
    side, e, sl, tp1, tp2 = sig["side"], sig["price"], sig["sl"], sig["tp1"], sig["tp2"]
    risk = abs(e - sl)
    TP1_R, TP2_R = abs(tp1 - e) / risk, abs(tp2 - e) / risk  # read targets from the signal
    fee_r = (fee_pct / 100) * e / risk
    long_ = side == "LONG"
    half = False
    for n, (h, l) in enumerate(zip(highs, lows)):
        if n >= max_bars:
            break
        hit_sl = l <= (e if half else sl) if long_ else h >= (e if half else sl)
        hit1 = h >= tp1 if long_ else l <= tp1
        hit2 = h >= tp2 if long_ else l <= tp2
        if not half:
            if hit_sl:
                return "SL", -1 - fee_r, n + 1
            if hit1:
                half = True
                if hit2:
                    return "TP2", 0.5 * TP1_R + 0.5 * TP2_R - fee_r, n + 1
                continue
        else:
            if hit_sl:
                return "TP1", 0.5 * TP1_R - fee_r, n + 1
            if hit2:
                return "TP2", 0.5 * TP1_R + 0.5 * TP2_R - fee_r, n + 1
    n = min(len(closes), max_bars)
    if n == 0:
        return "OPEN", 0.0, 0
    last = closes[n - 1]
    move_r = (last - e) / risk if long_ else (e - last) / risk
    if half:
        return "TP1", 0.5 * TP1_R + 0.5 * move_r - fee_r, n
    return ("TIME" if len(closes) >= max_bars else "OPEN"), move_r - fee_r, n
