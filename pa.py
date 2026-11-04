"""
Price action layer (v4). Pure functions on candle lists, shared by bot and backtest.

Concepts (all computed only from CLOSED candles up to index i, no look-ahead):
  - Swing pivots: a high/low that is the extreme of PIVOT bars on each side.
    A pivot at bar p is only "known" at bar p + PIVOT.
  - Market structure: last two swing highs / lows -> HH+HL = up, LH+LL = down.
  - S/R levels: swing highs/lows of the 1h chart; "room" = distance from entry
    to the nearest opposing level, in R.
  - Candle patterns on the signal candle: engulfing, pin bar (rejection wick),
    strong close (body closes in the outer 1/3 of the range).
  - Break of structure (BOS): close beyond the last known 15m swing high/low.
  - Liquidity sweep: wick beyond the last swing low/high, close back inside.
"""

PIVOT = 3


def pivots(highs, lows, k=PIVOT):
    """Return (sh, sl): lists of (bar_index, price) for swing highs / lows."""
    sh, sl = [], []
    n = len(highs)
    for p in range(k, n - k):
        h, l = highs[p], lows[p]
        if all(h >= highs[p - j] for j in range(1, k + 1)) and all(h > highs[p + j] for j in range(1, k + 1)):
            sh.append((p, h))
        if all(l <= lows[p - j] for j in range(1, k + 1)) and all(l < lows[p + j] for j in range(1, k + 1)):
            sl.append((p, l))
    return sh, sl


def known(piv, i, k=PIVOT):
    """Pivots already confirmed when bar i closes."""
    out = []
    for p, v in piv:
        if p + k <= i:
            out.append((p, v))
        else:
            break
    return out


class PA:
    """Precomputed pivots for one timeframe; query with bar index."""

    def __init__(self, k):
        self.h, self.l, self.o, self.c = k["high"], k["low"], k["open"], k["close"]
        self.sh, self.sl = pivots(self.h, self.l)
        self._shp = [p for p, _ in self.sh]
        self._slp = [p for p, _ in self.sl]

    def _upto(self, piv, pos, i):
        import bisect
        j = bisect.bisect_right(pos, i - PIVOT)
        return piv[:j]

    def highs_upto(self, i):
        return self._upto(self.sh, self._shp, i)

    def lows_upto(self, i):
        return self._upto(self.sl, self._slp, i)

    def structure(self, i):
        """1 = HH+HL, -1 = LH+LL, 0 = mixed / not enough swings."""
        hs, ls = self.highs_upto(i), self.lows_upto(i)
        if len(hs) < 2 or len(ls) < 2:
            return 0
        hh = hs[-1][1] > hs[-2][1]
        hl = ls[-1][1] > ls[-2][1]
        if hh and hl:
            return 1
        if not hh and not hl:
            return -1
        return 0

    def room_r(self, i, side, entry, risk, lookback=120):
        """Distance to the nearest opposing swing level (in R) within lookback bars.
        None if no level in the way (open air)."""
        if side == "LONG":
            lv = [v for p, v in self.highs_upto(i) if p >= i - lookback and v > entry]
            return (min(lv) - entry) / risk if lv else None
        lv = [v for p, v in self.lows_upto(i) if p >= i - lookback and v < entry]
        return (entry - max(lv)) / risk if lv else None


# ---------------- candle patterns on bar i ----------------
def candle(o, h, l, c, i, side):
    """Returns set of pattern names confirming `side` on bar i."""
    rng = h[i] - l[i]
    if rng <= 0:
        return set()
    body = abs(c[i] - o[i])
    up_w = h[i] - max(o[i], c[i])
    dn_w = min(o[i], c[i]) - l[i]
    pos = (c[i] - l[i]) / rng  # 0 = closed at low, 1 = closed at high
    out = set()
    if side == "LONG":
        if c[i] > o[i] and c[i - 1] < o[i - 1] and c[i] >= o[i - 1] and o[i] <= c[i - 1]:
            out.add("engulf")
        if dn_w >= 2 * max(body, rng * 0.05) and pos >= 0.6:
            out.add("pin")
        if c[i] > o[i] and pos >= 0.67 and body >= 0.5 * rng:
            out.add("strong")
    else:
        if c[i] < o[i] and c[i - 1] > o[i - 1] and c[i] <= o[i - 1] and o[i] >= c[i - 1]:
            out.add("engulf")
        if up_w >= 2 * max(body, rng * 0.05) and pos <= 0.4:
            out.add("pin")
        if c[i] < o[i] and pos <= 0.33 and body >= 0.5 * rng:
            out.add("strong")
    return out


