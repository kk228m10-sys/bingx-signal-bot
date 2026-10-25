"""Setup detection, targets and position sizing.

Rules are deterministic so every signal can be checked afterwards:
  * bounce  - rejection candle at a strong level (several swing touches on 1h/4h)
  * retest  - 1h breakout of a strong level on volume, then a retest that holds
Higher timeframes (1d + 4h trend score) filter direction; funding filters crowding.
All thresholds live in PARAMS so backtest.py can test them.
"""
import market as m

FUNDING_MAX = 0.0005      # 0.05% per 8h: beyond this one side is crowded
MMR = 0.005               # maintenance margin rate (approx.)

# Chosen by `backtest.py --grid` (BTC/ETH/SOL, 2026-07-22..09-20 in-sample):
# in-sample +0.31R/trade (n=121), out-of-sample 09-20..10-20 +0.12R/trade (n=61).
# 135 of 144 tested combinations lost money; 15m triggers and counter-trend trades were the worst.
PARAMS = {
    "trigger": "1h",        # candle that must show the rejection: 15m or 1h
    "stop_atr": 0.4,        # stop buffer beyond the level / wick, in 1h ATR
    "touch_min": 3,         # swing touches for a level to count as strong
    "trend_only": True,     # longs only if htf >= trend_min, shorts only if <= -trend_min
    "trend_min": 3,
    "tp1_min_r": 1.0,       # first target at least this many R away
    "min_tp2_r": 1.5,       # TP2 must pay at least this after fees
    "kinds": ("bounce",),
    "stop_min_pct": 0.15,
    "stop_max_pct": 3.0,
    # altcoins follow BTC: "off" | "soft" (no longs if BTC htf <= -3, no shorts if >= 3)
    # | "strict" (longs need BTC htf >= 1, shorts need <= -1)
    "btc_filter": "off",
}


# Variants run side by side; every setup of every variant is tracked for learning.
# Order = priority when several fire on the same coin and side. "base" is the backtested rule set
# (+0.12R/trade out-of-sample on BTC/ETH/SOL; negative on alts, see README). The others explore.
VARIANTS = [
    ("base", {}),
    ("retest", {"kinds": ("retest",)}),
    ("range", {"trend_only": False}),
]


# --------------------------------------------------------------------------- context

def build_context(symbol):
    d1 = m.closed(m.klines(symbol, "1d", 300))
    h4 = m.closed(m.klines(symbol, "4h", 500))
    h1 = m.closed(m.klines(symbol, "1h", 500))
    m15_all = m.klines(symbol, "15m", 200)
    try:
        prem = m.premium(symbol)
    except RuntimeError:
        prem = {"mark": None, "funding": None}
    return context_from(symbol, d1, h4, h1, m.closed(m15_all), m15_all[-1]["c"],
                        prem.get("funding"), m.open_interest(symbol))


def context_from(symbol, d1, h4, h1, m15, price, funding=None, oi=None):
    """Indicators and levels from closed candles (also used by the backtest)."""
    atr1h = m.atr(h1)
    pts = []
    for rows in (h1[-300:], h4[-200:]):
        hi, lo = m.swings(rows, 3)
        pts += hi + lo
    levels = m.cluster_levels(pts, 0.35 * atr1h)
    d1_closes = [r["c"] for r in d1]
    return {
        "symbol": symbol,
        "price": price,
        "h1": h1[-30:], "m15": m15[-3:],
        "atr1h": atr1h,
        "atr15": m.atr(m15),
        "score_d1": m.trend_score(d1),
        "score_h4": m.trend_score(h4),
        "score_h1": m.trend_score(h1),
        "struct_d1": m.structure(d1),
        "d1_ema200": m.ema(d1_closes, 200)[-1] if len(d1_closes) >= 200 else None,
        "rsi1h": m.rsi([r["c"] for r in h1]),
        "rsi15": m.rsi([r["c"] for r in m15]),
        "levels": levels,
        "funding": funding,
        "oi": oi,
    }


