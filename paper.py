"""
Paper trading account: every signal the bot sends is "taken" on a virtual
account (start 1000 USDT, RISK_PERCENT of current equity risked per trade,
fees already inside the R result). No real orders, ever.

Render's free disk is wiped on every deploy/restart, so the account state and
the open signals are kept in one pinned Telegram message ("PAPER_STATE {json}")
that the bot edits after every change and reads back on start-up via getChat.
"""
import json
import logging
import os
import time

import requests

log = logging.getLogger("paper")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
START_BAL = float(os.getenv("PAPER_START_BALANCE", "1000"))
RISK_PCT = float(os.getenv("RISK_PERCENT", "1"))
TAG = "PAPER_STATE "
API = f"https://api.telegram.org/bot{TOKEN}"

state = {"start_ts": 0, "start_bal": START_BAL, "bal": START_BAL, "peak": START_BAL,
         "max_dd": 0.0, "n": 0, "w": 0, "l": 0, "R": 0.0, "long": [0, 0.0], "short": [0, 0.0],
         "setups": {}, "open": [], "msg_id": None}

OPEN_KEYS = ("id", "symbol", "side", "price", "sl", "tp1", "tp2", "ts", "setup", "risk_usd")


def _tg(method, **payload):
    if not TOKEN or not CHAT_ID:
        return None
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=15)
        j = r.json()
        if not j.get("ok"):
            log.warning("telegram %s failed: %s", method, str(j)[:200])
            return None
        return j["result"]
    except Exception as e:
        log.warning("telegram %s failed: %s", method, e)
        return None


def load():
    """Restore state from the pinned message (if any). Returns the open signals."""
    chat = _tg("getChat", chat_id=CHAT_ID)
    pm = (chat or {}).get("pinned_message") or {}
    text = pm.get("text", "")
    if text.startswith(TAG):
        try:
            saved = json.loads(text[len(TAG):])
            state.update(saved)
            state["msg_id"] = pm.get("message_id")
            log.info("paper state restored: bal %.2f, %d trades, %d open",
                     state["bal"], state["n"], len(state["open"]))
        except ValueError as e:
            log.warning("paper state unreadable: %s", e)
    if not state["start_ts"]:
        state["start_ts"] = int(time.time())
        save()
    return list(state["open"])


def save():
    body = TAG + json.dumps({k: v for k, v in state.items() if k != "msg_id"},
                            separators=(",", ":"), ensure_ascii=False)
    if len(body) > 4000:  # Telegram limit 4096: drop the oldest open trades from storage
        log.warning("paper state too long (%d), trimming open list", len(body))
        state["open"] = state["open"][-15:]
        body = TAG + json.dumps({k: v for k, v in state.items() if k != "msg_id"},
                                separators=(",", ":"), ensure_ascii=False)
    if state["msg_id"]:
        if _tg("editMessageText", chat_id=CHAT_ID, message_id=state["msg_id"], text=body) is not None:
            return
    m = _tg("sendMessage", chat_id=CHAT_ID, text=body, disable_notification=True)
    if m:
        state["msg_id"] = m["message_id"]
        _tg("pinChatMessage", chat_id=CHAT_ID, message_id=m["message_id"], disable_notification=True)


def open_trade(sig):
    risk_usd = round(state["bal"] * RISK_PCT / 100, 2)
    rec = {k: sig.get(k) for k in OPEN_KEYS if k != "risk_usd"}
    rec["risk_usd"] = risk_usd
    state["open"].append(rec)
    save()
    return risk_usd


def close_trade(sig_id, R):
    rec = next((o for o in state["open"] if o["id"] == sig_id), None)
    if not rec:
        return None
    state["open"] = [o for o in state["open"] if o["id"] != sig_id]
    pnl = round(R * rec["risk_usd"], 2)
    state["bal"] = round(state["bal"] + pnl, 2)
    state["peak"] = max(state["peak"], state["bal"])
    dd = (state["peak"] - state["bal"]) / state["peak"] * 100
    state["max_dd"] = round(max(state["max_dd"], dd), 2)
    state["n"] += 1
    state["w" if R > 0 else "l"] += 1
    state["R"] = round(state["R"] + R, 3)
    side = "long" if rec["side"] == "LONG" else "short"
    state[side] = [state[side][0] + 1, round(state[side][1] + R, 3)]
    st = state["setups"].setdefault(rec.get("setup") or "v2", [0, 0, 0.0])
    st[0] += 1
    st[1] += 1 if R > 0 else 0
    st[2] = round(st[2] + R, 3)
    save()
    return pnl


def summary_fa(fa):
    s = state
    wr = 100 * s["w"] / s["n"] if s["n"] else 0
    ret = (s["bal"] - s["start_bal"]) / s["start_bal"] * 100
    days = (time.time() - s["start_ts"]) / 86400 if s["start_ts"] else 0
    return (f"💼 حساب آزمایشی (Paper): {fa(format(s['bal'], ',.2f'))} دلار "
            f"({fa(format(ret, '+.1f'))}% در {fa(format(days, '.0f'))} روز)\n"
            f"معاملات: {fa(s['n'])}  |  وین‌ریت {fa(format(wr, '.0f'))}%  |  "
            f"جمع {fa(format(s['R'], '+.2f'))}R  |  بیشترین افت {fa(format(s['max_dd'], '.1f'))}%  |  "
            f"باز: {fa(len(s['open']))}")
