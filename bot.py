
import os
import json
import asyncio
import urllib.request
import urllib.parse
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

TOKEN = os.environ["BOT_TOKEN"]

TIMEFRAME = "1h"
MAX_SYMBOLS = 100
RISK_PERCENT = 1.0
DEFAULT_CAPITAL = 1000.0

SPOT_URL = "https://api.binance.com"
FUTURES_URL = "https://fapi.binance.com"


def get_json(url):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "CryptoScannerBot/1.0"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


async def fetch_json(url):
    return await asyncio.to_thread(get_json, url)


async def get_top_symbols(market):
    base = SPOT_URL if market == "spot" else FUTURES_URL
    endpoint = "/api/v3/ticker/24hr" if market == "spot" else "/fapi/v1/ticker/24hr"
    data = await fetch_json(base + endpoint)

    symbols = [
        item for item in data
        if item["symbol"].endswith("USDT")
        and not any(x in item["symbol"] for x in ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"))
    ]

    symbols.sort(
        key=lambda x: float(x.get("quoteVolume", 0)),
        reverse=True,
    )
    return [item["symbol"] for item in symbols[:MAX_SYMBOLS]]


async def get_candles(symbol, market):
    if market == "spot":
        url = SPOT_URL + "/api/v3/klines"
    else:
        url = FUTURES_URL + "/fapi/v1/klines"

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": TIMEFRAME,
        "limit": 100,
    })
    data = await fetch_json(url + "?" + params)

    # Ignore the currently forming candle.
    data = data[:-1]

    return [
        {
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "volume": float(row[5]),
        }
        for row in data
    ]


def midpoint(candles, period):
    section = candles[-period:]
    return (
        max(c["high"] for c in section)
        + min(c["low"] for c in section)
    ) / 2


def analyze(candles, market):
    if len(candles) < 60:
        return None

    close = candles[-1]["close"]
    previous = candles[-2]
    tenkan = midpoint(candles, 9)
    kijun = midpoint(candles, 26)
    span_a = (tenkan + kijun) / 2
    span_b = midpoint(candles, 52)

    cloud_top = max(span_a, span_b)
    cloud_bottom = min(span_a, span_b)

    avg_volume = sum(c["volume"] for c in candles[-21:-1]) / 20
    volume_ok = candles[-1]["volume"] > avg_volume

    recent = candles[-15:-1]
    support = min(c["low"] for c in recent)
    resistance = max(c["high"] for c in recent)

    true_ranges = []
    for i in range(-14, 0):
        candle = candles[i]
        prior_close = candles[i - 1]["close"]
        true_ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - prior_close),
            abs(candle["low"] - prior_close),
        ))
    atr = sum(true_ranges) / len(true_ranges)

    long_score = 0
    short_score = 0

    if close > cloud_top:
        long_score += 1
    if close < cloud_bottom:
        short_score += 1
    if tenkan > kijun:
        long_score += 1
    if tenkan < kijun:
        short_score += 1
    if close > previous["high"]:
        long_score += 1
    if close < previous["low"]:
        short_score += 1
    if volume_ok and close > candles[-2]["close"]:
        long_score += 1
    if volume_ok and close < candles[-2]["close"]:
        short_score += 1

    if long_score >= 3 and long_score > short_score:
        side = "LONG"
        entry = close
        stop = min(entry - 1.5 * atr, support)
        if stop >= entry:
            return None
        risk = entry - stop
        targets = [entry + risk, entry + 2 * risk, entry + 3 * risk]
        score = long_score
    elif market == "futures" and short_score >= 3 and short_score > long_score:
        side = "SHORT"
        entry = close
        stop = max(entry + 1.5 * atr, resistance)
        if stop <= entry:
            return None
        risk = stop - entry
        targets = [entry - risk, entry - 2 * risk, entry - 3 * risk]
        score = short_score
    else:
        return None

    return {
        "side": side,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "score": score,
    }


def format_signal(symbol, market, signal, capital):
    risk_money = capital * RISK_PERCENT / 100
    distance = abs(signal["entry"] - signal["stop"])
    quantity = risk_money / distance if distance > 0 else 0

    return (
        f"{'SPOT' if market == 'spot' else 'FUTURES'} | {symbol}\n"
        f"جهت: {signal['side']}\n"
        f"ورود مرجع: {signal['entry']:.8g}\n"
        f"حد ضرر: {signal['stop']:.8g}\n"
        f"TP1: {signal['targets'][0]:.8g}\n"
        f"TP2: {signal['targets'][1]:.8g}\n"
        f"TP3: {signal['targets'][2]:.8g}\n"
        f"امتیاز اولیه: {signal['score']}/4\n"
        f"ریسک هدف: {RISK_PERCENT:.1f}% = {risk_money:.2f} USDT\n"
        f"اندازه تقریبی موقعیت: {quantity:.8g} واحد\n"
        f"توجه: سیگنال آزمایشی؛ کارمزد، لغزش قیمت و اهرم لحاظ نشده‌اند."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ربات اسکنر راه‌اندازی شده است ✅\n\n"
        "فرمان‌ها:\n"
        "/scan - اسکن بازار اسپات و فیوچرز\n"
        "/capital 1000 - تعیین سرمایه مبنا به USDT"
    )


async def capital(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "مثال: /capital 1000"
        )
        return

    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
        context.user_data["capital"] = amount
        await update.message.reply_text(
            f"سرمایه مبنا تنظیم شد: {amount:.2f} USDT"
        )
    except ValueError:
        await update.message.reply_text(
            "لطفاً مبلغ مثبت وارد کن؛ مثال: /capital 1000"
        )


async def scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = await update.message.reply_text(
        "اسکن آغاز شد؛ لطفاً صبر کن. ⏳"
    )
    capital_amount = context.user_data.get(
        "capital", DEFAULT_CAPITAL
    )
    found = []

    for market in ("spot", "futures"):
        try:
            symbols = await get_top_symbols(market)
        except Exception as exc:
            found.append(f"خطا در دریافت فهرست {market}: {exc}")
            continue

        semaphore = asyncio.Semaphore(8)

        async def inspect(symbol):
            async with semaphore:
                try:
                    candles = await get_candles(symbol, market)
                    signal = analyze(candles, market)
                    if signal:
                        return format_signal(
                            symbol, market, signal, capital_amount
                        )
                except Exception:
                    return None
                return None

        results = await asyncio.gather(
            *(inspect(symbol) for symbol in symbols)
        )
        found.extend(item for item in results if item)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not found:
        report = (
            f"اسکن پایان یافت؛ سیگنال واجد شرایط پیدا نشد.\n"
            f"تعداد نمادهای هدف: تا {MAX_SYMBOLS} در هر بازار\n"
            f"تایم‌فریم: {TIMEFRAME}\nزمان: {timestamp}"
        )
    else:
        report = (
            f"گزارش اسکن آزمایشی\n"
            f"تایم‌فریم: {TIMEFRAME}\nزمان: {timestamp}\n\n"
            + "\n\n".join(found)
        )

    for start_index in range(0, len(report), 3500):
        await update.message.reply_text(report[start_index:start_index + 3500])

    await message.delete()


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scan", scan))
    app.add_handler(CommandHandler("capital", capital))
    app.run_polling()


if __name__ == "__main__":
    main()