def htf_score(ctx):
    return ctx["score_d1"] + ctx["score_h4"]


# --------------------------------------------------------------------------- setups

def _bull_rejection(b):
    rng = b["h"] - b["l"]
    return rng > 0 and b["c"] > b["o"] and (min(b["o"], b["c"]) - b["l"]) >= 0.4 * rng


def _bear_rejection(b):
    rng = b["h"] - b["l"]
    return rng > 0 and b["c"] < b["o"] and (b["h"] - max(b["o"], b["c"])) >= 0.4 * rng


def _breakout(h1, level, atr1h, side, lookback=8):
    """1h close through `level` within the last `lookback` bars, on 1.5x volume."""
    n = len(h1)
    for i in range(max(21, n - lookback), n):
        prev, bar = h1[i - 1], h1[i]
        avg = sum(r["v"] for r in h1[i - 20:i]) / 20
        vol_ok = avg > 0 and bar["v"] >= 1.5 * avg
        if side == "long" and prev["c"] < level and bar["c"] > level + 0.1 * atr1h and vol_ok:
            return True
        if side == "short" and prev["c"] > level and bar["c"] < level - 0.1 * atr1h and vol_ok:
            return True
    return False


def find_setups(ctx, p=None):
    p = {**PARAMS, **(p or {})}
    b = ctx["h1"][-1] if p["trigger"] == "1h" else ctx["m15"][-1]
    a = ctx["atr1h"]
    sb = p["stop_atr"] * a
    htf = htf_score(ctx)
    funding = ctx["funding"] or 0.0
    strong = [(lv, n) for lv, n in ctx["levels"] if n >= p["touch_min"]]
    found = []

    # news-spike filter: last 15m range far above normal
    last15 = ctx["m15"][-1]
    if ctx["atr15"] and (last15["h"] - last15["l"]) > 3.5 * ctx["atr15"]:
        return found

    tmin = p["trend_min"] if p["trend_only"] else None
    long_ok = (htf >= tmin if tmin else htf >= -2) and ctx["rsi1h"] < 68 and funding < FUNDING_MAX
    short_ok = (htf <= -tmin if tmin else htf <= 2) and ctx["rsi1h"] > 32 and funding > -FUNDING_MAX
    btc = ctx.get("btc_htf")
    if btc is not None and not ctx["symbol"].startswith("BTC-"):
        if p["btc_filter"] == "soft":
            long_ok, short_ok = long_ok and btc > -3, short_ok and btc < 3
        elif p["btc_filter"] == "strict":
            long_ok, short_ok = long_ok and btc >= 1, short_ok and btc <= -1

    for level, touches in sorted(strong, key=lambda x: abs(x[0] - b["c"])):
        if "bounce" in p["kinds"]:
            if long_ok and b["c"] > level and level - 0.6 * a <= b["l"] <= level + 0.25 * a and _bull_rejection(b):
                found.append(_make(ctx, p, "long", "bounce", level, touches, b, min(level, b["l"]) - sb))
            if short_ok and b["c"] < level and level - 0.25 * a <= b["h"] <= level + 0.6 * a and _bear_rejection(b):
                found.append(_make(ctx, p, "short", "bounce", level, touches, b, max(level, b["h"]) + sb))
        if "retest" in p["kinds"]:
            if (long_ok and b["c"] > level and b["c"] > b["o"] and b["l"] <= level + 0.25 * a
                    and _breakout(ctx["h1"], level, a, "long")):
                found.append(_make(ctx, p, "long", "retest", level, touches, b, min(level, b["l"]) - sb))
            if (short_ok and b["c"] < level and b["c"] < b["o"] and b["h"] >= level - 0.25 * a
                    and _breakout(ctx["h1"], level, a, "short")):
                found.append(_make(ctx, p, "short", "retest", level, touches, b, max(level, b["h"]) + sb))

    best = {}
    for s in found:
        if s.get("ok") and (s["side"] not in best or s["tp_r"][1] > best[s["side"]]["tp_r"][1]):
            best[s["side"]] = s
    return list(best.values())


