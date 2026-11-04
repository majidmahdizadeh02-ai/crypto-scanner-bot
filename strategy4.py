"""
v4 setups: price action + Bollinger Bands, chosen by a 60-day backtest on 30 MEXC
futures pairs (40 days to pick, last 20 days unseen to check). Both setups need
the v2 1h/4h trend filter and an ATR between 0.3% and 5%.

  1. Liquidity sweep: in the last 3 candles a wick took the last 15m swing low
     (shorts: high) and price closed back above it, with an engulfing / pin /
     strong candle; 15m AND 1h structure agree (HH+HL, shorts LH+LL);
     volume >= 1.2x the 20-candle average.
  2. Bollinger squeeze breakout: band width was in its lowest 20% of the last
     100 candles and the candle closes outside the band in the trend direction,
     with an engulfing or pin candle and 15m structure agreeing.

Exit (the plan that tested best): stop 2 x ATR, TP1 = 1R (close half, stop to
entry), TP2 = 2R.

Backtest, fees 0.12% round trip: 214 trades, 63% wins, +0.20R per trade,
profit factor 1.5. Most of the edge came from shorts in a falling market;
longs were about break-even. Ichimoku and S/R-room filters did not survive the
unseen 20 days, so they are not used for entries.
"""
import strategy as S
import pa as P

SL_ATR, TP1_R, TP2_R = 2.0, 1.0, 2.0
VOL_SWEEP = 1.2
NAMES = {"sweep": "شکار نقدینگی + برگشت", "bb_break": "شکست فشردگی بولینگر"}


def setup_at(m15, h1, h4):
    """Evaluate the last closed 15m candle. Returns a signal dict or None."""
    E, H = S.prep_entry(m15), S.prep_htf(h1, h4)
    i, j1, j4 = len(m15["close"]) - 1, len(h1["close"]) - 1, len(h4["close"]) - 1
    bias = S.htf_bias(H, j1, j4)
    if bias == 0 or i < 210:
        return None
    side = "LONG" if bias == 1 else "SHORT"
    o, hi, lo, c = m15["open"], m15["high"], m15["low"], m15["close"]
    price, a = c[i], E["atr"][i]
    atr_pct = a / price * 100
    if not (S.MIN_ATR_PCT <= atr_pct <= S.MAX_ATR_PCT):
        return None
    p15, p1 = P.PA(m15), P.PA(h1)
    pats = P.candle(o, hi, lo, c, i, side)
    st15, st1h = p15.structure(i) * bias, p1.structure(j1) * bias
    vol_x = E["v"][i] / (E["vavg"][i] or 1e-12)
    setup = None
    if pats and P.sweep(p15, i, side) and st15 == 1 and st1h == 1 and vol_x >= VOL_SWEEP:
        setup = "sweep"
    else:
        _, bbu, bbl, bbw = P.bollinger(c)
        brk = c[i] > bbu[i] if bias == 1 else c[i] < bbl[i]
        if brk and P.squeeze(bbw, i) and st15 == 1 and ({"engulf", "pin"} & pats):
            setup = "bb_break"
    if not setup:
        return None
    d = SL_ATR * a
    g = bias
    return {"side": side, "price": price, "sl": price - g * d,
            "tp1": price + g * d * TP1_R, "tp2": price + g * d * TP2_R,
            "stop_pct": d / price * 100, "rsi": E["rsi"][i], "vol_x": vol_x,
            "atr_pct": atr_pct, "setup": setup, "pats": sorted(pats),
            "st1h": st1h, "candle_time": m15["time"][i]}
