"""BingX signal bot: scans futures every 15 minutes and sends trade plans to Telegram.

Read-only market data, never places orders. Run: python bot.py
"""
import html
import json
import logging
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import market as m
import signals as sg
import tracker as tr

ROOT = Path(__file__).resolve().parent


def load_env(path):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


load_env(ROOT / ".env")
TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))
TZ = timezone(timedelta(hours=float(os.environ.get("TZ_OFFSET_HOURS", "5"))))
DATA_DIR = Path(os.environ.get("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
API = f"https://api.telegram.org/bot{TOKEN}"

COOLDOWN_H = 4           # same symbol + side not repeated within this window
DIGEST_HOUR = 9          # local hour for the morning overview

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(DATA_DIR / "bot.log", encoding="utf-8")])
log = logging.getLogger("bot")

DEFAULTS = {"balance": 50.0, "risk": 2.0, "leverage": 20.0,
            "watch": ["BTC-USDT", "ETH-USDT", "SOL-USDT"], "paused": False}


# --------------------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        st = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("settings", {})
    for k, v in DEFAULTS.items():
        st["settings"].setdefault(k, v)
    st.setdefault("signals", [])
    st.setdefault("cooldown", {})
    st.setdefault("last_bar", {})
    st.setdefault("digest_day", "")
    return st


def save_state(st):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_FILE)


# --------------------------------------------------------------------------- telegram