def _confidence(side, htf, kind, touches):
    with_trend = htf >= 3 if side == "long" else htf <= -3
    against = htf <= -3 if side == "long" else htf >= 3
    if against:
        return "низкая (против старшего тренда)"
    if with_trend and (touches >= 4 or kind == "retest"):
        return "средняя-высокая"
    if with_trend:
        return "средняя"
    return "низкая-средняя"


def _make(ctx, p, side, kind, level, touches, bar, stop):
    entry = bar["c"]
    a = ctx["atr1h"]
    risk = abs(entry - stop)
    s = {"symbol": ctx["symbol"], "side": side, "kind": kind, "level": level,
         "touches": touches, "entry": entry, "stop": stop, "ok": False,
         "bar_t": bar["t"],
         "bar_close": bar["t"] + (3_600_000 if p["trigger"] == "1h" else 900_000)}
    pct = risk / entry * 100
    if not (p["stop_min_pct"] <= pct <= p["stop_max_pct"]):
        s["reason"] = f"stop {pct:.2f}% out of range"
        return s

    sign = 1 if side == "long" else -1
    # take profit slightly before the next levels (price often turns early)
    beyond = sorted((lv for lv, n in ctx["levels"] if n >= 2 and sign * (lv - entry) > 0),
                    key=lambda lv: sign * (lv - entry))
    tps = []
    for lv in beyond:
        tp = lv - sign * 0.1 * a
        dist = sign * (tp - entry)
        prev = sign * (tps[-1] - entry) if tps else 0
        need = p["tp1_min_r"] * risk if not tps else prev + 0.5 * risk
        if dist >= need:
            tps.append(tp)
        if len(tps) == 3:
            break
    while len(tps) < 3:                     # no level left: extend by R multiples
        if tps:
            r_last = sign * (tps[-1] - entry) / risk
            tps.append(entry + sign * (r_last + 1.0) * risk)
        else:
            tps.append(entry + sign * (p["tp1_min_r"] + 0.5) * risk)

    fee = 0.0005
    tp_r = [(sign * (tp - entry) - (entry + tp) * fee) / (risk + (entry + stop) * fee) for tp in tps]
    s.update(tps=tps, tp_r=tp_r, limit=level + sign * 0.1 * a)
    if tp_r[1] < p["min_tp2_r"]:
        s["reason"] = f"TP2 only {tp_r[1]:.2f}R"
        return s
    s["confidence"] = _confidence(side, htf_score(ctx), kind, touches)
    s["ok"] = True
    s["why"] = explain(ctx, s, p, bar)
    return s


