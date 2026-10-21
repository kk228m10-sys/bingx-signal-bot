"""BingX public market data and indicators. Read-only, no API key."""
import json
import time
import urllib.parse
import urllib.request

BASE = "https://open-api.bingx.com"


def get(path, **params):
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}{path}?{query}" if query else f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "bingx-signal-bot/1.0"})
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if isinstance(body, dict) and body.get("code") not in (0, "0", None):
                raise RuntimeError(f"BingX error {body.get('code')}: {body.get('msg')}")
            return body.get("data") if isinstance(body, dict) else body
        except Exception as e:
            last_err = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"GET {path} failed: {last_err}")


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def norm_symbol(s):
    s = s.upper().replace("/", "-").replace("_", "-")
    if "-" not in s:
        for q in ("USDT", "USDC"):
            if s.endswith(q) and len(s) > len(q):
                return s[: -len(q)] + "-" + q
        return s + "-USDT"
    return s


# --------------------------------------------------------------------------- data

def klines(symbol, interval, limit=500, start_ms=None):
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    if start_ms:
        params["startTime"] = int(start_ms)
    data = None
    for path in ("/openApi/swap/v3/quote/klines", "/openApi/swap/v2/quote/klines"):
        try:
            data = get(path, **params)
            if data:
                break
        except RuntimeError:
            continue
    if not data:
        raise RuntimeError(f"no klines for {symbol} {interval}")
    rows = []
    for k in data:
        if isinstance(k, dict):
            rows.append({"t": int(k.get("time") or k.get("openTime")),
                         "o": fnum(k["open"]), "h": fnum(k["high"]),
                         "l": fnum(k["low"]), "c": fnum(k["close"]),
                         "v": fnum(k.get("volume")) or 0.0})
        else:
            rows.append({"t": int(k[0]), "o": fnum(k[1]), "h": fnum(k[2]),
                         "l": fnum(k[3]), "c": fnum(k[4]), "v": fnum(k[5]) or 0.0})
    rows.sort(key=lambda r: r["t"])
    return rows


def premium(symbol):
    d = get("/openApi/swap/v2/quote/premiumIndex", symbol=symbol)
    if isinstance(d, list):
        d = d[0] if d else {}
    return {"mark": fnum(d.get("markPrice")), "funding": fnum(d.get("lastFundingRate"))}


def open_interest(symbol):
    try:
        d = get("/openApi/swap/v2/quote/openInterest", symbol=symbol)
        if isinstance(d, list):
            d = d[0] if d else {}
        return fnum(d.get("openInterest"))
    except RuntimeError:
        return None


_contracts = {"t": 0, "data": {}}


def contract_info(symbol):
    if time.time() - _contracts["t"] > 6 * 3600 or not _contracts["data"]:
        try:
            data = get("/openApi/swap/v2/quote/contracts") or []
        except RuntimeError:
            data = []
        if data:
            parsed = {}
            for c in data:
                prec = c.get("quantityPrecision")
                pprec = c.get("pricePrecision")
                parsed[c.get("symbol")] = {
                    "min_qty": fnum(c.get("tradeMinQuantity")) or 0.0,
                    "qty_step": 10 ** -int(prec) if prec is not None else None,
                    "price_decimals": int(pprec) if pprec is not None else None,
                    "taker_fee": fnum(c.get("takerFeeRate")) or 0.0005,
                    "maker_fee": fnum(c.get("makerFeeRate")) or 0.0002,
                }
            _contracts.update(t=time.time(), data=parsed)
    return _contracts["data"].get(symbol)


STABLE = {"USDC", "FDUSD", "TUSD", "DAI", "USDE", "USD1", "PYUSD", "BUSD", "USDP", "EUR", "XAUT", "PAXG"}


def top_symbols(n=30, exclude=()):
    """Most liquid crypto USDT perpetuals by 24h quote volume (no stables, tokenized stocks/commodities)."""
    rows = []
    for t in get("/openApi/swap/v2/quote/ticker") or []:
        sym = t.get("symbol", "")
        base = sym.split("-")[0]
        if (not sym.endswith("-USDT") or sym.startswith("NCCO") or base in STABLE
                or sym in exclude or not base.isalnum()):
            continue
        rows.append((fnum(t.get("quoteVolume")) or 0.0, sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:n]]


def symbol_exists(symbol):
    return contract_info(symbol) is not None or bool(_contracts["data"]) is False


# --------------------------------------------------------------------------- indicators

def ema(vals, n):
    out, k, e = [], 2 / (n + 1), None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, n=14):
    if len(closes) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def atr(rows, n=14):
    if len(rows) <= n:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def macd_hist(closes):
    m = [a - b for a, b in zip(ema(closes, 12), ema(closes, 26))]
    sig = ema(m, 9)
    return m[-1] - sig[-1]


def swings(rows, k=3):
    highs, lows = [], []
    for i in range(k, len(rows) - k):
        win = rows[i - k:i + k + 1]
        if rows[i]["h"] == max(r["h"] for r in win):
            highs.append(rows[i]["h"])
        if rows[i]["l"] == min(r["l"] for r in win):
            lows.append(rows[i]["l"])
    return highs, lows


def cluster_levels(points, tol):
    """Merge nearby swing prices into levels: list of (price, touches)."""
    levels = []
    for p in sorted(points):
        if levels and abs(p - levels[-1][0]) <= tol:
            price, n = levels[-1]
            levels[-1] = ((price * n + p) / (n + 1), n + 1)
        else:
            levels.append((p, 1))
    return levels


def structure(rows, k=3):
    highs, lows = swings(rows, k)
    if len(highs) < 2 or len(lows) < 2:
        return 0
    hh, hl = highs[-1] > highs[-2], lows[-1] > lows[-2]
    return 1 if hh and hl else -1 if not hh and not hl else 0


def trend_score(rows):
    """-5..+5 from EMA stack and MACD on closed candles."""
    closes = [r["c"] for r in rows]
    c = closes[-1]
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    s = (1 if c > e20 else -1) + (1 if c > e50 else -1) + (1 if e20 > e50 else -1)
    if len(closes) >= 200:
        s += 1 if c > ema(closes, 200)[-1] else -1
    s += 1 if macd_hist(closes) > 0 else -1
    return s


def closed(rows):
    """Drop the still-forming last candle."""
    return rows[:-1]
