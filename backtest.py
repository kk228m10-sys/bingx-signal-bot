"""Replay the signal rules over BingX history and score the outcomes.

  python backtest.py BTC-USDT ETH-USDT SOL-USDT --days 30          # current PARAMS
  python backtest.py BTC-USDT ETH-USDT SOL-USDT --days 90 --grid   # tune on older part, check on last 30d

No look-ahead: each step sees closed candles only. Funding filter is off (no
historical funding). Outcomes follow the bot: market entry at the signal close,
stop first on ambiguous candles, break-even after TP1, 72h expiry, taker fees.
"""
import argparse
import itertools
import time

import learner as ln
import market as m
import signals as sg
import tracker as tr

MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def history(symbol, interval, start_ms, end_ms):
    rows, cur = {}, start_ms
    while cur < end_ms:
        batch = [r for r in m.klines(symbol, interval, 500, start_ms=cur) if r["t"] < end_ms]
        if not batch:
            break
        for r in batch:
            rows[r["t"]] = r
        nxt = batch[-1]["t"] + MS[interval]
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.15)
    return [rows[t] for t in sorted(rows)]


def prepare(symbol, days, btc_htf=None):
    """Fetch history and precompute the context at every closed 15m candle.

    `btc_htf` maps candle close time -> BTC 1d+4h trend score, used by the BTC filter.
    """
    now = int(time.time() * 1000)
    start = now - days * MS["1d"]
    m15 = history(symbol, "15m", start - 3 * MS["1d"], now)
    h1 = history(symbol, "1h", start - 320 * MS["1h"], now)
    h4 = history(symbol, "4h", start - 220 * MS["4h"], now)
    d1 = history(symbol, "1d", start - 260 * MS["1d"], now)
    m15 = [r for r in m15 if r["t"] + MS["15m"] <= now]

    steps, j1, j4, jd = [], 0, 0, 0
    for i, bar in enumerate(m15):
        t_close = bar["t"] + MS["15m"]
        while j1 < len(h1) and h1[j1]["t"] + MS["1h"] <= t_close:
            j1 += 1
        while j4 < len(h4) and h4[j4]["t"] + MS["4h"] <= t_close:
            j4 += 1
        while jd < len(d1) and d1[jd]["t"] + MS["1d"] <= t_close:
            jd += 1
        new_hour = t_close % MS["1h"] == 0
        if bar["t"] < start or j1 < 60 or j4 < 60 or jd < 60:
            continue
        if btc_htf is not None and not new_hour:
            continue                      # alt runs use the 1h trigger only: skip 15m steps to save time
        ctx = sg.context_from(symbol, d1[max(0, jd - 300):jd], h4[max(0, j4 - 500):j4],
                              h1[max(0, j1 - 500):j1], m15[max(0, i - 199):i + 1], bar["c"])
        if btc_htf is not None:
            ctx["btc_htf"] = btc_htf.get(t_close)
        steps.append((i, t_close, new_hour, ctx))
    return {"symbol": symbol, "m15": m15, "steps": steps, "contract": m.contract_info(symbol)}