def tg(method, **params):
    data = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}).encode()
    timeout = int(params.get("timeout", 0)) + 15
    with urllib.request.urlopen(f"{API}/{method}", data=data, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def send(text, chat_id=None):
    try:
        tg("sendMessage", chat_id=chat_id or ADMIN_ID, text=text, parse_mode="HTML",
           disable_web_page_preview="true")
    except Exception as e:
        log.error("send failed: %s", e)


def local(ts_ms):
    return datetime.fromtimestamp(ts_ms / 1000, TZ).strftime("%d.%m %H:%M")


# --------------------------------------------------------------------------- formatting

P = sg.fmt_price
KIND = {"bounce": "отбой от уровня", "retest": "ретест пробитого уровня"}
ARROW = lambda s: "↑" if s >= 3 else "↓" if s <= -3 else "→"


def coin(sym):
    return sym.split("-")[0]


def fmt_signal(s, sz, ctx, st):
    long_ = s["side"] == "long"
    dec = (m.contract_info(s["symbol"]) or {}).get("price_decimals")
    pp = lambda x: P(x, dec)
    head = "🟢 <b>ЛОНГ</b>" if long_ else "🔴 <b>ШОРТ</b>"
    stop_pct = abs(s["entry"] - s["stop"]) / s["entry"] * 100
    lines = [
        f"{head} <b>{s['symbol']}</b> · {KIND[s['kind']]}",
        f"Уверенность: {s['confidence']}",
        f"Уровень {pp(s['level'])} (касаний: {s['touches']})",
        "",
        f"Вход: <b>по рынку ≈ {pp(s['entry'])}</b> (или лимит {pp(s['limit'])})",
        f"Стоп: <b>{pp(s['stop'])}</b> (−{stop_pct:.2f}%)",
    ]
    for i, (tp, r) in enumerate(zip(s["tps"], s["tp_r"])):
        part = sz["parts"][i]
        q = f" — {part:g} {coin(s['symbol'])}" if part else " — (объём мал для деления)"
        lines.append(f"Тейк {i + 1}: <b>{pp(tp)}</b> (R {r:.1f}){q}")
    se = st["settings"]
    liq_pct = abs(s["entry"] - sz["liq"]) / s["entry"] * 100
    liq_txt = f"далеко (>{50}%, кросс)" if liq_pct > 50 else f"{pp(sz['liq'])} ({liq_pct:.0f}% от входа)"
    lines += [
        "",
        f"Объём: <b>{sz['qty']:g} {coin(s['symbol'])}</b> ≈ {sz['notional']:.0f}$ · "
        f"маржа {sz['margin']:.2f}$ при {se['leverage']:g}x кросс",
        f"Риск: <b>{sz['risk_usd']:.2f}$</b> ({se['risk']:g}% от {se['balance']:g}$) · "
        f"ликвидация ≈ {liq_txt}",
        f"После Тейка 1 стоп в безубыток: {pp(s['entry'] * (1 + 2 * sz['fee']) if long_ else s['entry'] * (1 - 2 * sz['fee']))}",
    ]
    for w in sz["warnings"]:
        lines.append(f"⚠️ {html.escape(w)}")
    fund = f"{ctx['funding'] * 100:.3f}%" if ctx["funding"] is not None else "—"
    lines += [
        "",
        f"Фон: 1d {ARROW(ctx['score_d1'])} · 4h {ARROW(ctx['score_h4'])} · 1h {ARROW(ctx['score_h1'])} · "
        f"RSI 1h {ctx['rsi1h']:.0f} · funding {fund}",
        f"Вход актуален ~30 мин, пока цена не ушла дальше {pp(s['entry'] + (1 if long_ else -1) * 0.3 * abs(s['entry'] - s['stop']))}",
        f"<i>Часовая свеча закрылась {local(s['bar_close'])}. Это сигнал по правилам, не гарантия; решение за тобой.</i>",
    ]
    return "\n".join(lines)


def fmt_overview(ctx):
    sym = ctx["symbol"]
    dec = (m.contract_info(sym) or {}).get("price_decimals")
    pp = lambda x: P(x, dec)
    price = ctx["price"]
    sup = sorted([(p, n) for p, n in ctx["levels"] if p < price and n >= 2], key=lambda x: -x[0])[:3]
    res = sorted([(p, n) for p, n in ctx["levels"] if p > price and n >= 2], key=lambda x: x[0])[:3]
    fund = f"{ctx['funding'] * 100:.3f}%" if ctx["funding"] is not None else "—"
    htf = sg.htf_score(ctx)
    lean = ("лонги по тренду" if htf >= 3 else "шорты по тренду" if htf <= -3
            else "боковик — торговать от краёв уровней")
    oi = f"{ctx['oi'] / 1e6:,.0f} млн".replace(",", " ") if ctx["oi"] else "—"
    return "\n".join([
        f"<b>{sym}</b> {pp(price)}",
        f"Тренд: 1d {ARROW(ctx['score_d1'])} ({ctx['score_d1']:+d}) · 4h {ARROW(ctx['score_h4'])} ({ctx['score_h4']:+d}) · "
        f"1h {ARROW(ctx['score_h1'])} ({ctx['score_h1']:+d})",
        f"RSI 1h {ctx['rsi1h']:.0f} · 15m {ctx['rsi15']:.0f} · funding {fund} · OI {oi}",
        "Сопротивления: " + (", ".join(f"{pp(p)} ({n})" for p, n in res) or "—"),
        "Поддержки: " + (", ".join(f"{pp(p)} ({n})" for p, n in sup) or "—"),
        f"Сейчас: {lean}",
    ])


EVENT_TEXT = {
    "tp1": "✅ Тейк 1 взят {price}. <b>Перенеси стоп в безубыток: {be}</b>",
    "tp2": "✅ Тейк 2 взят {price}",
    "tp3": "🏁 Тейк 3 взят {price} — сделка закрыта полностью",
    "stop": "❌ Стоп {price}",
    "breakeven": "➖ Остаток закрылся в безубыток {price}",
    "expired": "⌛ Сигнал истёк через 72 ч, условно закрыт по {price}",
}


# --------------------------------------------------------------------------- jobs

def scan(st, manual=False):
    se = st["settings"]
    sent = 0
    for sym in se["watch"]:
        try:
            ctx = sg.build_context(sym)
        except Exception as e:
            log.warning("context %s failed: %s", sym, e)
            continue
        trig = ctx["h1"] if sg.PARAMS["trigger"] == "1h" else ctx["m15"]
        bar_t = trig[-1]["t"]
        if not manual and st["last_bar"].get(sym) == bar_t:
            continue
        st["last_bar"][sym] = bar_t
        for s in sg.find_setups(ctx):
            key = f"{sym}:{s['side']}"
            if time.time() - st["cooldown"].get(key, 0) < COOLDOWN_H * 3600:
                log.info("cooldown %s", key)
                continue
            contract = m.contract_info(sym)
            sz = sg.size(s["side"], s["entry"], s["stop"], se["balance"], se["risk"], se["leverage"], contract)
            send(fmt_signal(s, sz, ctx, st))
            st["cooldown"][key] = time.time()
            st["signals"].append({
                "id": f"{sym}-{s['side']}-{bar_t}", "symbol": sym, "side": s["side"], "kind": s["kind"],
                "entry": s["entry"], "stop0": s["stop"], "stop": s["stop"], "tps": s["tps"],
                "parts": sz["parts"], "risk_usd": sz["risk_usd"], "fee": sz["fee"],
                "confidence": s["confidence"], "sent_at": time.time(),
                "last_t": s["bar_close"] - 1, "tp_hit": 0, "realized_r": 0.0,
                "status": "open", "result": None,
            })
            sent += 1
            log.info("signal %s %s entry=%s stop=%s tps=%s", sym, s["side"], s["entry"], s["stop"], s["tps"])
    save_state(st)
    return sent


def track(st):
    changed = False
    for sig in st["signals"]:
        if sig["status"] != "open":
            continue
        try:
            events = tr.fetch_and_update(sig)
        except Exception as e:
            log.warning("track %s failed: %s", sig["id"], e)
            continue
        dec = (m.contract_info(sig["symbol"]) or {}).get("price_decimals")
        for ev in events:
            changed = True
            text = EVENT_TEXT[ev["type"]].format(price=P(ev["price"], dec), be=P(sig["stop"], dec))
            side = "ЛОНГ" if sig["side"] == "long" else "ШОРТ"
            tail = f"\nИтог сделки: {sig['realized_r']:+.2f}R ≈ {sig['realized_r'] * sig['risk_usd']:+.2f}$" \
                if sig["status"] == "closed" else ""
            send(f"{sig['symbol']} {side} (вход {P(sig['entry'], dec)})\n{text}{tail}")
    if changed:
        save_state(st)


def digest(st):
    now = datetime.now(TZ)
    day = now.strftime("%Y-%m-%d")
    if now.hour < DIGEST_HOUR or st["digest_day"] == day:
        return
    st["digest_day"] = day
    save_state(st)
    try:
        text = "☀️ <b>Утренний обзор</b>\n\n" + fmt_overview(sg.build_context("BTC-USDT"))
    except Exception as e:
        text = f"☀️ Утренний обзор не собрался: {html.escape(str(e))}"
    s = tr.stats(st["signals"])
    if s:
        text += f"\n\nСигналы: {s['closed']} закрыто, винрейт {s['winrate']:.0f}%, итог {s['sum_r']:+.1f}R"
    send(text)


# --------------------------------------------------------------------------- commands

HELP = """<b>Команды</b>
/now [монета] — обзор рынка (по умолчанию BTC)
/scan — проверить сетапы прямо сейчас
/open — открытые сигналы
/stats — статистика всех сигналов
/watch — список монет · /add SOL · /remove SOL
/balance 50 · /risk 2 · /leverage 20 — настройки расчёта
/pause · /resume — выключить/включить сигналы

Бот проверяет монеты после закрытия каждой часовой свечи и пишет, только когда есть отбой от сильного уровня по направлению старшего тренда (1d+4h). Обычно это 1–3 сигнала в день на 3 монеты, бывают дни без сигналов. Ордеров он не ставит."""


def handle(st, msg):
    chat = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not text:
        return
    if chat != ADMIN_ID:
        send("Это приватный бот.", chat)
        log.info("ignored chat %s", chat)
        return
    cmd, *args = text.split()
    cmd = cmd.split("@")[0].lower()
    se = st["settings"]

    if cmd in ("/start", "/help"):
        send(f"Привет! Слежу за: {', '.join(se['watch'])}.\nДепозит {se['balance']:g}$, риск {se['risk']:g}%, "
             f"плечо {se['leverage']:g}x кросс.\n\n" + HELP)
    elif cmd == "/now":
        sym = m.norm_symbol(args[0]) if args else "BTC-USDT"
        try:
            send(fmt_overview(sg.build_context(sym)))
        except Exception as e:
            send(f"Не получилось загрузить {sym}: {html.escape(str(e))}")
    elif cmd == "/scan":
        send("Проверяю…")
        n = scan(st, manual=True)
        if not n:
            send("Сетапов по правилам сейчас нет. Это нормально: лучше ждать уровень, чем входить посередине.")
    elif cmd == "/open":
        opened = [s for s in st["signals"] if s["status"] == "open"]
        if not opened:
            send("Открытых сигналов нет.")
        for s in opened:
            dec = (m.contract_info(s["symbol"]) or {}).get("price_decimals")
            send(f"{s['symbol']} {'ЛОНГ' if s['side'] == 'long' else 'ШОРТ'} вход {P(s['entry'], dec)}, "
                 f"стоп {P(s['stop'], dec)}, тейков взято {s['tp_hit']}/3, отправлен {local(s['sent_at'] * 1000)}")
    elif cmd == "/stats":
        s = tr.stats(st["signals"])
        if not s:
            send(f"Закрытых сигналов пока нет (всего отправлено: {len(st['signals'])}).")
        else:
            send(f"<b>Статистика</b>\nСигналов: {s['total']} (закрыто {s['closed']}, открыто {s['open']})\n"
                 f"Плюс {s['wins']} · минус {s['losses']} · ноль {s['flat']} · винрейт {s['winrate']:.0f}%\n"
                 f"Средний результат {s['avg_r']:+.2f}R · итого {s['sum_r']:+.1f}R ≈ {s['usd']:+.2f}$\n"
                 f"<i>Считается вход по цене сигнала и перенос стопа в безубыток после Тейка 1.</i>")
    elif cmd == "/watch":
        send("Слежу за: " + ", ".join(se["watch"]))
    elif cmd == "/add" and args:
        sym = m.norm_symbol(args[0])
        if m.contract_info(sym) is None:
            send(f"{sym} не нашёл среди фьючерсов BingX.")
        elif sym not in se["watch"]:
            if len(se["watch"]) >= 10:
                send("Максимум 10 монет.")
            else:
                se["watch"].append(sym)
                send("Добавил. Слежу за: " + ", ".join(se["watch"]))
    elif cmd == "/remove" and args:
        sym = m.norm_symbol(args[0])
        if sym in se["watch"]:
            se["watch"].remove(sym)
        send("Слежу за: " + (", ".join(se["watch"]) or "ничего"))
    elif cmd in ("/balance", "/risk", "/leverage") and args:
        try:
            val = float(args[0].replace(",", "."))
        except ValueError:
            send("Нужно число, например /balance 50")
            return
        key = cmd[1:]
        limits = {"balance": (1, 1e7), "risk": (0.1, 5), "leverage": (1, 125)}
        lo, hi = limits[key]
        if not lo <= val <= hi:
            send(f"Допустимо от {lo:g} до {hi:g}." + (" Риск выше 5% на сделку не считаю." if key == "risk" else ""))
            return
        se[key] = val
        send(f"Готово: {key} = {val:g}")
    elif cmd == "/pause":
        se["paused"] = True
        send("Сигналы на паузе. Трекинг открытых продолжаю. /resume — включить.")
    elif cmd == "/resume":
        se["paused"] = False
        send("Сигналы включены.")
    else:
        send(HELP)
    save_state(st)


# --------------------------------------------------------------------------- main loop

def next_boundary(minutes, delay_s):
    now = time.time()
    step = minutes * 60
    t = (now // step + 1) * step + delay_s
    return t if t - now > 0 else t + step


def main():
    if not TOKEN or not ADMIN_ID:
        sys.exit("BOT_TOKEN and ADMIN_ID must be set in .env")
    st = load_state()
    me = tg("getMe")["result"]
    log.info("started as @%s", me.get("username"))
    send(f"✅ Бот запущен. Слежу за: {', '.join(st['settings']['watch'])}. /help — команды.")

    next_scan = next_boundary(15, 25)
    next_track = next_boundary(5, 20)
    errors = 0
    while True:
        try:
            wait = max(1, min(25, int(min(next_scan, next_track) - time.time())))
            upd = tg("getUpdates", offset=st["offset"], timeout=wait, allowed_updates=["message"])
            for u in upd.get("result", []):
                st["offset"] = u["update_id"] + 1
                if "message" in u:
                    handle(st, u["message"])
            if upd.get("result"):
                save_state(st)

            if time.time() >= next_track:
                track(st)
                digest(st)
                next_track = next_boundary(5, 20)
            if time.time() >= next_scan:
                if not st["settings"]["paused"]:
                    scan(st)
                next_scan = next_boundary(15, 25)
            errors = 0
        except urllib.error.HTTPError as e:
            if e.code == 409:
                log.error("409 conflict: another copy of the bot is running with this token")
            else:
                log.error("HTTP %s", e)
            time.sleep(10)
        except Exception:
            errors += 1
            log.error("loop error:\n%s", traceback.format_exc())
            if errors == 5:
                send("⚠️ Бот ловит ошибки подряд (сеть или BingX). Продолжаю пытаться.")
            time.sleep(min(60, 5 * errors))


if __name__ == "__main__":
    main()