def bos(P, i, side, lookback=40):
    """Close of bar i broke the last known swing high (long) / low (short)."""
    if side == "LONG":
        hs = [v for p, v in P.highs_upto(i - 1) if p >= i - lookback]
        return bool(hs) and P.c[i] > hs[-1]
    ls = [v for p, v in P.lows_upto(i - 1) if p >= i - lookback]
    return bool(ls) and P.c[i] < ls[-1]


def sweep(P, i, side, lookback=40, within=3):
    """In the last `within` bars a wick took the last swing low (long) / high (short)
    and price closed back beyond it."""
    if side == "LONG":
        ls = [v for p, v in P.lows_upto(i - within) if p >= i - lookback]
        if not ls:
            return False
        lv = ls[-1]
        return any(P.l[j] < lv for j in range(i - within + 1, i + 1)) and P.c[i] > lv
    hs = [v for p, v in P.highs_upto(i - within) if p >= i - lookback]
    if not hs:
        return False
    hv = hs[-1]
    return any(P.h[j] > hv for j in range(i - within + 1, i + 1)) and P.c[i] < hv


def swing_stop(P, i, side, lookback=30):
    """Last known swing low (long) / high (short) below/above price, or None."""
    if side == "LONG":
        ls = [v for p, v in P.lows_upto(i) if p >= i - lookback and v < P.c[i]]
        return ls[-1] if ls else None
    hs = [v for p, v in P.highs_upto(i) if p >= i - lookback and v > P.c[i]]
    return hs[-1] if hs else None


LABELS = {"engulf": "کندل پوشا", "pin": "پین‌بار", "strong": "کندل قدرتمند"}


# ---------------- Bollinger Bands ----------------
def bollinger(closes, n=20, k=2.0):
    """Returns (mid, upper, lower, width_pct) lists."""
    mid, up, lo, wd = [], [], [], []
    for i in range(len(closes)):
        w = closes[max(0, i - n + 1):i + 1]
        m = sum(w) / len(w)
        sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
        mid.append(m); up.append(m + k * sd); lo.append(m - k * sd)
        wd.append(2 * k * sd / m * 100 if m else 0)
    return mid, up, lo, wd


def squeeze(wd, i, look=100, pct=0.2):
    """Band width at bar i-1 was in the lowest `pct` of the last `look` bars."""
    w = wd[max(0, i - look):i]
    if len(w) < 30:
        return False
    return sorted(w)[int(len(w) * pct)] >= wd[i - 1]


# ---------------- Ichimoku (9, 26, 52) ----------------
def ichimoku(highs, lows, closes, t=9, k=26, s=52):
    """Returns dict of lists. Cloud values are shifted forward 26 bars, so
    cloud_a[i]/cloud_b[i] is the cloud drawn under bar i (uses data up to i-26)."""
    n = len(closes)
    def mid(p, i):
        a = max(0, i - p + 1)
        return (max(highs[a:i + 1]) + min(lows[a:i + 1])) / 2
    ten = [mid(t, i) for i in range(n)]
    kij = [mid(k, i) for i in range(n)]
    sa = [(ten[i] + kij[i]) / 2 for i in range(n)]
    sb = [mid(s, i) for i in range(n)]
    ca = [sa[i - k] if i >= k else sa[0] for i in range(n)]
    cb = [sb[i - k] if i >= k else sb[0] for i in range(n)]
    return {"ten": ten, "kij": kij, "ca": ca, "cb": cb, "c": closes}


def ichi_bias(I, i, k=26):
    """1 bullish: price above cloud, tenkan > kijun, chikou (close) above close 26 bars ago,
    -1 mirror, else 0."""
    if i < 52 + k:
        return 0
    c = I["c"][i]
    top, bot = max(I["ca"][i], I["cb"][i]), min(I["ca"][i], I["cb"][i])
    if c > top and I["ten"][i] > I["kij"][i] and c > I["c"][i - k]:
        return 1
    if c < bot and I["ten"][i] < I["kij"][i] and c < I["c"][i - k]:
        return -1
    return 0
