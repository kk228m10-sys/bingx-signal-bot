"""Learning from closed signals: block contexts that lose with confidence.

Every signal (executed or not) is followed by tracker.py until it closes with a
result in R. Results are grouped into buckets (side, side x BTC regime, symbol,
variant, coin group), each over its last WINDOW results. Before a new entry,
each matching bucket is checked:
  * block   if n >= MIN_N and the upper bound mean + Z*sd/sqrt(n) is still below 0
            (the context loses even under an optimistic reading of the noise);
  * live mode additionally requires the variant itself to be proven:
            n >= PROVEN_N and mean > PROVEN_MEAN.
This is deliberately conservative: with a few dozen trades per bucket, only
clearly bad contexts can be told apart from bad luck.

On the demo account the verdict is advisory (the bot still trades and records
whether the advice would have helped); it blocks entries only on a live account.
A walk-forward test on 25 alts (60d) showed blocking on demo would have stopped
almost all trading while the kept trades were no better, hence advisory there.
"""
import math

MIN_N = 8
SYMBOL_MIN_N = 6
Z = 1.0
PROVEN_N = 20
PROVEN_MEAN = 0.05
MAJORS = {"BTC-USDT", "ETH-USDT", "SOL-USDT"}


def btc_regime(htf):
    if htf is None:
        return "?"
    return "up" if htf >= 3 else "down" if htf <= -3 else "flat"


def buckets(sig):
    side = sig["side"]
    reg = btc_regime(sig.get("btc_htf"))
    group = "major" if sig["symbol"] in MAJORS else "alt"
    return [
        ("variant", sig.get("variant", "base")),
        ("side", side),
        ("side+btc", f"{side}/{reg}"),
        ("group+side", f"{group}/{side}"),
        ("symbol", sig["symbol"]),
    ]


WINDOW = 40   # only the most recent results per bucket count: markets change, old lessons fade


def table(signals, window=WINDOW):
    """bucket -> {n, sum, sumsq, wins} over the last `window` closed results of each bucket."""
    closed = sorted((s for s in signals if s.get("status") == "closed"),
                    key=lambda s: s.get("closed_t") or s.get("sent_at") or 0)
    per = {}
    for s in closed:
        r = s.get("realized_r") or 0.0
        for b in buckets(s):
            per.setdefault(b, []).append(r)
    t = {}
    for b, rs in per.items():
        rs = rs[-window:] if window else rs
        t[b] = {"n": len(rs), "sum": sum(rs), "sumsq": sum(r * r for r in rs),
                "wins": sum(1 for r in rs if r > 0.05)}
    return t


def summary(e):
    n = e["n"]
    mean = e["sum"] / n
    var = max(0.0, e["sumsq"] / n - mean * mean) * n / max(1, n - 1)
    se = math.sqrt(var / n) if n > 1 else float("inf")
    return mean, se


def decide(sig, signals, live=False):
    """Return (allowed, reason)."""
    t = table(signals)
    for b in buckets(sig):
        e = t.get(b)
        need = SYMBOL_MIN_N if b[0] == "symbol" else MIN_N
        if not e or e["n"] < need:
            continue
        mean, se = summary(e)
        if mean + Z * se < 0:
            return False, f"обучение: «{b[0]}={b[1]}» стабильно в минусе ({e['n']} сделок, в среднем {mean:+.2f}R)"
    if live:
        e = t.get(("variant", sig.get("variant", "base")))
        if not e or e["n"] < PROVEN_N or summary(e)[0] <= PROVEN_MEAN:
            n = e["n"] if e else 0
            return False, f"на реальном счёте торгую только доказанные варианты (у этого {n} сделок)"
    return True, ""


def report(signals, top=8):
    t = table(signals)
    closed = sum(1 for s in signals if s.get("status") == "closed")
    if not t:
        return "Пока нечему учиться: закрытых сигналов нет."
    rows = []
    for (kind, val), e in t.items():
        mean, se = summary(e)
        rows.append((kind, val, e["n"], e["wins"] / e["n"] * 100, mean, mean + Z * se < 0 and e["n"] >= MIN_N))
    out = [f"Закрытых сигналов: {closed}"]
    for kind in ("variant", "side+btc", "group+side"):
        part = sorted((r for r in rows if r[0] == kind), key=lambda r: -r[4])
        if part:
            out.append(f"\n<b>{kind}</b>")
            out += [f"{'⛔' if r[5] else '·'} {r[1]}: {r[2]} шт, винрейт {r[3]:.0f}%, {r[4]:+.2f}R" for r in part]
    syms = sorted((r for r in rows if r[0] == "symbol" and r[2] >= 3), key=lambda r: -r[4])
    if syms:
        out.append("\n<b>лучшие монеты</b>")
        out += [f"· {r[1]}: {r[2]} шт, {r[4]:+.2f}R" for r in syms[:top]]
        out.append("<b>худшие монеты</b>")
        out += [f"{'⛔' if r[5] else '·'} {r[1]}: {r[2]} шт, {r[4]:+.2f}R" for r in syms[-top:][::-1]]
    out.append("\n⛔ — бот перестал входить в таких условиях")
    return "\n".join(out)
