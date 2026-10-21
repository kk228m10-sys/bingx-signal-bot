"""Follow sent signals on 5m candles: TP hits, stop, break-even, expiry.

Assumes the market entry filled at the signal price. When one candle touches
both the stop and a target, the stop is counted first (conservative).
"""
import time

import market as m

EXPIRY_H = 72


def _r(sig, exit_price):
    sign = 1 if sig["side"] == "long" else -1
    risk = abs(sig["entry"] - sig["stop0"])
    fee = sig.get("fee", 0.0005)
    return (sign * (exit_price - sig["entry"]) - (sig["entry"] + exit_price) * fee) / risk


def _weights(sig):
    parts = sig["parts"]
    total = sum(parts) or 1
    return [p / total for p in parts]


def update(sig, rows, now=None):
    """Advance one signal through new candles. Returns list of event dicts.

    `now` (epoch seconds) drives the 72h expiry; the backtest passes 0 to skip it.
    """
    now = time.time() if now is None else now
    events = []
    if sig["status"] != "open":
        return events
    long_ = sig["side"] == "long"
    w = _weights(sig)
    for b in rows:
        if b["t"] <= sig["last_t"]:
            continue
        sig["last_t"] = b["t"]
        hit_stop = b["l"] <= sig["stop"] if long_ else b["h"] >= sig["stop"]
        if hit_stop:
            rest = sum(w[sig["tp_hit"]:])
            sig["realized_r"] += rest * _r(sig, sig["stop"])
            sig["status"] = "closed"
            sig["closed_t"] = b["t"] / 1000
            sig["result"] = "breakeven" if sig["tp_hit"] else "stop"
            events.append({"type": sig["result"], "price": sig["stop"], "t": b["t"]})
            break
        while sig["tp_hit"] < min(3, len(sig["tps"])):
            tp = sig["tps"][sig["tp_hit"]]
            if (b["h"] >= tp) if long_ else (b["l"] <= tp):
                i = sig["tp_hit"]
                sig["realized_r"] += w[i] * _r(sig, tp)
                sig["tp_hit"] += 1
                events.append({"type": f"tp{i + 1}", "price": tp, "t": b["t"]})
                if i == 0:
                    # move stop to entry plus round-trip fees
                    f = sig.get("fee", 0.0005)
                    sig["stop"] = sig["entry"] * (1 + 2 * f) if long_ else sig["entry"] * (1 - 2 * f)
                if sig["tp_hit"] == 3 or sum(w[sig["tp_hit"]:]) == 0:
                    sig["status"] = "closed"
                    sig["closed_t"] = b["t"] / 1000
                    sig["result"] = f"tp{sig['tp_hit']}"
                    break
            else:
                break
        if sig["status"] != "open":
            break

    if sig["status"] == "open" and now - sig["sent_at"] > EXPIRY_H * 3600 and rows:
        last = rows[-1]["c"]
        sig["realized_r"] += sum(w[sig["tp_hit"]:]) * _r(sig, last)
        sig["status"] = "closed"
        sig["closed_t"] = rows[-1]["t"] / 1000
        sig["result"] = "expired"
        events.append({"type": "expired", "price": last, "t": rows[-1]["t"]})
    return events


def fetch_and_update(sig):
    rows = m.klines(sig["symbol"], "5m", 300, start_ms=sig["last_t"] + 1)
    rows = m.closed(rows) if rows and rows[-1]["t"] > time.time() * 1000 - 5 * 60 * 1000 else rows
    return update(sig, rows)


def stats(signals):
    closed = [s for s in signals if s["status"] == "closed"]
    if not closed:
        return None
    rs = [s["realized_r"] for s in closed]
    wins = [r for r in rs if r > 0.05]
    losses = [r for r in rs if r < -0.05]
    return {
        "total": len(signals), "closed": len(closed),
        "open": len(signals) - len(closed),
        "wins": len(wins), "losses": len(losses),
        "flat": len(closed) - len(wins) - len(losses),
        "winrate": len(wins) / len(closed) * 100,
        "avg_r": sum(rs) / len(rs), "sum_r": sum(rs),
        "usd": sum(s["realized_r"] * s.get("risk_usd", 0) for s in closed),
    }
