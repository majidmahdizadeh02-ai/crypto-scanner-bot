"""
Simple backtest of the scanner on MEXC futures history (same data the bot uses).

  python backtest.py --days 60 --symbols 30

Walks every closed 15m candle, uses only 1h/4h candles that had closed by then,
applies the same cooldown as the bot, and resolves each signal on the following
15m candles with strategy.resolve (fees 0.12% round trip, stop wins ties,
24h max hold). Reports v2 (strategy.py) next to v1 (the bot's old rules).

Caveats: symbols are today's most liquid ones (survivorship bias), no slippage,
and past results do not guarantee future win rate.
"""
import argparse
import bisect
import json
import time

import requests

import strategy as S

BASE = "https://contract.mexc.com/api/v1/contract"
SEC = {"Min15": 900, "Min60": 3600, "Hour4": 14400}
COOLDOWN_BARS = 8  # 120 min of 15m candles


def fetch(symbol, interval, start, end):
    step, rows, e = SEC[interval] * 1900, {}, end
    while e > start:
        s = max(start, e - step)
        for _ in range(3):
            try:
                d = requests.get(f"{BASE}/kline/{symbol}",
                                 params={"interval": interval, "start": s, "end": e}, timeout=20).json()["data"]
                break
            except Exception:
                time.sleep(2)
        else:
            return None
        for i, t in enumerate(d["time"]):
            rows[t] = (float(d["open"][i]), float(d["high"][i]), float(d["low"][i]),
                       float(d["close"][i]), float(d["vol"][i]))
        e = s
        time.sleep(0.1)
    ts = sorted(t for t in rows if t + SEC[interval] <= end)  # closed candles only
    k = {"time": ts}
    for n, key in enumerate(("open", "high", "low", "close", "vol")):
        k[key] = [rows[t][n] for t in ts]
    return k


def top_symbols(n, min_vol=5_000_000):
    t = requests.get(f"{BASE}/ticker", timeout=20).json()["data"]
    t = [x for x in t if x["symbol"].endswith("_USDT") and x["symbol"] != "USDC_USDT"
         and float(x.get("amount24") or 0) >= min_vol]
    t.sort(key=lambda x: -float(x["amount24"]))
    return [x["symbol"] for x in t[:n]]


def v1_signal_at(E, i, h1_trend):
    """The bot's original rules (bot.py before the upgrade), for comparison."""
    if i < 205 or h1_trend is None:
        return None
    c, h, l = E["c"], E["h"], E["l"]
    price, r = c[i], E["rsi"]
    vol_x = E["v"][i] / (E["vavg"][i] or 1e-12)
    hi20, lo20 = max(h[i - 20:i]), min(l[i - 20:i])
    up = price > h1_trend and E["ema_f"][i] > E["ema_s"][i]
    dn = price < h1_trend and E["ema_f"][i] < E["ema_s"][i]
    side = None
    if up and price > hi20 and vol_x >= 2 and r[i] < 75:
        side = "LONG"
    elif up and r[i - 1] < 40 <= r[i]:
        side = "LONG"
    elif dn and price < lo20 and vol_x >= 2 and r[i] > 25:
        side = "SHORT"
    elif dn and r[i - 1] > 60 >= r[i]:
        side = "SHORT"
    if not side:
        return None
    d = 1.5 * E["atr"][i]
    g = 1 if side == "LONG" else -1
    return {"side": side, "price": price, "sl": price - g * d,
            "tp1": price + g * d * 1.5, "tp2": price + g * d * 3.0}


def run(symbols, days):
    end = int(time.time())
    start = end - days * 86400
    warm = 220 * 3600  # history for EMA200 on 1h
    res = {"v1": [], "v2": []}
    for sym in symbols:
        m15 = fetch(sym, "Min15", start - 3 * 86400, end)
        h1 = fetch(sym, "Min60", start - warm, end)
        h4 = fetch(sym, "Hour4", start - 60 * 14400, end)
        if not m15 or not h1 or not h4 or len(m15["close"]) < 400:
            print(f"{sym}: not enough data, skipped")
            continue
        E, H = S.prep_entry(m15), S.prep_htf(h1, h4)
        h1_close_t = [t + 3600 for t in h1["time"]]
        h4_close_t = [t + 14400 for t in h4["time"]]
        last = {"v1": -99, "v2": -99}
        for i in range(210, len(m15["time"]) - 1):
            tclose = m15["time"][i] + 900
            if tclose < start:
                continue
            j1 = bisect.bisect_right(h1_close_t, tclose) - 1
            j4 = bisect.bisect_right(h4_close_t, tclose) - 1
            if j1 < 0 or j4 < 0:
                continue
            sigs = {
                "v2": S.signal_at(E, i, S.htf_bias(H, j1, j4)),
                "v1": v1_signal_at(E, i, H["h1_e200"][j1] if j1 >= 200 else None),
            }
            for ver, sig in sigs.items():
                if sig and i - last[ver] >= COOLDOWN_BARS:
                    last[ver] = i
                    out, r, bars = S.resolve(sig, E["h"][i + 1:], E["l"][i + 1:], E["c"][i + 1:])
                    if out != "OPEN":
                        res[ver].append({"symbol": sym, "t": tclose, "side": sig["side"],
                                         "result": out, "R": round(r, 3), "bars": bars})
        print(f"{sym}: v1 {sum(x['symbol'] == sym for x in res['v1'])} / "
              f"v2 {sum(x['symbol'] == sym for x in res['v2'])} trades")
    return res


def summary(trades):
    n = len(trades)
    if not n:
        return {"trades": 0}
    wins = [t for t in trades if t["R"] > 0]
    losses = [t for t in trades if t["R"] <= 0]
    gw, gl = sum(t["R"] for t in wins), -sum(t["R"] for t in losses)
    return {
        "trades": n,
        "win_rate_%": round(100 * len(wins) / n, 1),
        "tp1_hit_%": round(100 * sum(t["result"] in ("TP1", "TP2") for t in trades) / n, 1),
        "avg_win_R": round(gw / len(wins), 2) if wins else 0,
        "avg_loss_R": round(-gl / len(losses), 2) if losses else 0,
        "expectancy_R": round((gw - gl) / n, 3),
        "profit_factor": round(gw / gl, 2) if gl else None,
        "total_R": round(gw - gl, 1),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--symbols", type=int, default=30)
    a = ap.parse_args()
    syms = top_symbols(a.symbols)
    res = run(syms, a.days)
    report = {v: summary(t) for v, t in res.items()}
    print(json.dumps(report, indent=2))
    with open("backtest_trades.json", "w") as f:
        json.dump({"days": a.days, "symbols": syms, "summary": report, "trades": res}, f)
