"""Score a Telegram channel's historical setups against BingX price data.

  python eval_channel.py futures_club11 --pages 40

For every parsed setup: market entry at the open of the first 15m candle after
the post, the channel's stop and targets, equal parts per target, stop to
break-even after TP1, taker fees, 7-day expiry. Result in R (1R = entry-to-stop).
"""
import argparse
import sys
import time

import channel as ch
import market as m
import tracker as tr

MS15 = 900_000
EXPIRY_D = 7
MAX_PRICE_GAP = 0.05     # BingX price must be within 5% of the price quoted in the post


def evaluate(s, now_ms):
    sym = f"{s['ticker']}-USDT"
    if m.contract_info(sym) is None:
        return {"skip": "нет на BingX"}
    start = int(s["ts"] * 1000) // MS15 * MS15 + MS15
    rows, cur, end = [], start, min(now_ms, start + EXPIRY_D * 86_400_000)
    while cur < end:
        batch = [r for r in m.klines(sym, "15m", 500, start_ms=cur) if r["t"] < end]
        if not batch:
            break
        rows += batch
        cur = batch[-1]["t"] + MS15
        time.sleep(0.15)
    rows = [r for r in rows if r["t"] + MS15 <= now_ms]
    if not rows:
        return {"skip": "нет свечей"}
    entry = rows[0]["o"]
    sign = 1 if s["side"] == "long" else -1
    if abs(entry / s["entry"] - 1) > MAX_PRICE_GAP:
        return {"skip": "цена на BingX далеко от цены в посте (другая монета или пост устарел)"}
    if sign * (entry - s["stop"]) <= 0:
        return {"skip": "цена уже за стопом к моменту поста"}
    tps = [tp for tp in s["tps"] if sign * (tp - entry) > 0]
    if not tps:
        return {"skip": "цели уже пройдены к моменту поста"}
    sig = {"symbol": sym, "side": s["side"], "entry": entry, "stop0": s["stop"], "stop": s["stop"],
           "tps": tps, "parts": [1.0] * len(tps), "fee": 0.0005, "risk_usd": 1.0, "sent_at": s["ts"],
           "last_t": rows[0]["t"] - 1, "tp_hit": 0, "realized_r": 0.0, "status": "open", "result": None}
    tr.update(sig, rows, now=0)
    if sig["status"] == "open":
        if now_ms >= start + EXPIRY_D * 86_400_000:
            w = tr._weights(sig)
            sig["realized_r"] += sum(w[sig["tp_hit"]:]) * tr._r(sig, rows[-1]["c"])
            sig["status"], sig["result"] = "closed", "expired"
        else:
            return {"open": True, "sig": sig}
    return {"sig": sig, "slip_pct": (entry / s["entry"] - 1) * 100}


def summarize(name, pages=40):
    """History stats of a channel for the bot / mini app."""
    posts = ch.fetch_history(name, pages=pages)
    sets = ch.setups(posts)
    now_ms = int(time.time() * 1000)
    done, skipped = [], 0
    for s in sets:
        try:
            res = evaluate(s, now_ms)
        except Exception:
            skipped += 1
            continue
        if "sig" in res and not res.get("open"):
            done.append(res["sig"])
        else:
            skipped += 1
    st = tr.stats(done) or {}
    by_side = {side: tr.stats([d for d in done if d["side"] == side]) for side in ("long", "short")}
    return {"t": time.time(), "posts": len(posts), "setups": len(sets), "closed": st.get("closed", 0),
            "winrate": st.get("winrate"), "avg_r": st.get("avg_r"), "sum_r": st.get("sum_r"), "skipped": skipped,
            "days": (posts[-1][1] - posts[0][1]) / 86400 if posts else 0,
            "by_side": {k: ({"n": v["closed"], "avg_r": v["avg_r"], "winrate": v["winrate"]} if v else None)
                        for k, v in by_side.items()}}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("channel")
    ap.add_argument("--pages", type=int, default=40)
    args = ap.parse_args()
    posts = ch.fetch_history(args.channel, pages=args.pages)
    sets = ch.setups(posts)
    span = (posts[-1][1] - posts[0][1]) / 86400 if posts else 0
    print(f"posts: {len(posts)} over {span:.0f} days, setups parsed+valid: {len(sets)}", flush=True)
    now_ms = int(time.time() * 1000)
    done, skipped, still_open = [], {}, 0
    for s in sets:
        res = evaluate(s, now_ms)
        if "skip" in res:
            skipped[res["skip"]] = skipped.get(res["skip"], 0) + 1
            continue
        if res.get("open"):
            still_open += 1
            continue
        g = res["sig"]
        g["ticker"] = s["ticker"]
        done.append(g)
        day = time.strftime("%m-%d", time.gmtime(s["ts"]))
        print(f"  {day} #{s['post_id']:<5} {s['ticker']:10s} {s['side']:5s} stop {abs(g['entry'] - g['stop0']) / g['entry'] * 100:5.1f}%"
              f" tps {len(g['tps'])} -> {g['result']:9s} {g['realized_r']:+.2f}R", flush=True)
    print(f"\nskipped: {skipped}, still open: {still_open}")
    st = tr.stats(done)
    if st:
        print(f"CLOSED {st['closed']}: win {st['wins']} loss {st['losses']} flat {st['flat']}, "
              f"winrate {st['winrate']:.0f}%, avg {st['avg_r']:+.2f}R, total {st['sum_r']:+.1f}R "
              f"(~{st['sum_r']:+.1f}$ at 1$ risk per trade)")
        for side in ("long", "short"):
            part = tr.stats([d for d in done if d["side"] == side])
            if part:
                print(f"  {side:5s}: n={part['closed']} winrate {part['winrate']:.0f}% avg {part['avg_r']:+.2f}R")


if __name__ == "__main__":
    main()