def simulate(data, p, t_from=0, t_to=None, balance=50, risk=2, lev=20):
    m15, sigs, cooldown = data["m15"], [], {}
    end = m15[-1]["t"] + MS["15m"]
    for i, t_close, new_hour, ctx in data["steps"]:
        if t_close < t_from or (t_to and t_close >= t_to):
            continue
        if p.get("trigger") == "1h" and not new_hour:
            continue
        for s in sg.find_setups(ctx, p):
            if t_close - cooldown.get(s["side"], 0) < 4 * MS["1h"]:
                continue
            cooldown[s["side"]] = t_close
            sz = sg.size(s["side"], s["entry"], s["stop"], balance, risk, lev, data["contract"])
            sig = {"symbol": data["symbol"], "side": s["side"], "kind": s["kind"], "entry": s["entry"],
                   "stop0": s["stop"], "stop": s["stop"], "tps": s["tps"], "parts": sz["parts"],
                   "risk_usd": sz["risk_usd"], "fee": sz["fee"], "confidence": s["confidence"],
                   "sent_at": t_close / 1000, "last_t": m15[i]["t"], "tp_hit": 0,
                   "realized_r": 0.0, "status": "open", "result": None}
            expiry = t_close + tr.EXPIRY_H * MS["1h"]
            horizon = [r for r in m15[i + 1:i + 1 + tr.EXPIRY_H * 4] if r["t"] < expiry]
            tr.update(sig, horizon, now=0)    # expiry handled below from candle time
            if sig["status"] == "open" and end >= expiry and horizon:
                w = tr._weights(sig)
                sig["realized_r"] += sum(w[sig["tp_hit"]:]) * tr._r(sig, horizon[-1]["c"])
                sig["status"], sig["result"] = "closed", "expired"
                sig["closed_t"] = expiry / 1000
            sig.update(variant=p.get("_name", "base"), btc_htf=ctx.get("btc_htf"))
            sigs.append(sig)
    return sigs


def line(st):
    if not st:
        return "no closed signals"
    return (f"n={st['closed']:3d}  win {st['winrate']:3.0f}%  avg {st['avg_r']:+.2f}R  "
            f"total {st['sum_r']:+6.1f}R  ~{st['usd']:+7.2f}$")


VARIANTS = {
    "base (BTC-фильтр выкл)": {},
    "base + BTC soft": {"btc_filter": "soft"},
    "base + BTC strict": {"btc_filter": "strict"},
    "bounce+retest, BTC soft": {"kinds": ("bounce", "retest"), "btc_filter": "soft"},
    "без фильтра тренда, BTC soft": {"trend_only": False, "btc_filter": "soft"},
    "стоп 0.8 ATR, BTC soft": {"stop_atr": 0.8, "tp1_min_r": 1.5, "btc_filter": "soft"},
}