def explain(ctx, s, p, bar):
    """Plain-language reasons for a setup, stored with the signal and shown in the mini app."""
    long_ = s["side"] == "long"
    a = ctx["atr1h"]
    fp = lambda x: fmt_price(x)
    htf = htf_score(ctx)
    rng = bar["h"] - bar["l"] or 1e-12
    wick = (min(bar["o"], bar["c"]) - bar["l"]) if long_ else (bar["h"] - max(bar["o"], bar["c"]))
    out = [f"Уровень {fp(s['level'])}: цена разворачивалась от этой зоны {s['touches']} раз(а) "
           f"на графиках 1h/4h — сильная поддержка/сопротивление."]
    if s["kind"] == "bounce":
        out.append(f"Часовая свеча ткнулась в уровень и закрылась {'выше' if long_ else 'ниже'} него; "
                   f"тень {wick / rng * 100:.0f}% свечи — {'покупатели выкупили' if long_ else 'продавцы сбили'} движение.")
    else:
        out.append("Уровень пробит часовой свечой на объёме ≥1.5× среднего, цена вернулась к нему "
                   "и удержалась — пробитое сопротивление стало поддержкой (или наоборот).")
    if (htf >= 3 and long_) or (htf <= -3 and not long_):
        trend = "сделка по старшему тренду"
    elif (htf <= -3 and long_) or (htf >= 3 and not long_):
        trend = "сделка против старшего тренда (повышенный риск)"
    else:
        trend = "старший тренд нейтральный"
    out.append(f"Старший тренд: дневка {ctx['score_d1']:+d}, 4h {ctx['score_h4']:+d} "
               f"(сумма {htf:+d} из ±10) — {trend}.")
    btc = ctx.get("btc_htf")
    if btc is not None and not s["symbol"].startswith("BTC-"):
        reg = "растёт" if btc >= 3 else "падает" if btc <= -3 else "в боковике"
        with_btc = (btc >= 0) if long_ else (btc <= 0)
        if p["btc_filter"] == "off":
            out.append(f"Биткоин {reg} (тренд {btc:+d}) — сделка {'по' if with_btc else 'против'} BTC. "
                       f"Фильтр по BTC выключен: на истории он не улучшал результат; обучение следит за этим отдельно.")
        else:
            out.append(f"Биткоин {reg} (тренд {btc:+d}); фильтр по BTC «{p['btc_filter']}» пропускает эту сделку: "
                       f"альты обычно идут за BTC.")
    out.append(f"RSI 1h {ctx['rsi1h']:.0f}: {'нет перекупленности' if long_ else 'нет перепроданности'}, "
               f"запас хода есть.")
    if ctx.get("funding") is not None:
        out.append(f"Funding {ctx['funding'] * 100:.3f}% за 8ч — толпа не перекошена в одну сторону.")
    out.append(f"Стоп {fp(s['stop'])} ({abs(s['entry'] - s['stop']) / s['entry'] * 100:.2f}%): за уровнем и тенью "
               f"плюс запас {p['stop_atr']}×ATR(1h)={fp(p['stop_atr'] * a)}. Если цена туда дошла — идея сломана.")
    out.append("Тейки ставлю чуть раньше следующих уровней, где цена уже разворачивалась: "
               + ", ".join(f"{fp(tp)} ({r:.1f}R)" for tp, r in zip(s["tps"], s["tp_r"]))
               + f". После первого тейка стоп переносится в безубыток.")
    return out


# --------------------------------------------------------------------------- sizing

def size(side, entry, stop, balance, risk_pct, leverage, contract=None, n_parts=3):
    contract = contract or {}
    fee = contract.get("taker_fee") or 0.0005
    step = contract.get("qty_step")
    min_qty = contract.get("min_qty") or 0.0

    risk_usd = balance * risk_pct / 100
    per_unit = abs(entry - stop) + (entry + stop) * fee
    qty = risk_usd / per_unit
    if step:
        qty = round(int(qty / step + 1e-9) * step, 10)
    warn = []
    if qty < min_qty:
        qty = min_qty
        warn.append(f"минимальный лот {min_qty} даёт риск {min_qty * per_unit:.2f}$ — больше заданного")
    notional = qty * entry
    margin = notional / leverage
    buffer = (balance - notional * MMR - notional * fee) / qty if qty else 0
    liq = entry - buffer if side == "long" else entry + buffer
    if margin > balance:
        warn.append("маржа больше депозита — позиция невозможна")
    if (side == "long" and stop <= liq) or (side == "short" and stop >= liq):
        warn.append("ЛИКВИДАЦИЯ РАНЬШЕ СТОПА")

    # split into one part per take-profit (up to three)
    k = max(1, min(3, n_parts))
    parts = [qty] + [0.0] * (k - 1)
    if step:
        n = int(round(qty / step))
        use = max(1, min(k, n))
        base, extra = divmod(n, use)
        parts = [round((base + (1 if i < extra else 0)) * step, 10) for i in range(use)] + [0.0] * (k - use)
    return {"qty": qty, "notional": notional, "margin": margin, "risk_usd": qty * per_unit,
            "liq": liq, "parts": parts, "fee": fee, "warnings": warn}


def fmt_price(p, decimals=None):
    if p is None:
        return "—"
    if decimals is None:
        decimals = 1 if p >= 1000 else 2 if p >= 10 else 4 if p >= 0.1 else 6
    return f"{p:,.{decimals}f}".replace(",", " ")