def run_alts(top, days):
    btc = prepare("BTC-USDT", days)
    btc_htf = {t: sg.htf_score(ctx) for _, t, _, ctx in btc["steps"]}
    syms = m.top_symbols(top, exclude={"BTC-USDT", "ETH-USDT", "SOL-USDT"})
    print(f"alts ({len(syms)}): {', '.join(s.split('-')[0] for s in syms)}", flush=True)
    datas = []
    for sym in syms:
        try:
            t0 = time.time()
            datas.append(prepare(sym, days, btc_htf))
            print(f"  {sym}: {len(datas[-1]['steps'])} steps, {time.time() - t0:.0f}s", flush=True)
        except Exception as e:
            print(f"  {sym}: skipped ({e})", flush=True)
    print(f"\nRules chosen on BTC/ETH/SOL, tested on {len(datas)} alts over {days}d (out-of-sample for alts):")
    for name, p in VARIANTS.items():
        sigs = sum((simulate(d, p) for d in datas), [])
        print(f"  {name:32s} {line(tr.stats(sigs))}  ~{len(sigs) / days:.1f}/day")
    sigs = {d["symbol"]: simulate(d, VARIANTS["base + BTC soft"]) for d in datas}
    print("\nbase + BTC soft by symbol:")
    for sym, ss in sorted(sigs.items(), key=lambda kv: -(tr.stats(kv[1]) or {"sum_r": 0})["sum_r"]):
        st = tr.stats(ss)
        if st:
            print(f"  {sym:14s} {line(st)}")
    by_side = {}
    for ss in sigs.values():
        for s in ss:
            by_side.setdefault(s["side"], []).append(s)
    for side, ss in by_side.items():
        print(f"  side {side:5s}: {line(tr.stats(ss))}")

    # walk-forward learner: the bot only knows results of signals closed before each new one
    print("\nWalk-forward learner (decisions use only results closed before the signal):")
    for vset in (["base"], ["base", "retest", "range"]):
        pool = []
        for name in vset:
            p = {**dict(sg.VARIANTS)[name], "_name": name}
            for d in datas:
                pool += simulate(d, p)
        pool = [s for s in pool if s["status"] == "closed"]
        pool.sort(key=lambda s: s["sent_at"])
        kept, done = [], []
        for s in pool:
            hist = [x for x in done if x["closed_t"] <= s["sent_at"]]
            if ln.decide(s, hist)[0]:
                kept.append(s)
            done.append(s)
        third = pool[0]["sent_at"] + (pool[-1]["sent_at"] - pool[0]["sent_at"]) * 2 / 3
        late_all = [s for s in pool if s["sent_at"] >= third]
        late_kept = [s for s in kept if s["sent_at"] >= third]
        print(f"  variants {'+'.join(vset)}")
        print(f"    all signals     : {line(tr.stats(pool))}")
        print(f"    learner kept    : {line(tr.stats(kept))}")
        print(f"    last third, all : {line(tr.stats(late_all))}")
        print(f"    last third, kept: {line(tr.stats(late_kept))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*")
    ap.add_argument("--alts", type=int, default=0, help="test on the top-N alts by volume")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--grid", action="store_true")
    ap.add_argument("--oos-days", type=int, default=30)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    if args.alts:
        run_alts(args.alts, args.days)
        return

    datas = []
    for sym in args.symbols:
        t0 = time.time()
        datas.append(prepare(m.norm_symbol(sym), args.days))
        print(f"prepared {sym}: {len(datas[-1]['steps'])} steps in {time.time() - t0:.0f}s", flush=True)

    if not args.grid:
        allsig = []
        for d in datas:
            sigs = simulate(d, {})
            allsig += sigs
            print(f"\n{d['symbol']}: {line(tr.stats(sigs))}")
            if args.verbose:
                for s in sigs:
                    t = time.strftime("%m-%d %H:%M", time.gmtime(s["sent_at"]))
                    print(f"  {t}Z {s['side']:5} {s['kind']:6} {s['entry']:<10g} stop {s['stop0']:<10g}"
                          f" -> {s['result'] or 'open':9} {s['realized_r']:+.2f}R")
        print(f"\nALL: {line(tr.stats(allsig))}")
        return

    split = int(time.time() * 1000) - args.oos_days * MS["1d"]
    grid = {
        "trigger": ["15m", "1h"],
        "stop_atr": [0.4, 0.8, 1.2],
        "trend_only": [False, True],
        "kinds": [("bounce", "retest"), ("bounce",), ("retest",)],
        "tp1_min_r": [1.0, 1.5],
        "touch_min": [3, 4],
    }
    keys = list(grid)
    results = []
    for combo in itertools.product(*grid.values()):
        p = dict(zip(keys, combo))
        ins = sum((simulate(d, p, t_to=split) for d in datas), [])
        st = tr.stats(ins)
        if st and st["closed"] >= 15:
            results.append((st["sum_r"], st["avg_r"], p, st))
    results.sort(key=lambda x: x[1], reverse=True)

    print(f"\nTop parameter sets on IN-SAMPLE ({args.days - args.oos_days}d), then OUT-OF-SAMPLE (last {args.oos_days}d):")
    for _, _, p, st in results[:8]:
        oos = sum((simulate(d, p, t_from=split) for d in datas), [])
        short = {k: v for k, v in p.items()}
        print(f"\n{short}\n  IS : {line(st)}\n  OOS: {line(tr.stats(oos))}")
    worst = results[-1]
    print(f"\nWorst IS set for reference: {worst[2]}\n  IS : {line(worst[3])}")
    print(f"Combos with >=15 IS signals: {len(results)}; positive avg R: {sum(1 for r in results if r[1] > 0)}")


if __name__ == "__main__":
    main()
