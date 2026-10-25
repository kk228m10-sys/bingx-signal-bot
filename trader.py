"""Automatic execution of signals on BingX perpetual futures through ccxt.

TRADING_MODE: off (default) | demo (BingX VST demo account) | live (real money).
Safety, enforced here regardless of user settings:
  * isolated margin per position; leverage lowered so liquidation sits beyond the stop
  * entry order carries its stop-loss; if no stop is confirmed the position is closed at once
  * risk per trade capped at MAX_RISK_PCT of the budget; trades below the exchange
    minimum lot are skipped (never sized up)
  * at most `max_positions` open, one per symbol
  * daily loss limit and total drawdown limit halt new entries
"""
import logging
import math
import time
from datetime import datetime, timezone

import signals as sg

log = logging.getLogger("TRADER")

MAX_RISK_PCT = 2.0
MMR = 0.005


def ccxt_symbol(sym):
    base, quote = sym.split("-")
    return f"{base}/{quote}:{quote}"


def _otype(o):
    return str((o.get("info") or {}).get("type") or o.get("type") or "").upper()


def _is_stop(o):
    return _otype(o) in ("STOP_MARKET", "STOP")


def _is_tp(o):
    return _otype(o) in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class Trader:
    def __init__(self, ex, mode):
        self.ex = ex
        self.mode = mode
        self._hedged = {}

    # ------------------------------------------------------------------ helpers

    @classmethod
    def from_env(cls, mode, key, secret):
        import ccxt  # imported lazily: signals-only bots do not need it
        ex = ccxt.bingx({"apiKey": key, "secret": secret, "enableRateLimit": True,
                         "options": {"defaultType": "swap"}})
        if mode == "demo":
            ex.set_sandbox_mode(True)
        ex.load_markets()
        return cls(ex, mode)

    def hedged(self, sym):
        if sym not in self._hedged:
            try:
                self._hedged[sym] = bool(self.ex.fetch_position_mode(ccxt_symbol(sym)).get("hedged"))
            except Exception as e:
                log.warning("position mode %s: %s; assuming hedge mode", sym, e)
                self._hedged[sym] = True
        return self._hedged[sym]

    def equity(self):
        bal = self.ex.fetch_balance()
        for cur in ("USDT", "VST"):
            if cur in bal and bal[cur].get("total") is not None:
                return float(bal[cur]["total"]), float(bal[cur].get("free") or 0)
        return 0.0, 0.0

    def position(self, sym, side=None):
        for p in self.ex.fetch_positions([ccxt_symbol(sym)]):
            amt = abs(float(p.get("contracts") or 0))
            if amt > 0 and (side is None or p.get("side") == side):
                return {"qty": amt, "entry": float(p.get("entryPrice") or 0), "side": p.get("side"),
                        "upnl": float(p.get("unrealizedPnl") or 0)}
        return None

    def _reduce(self, sym, trade_side, qty, extra):
        close_side = "sell" if trade_side == "long" else "buy"
        params = {"hedged": self.hedged(sym), "reduceOnly": True, **extra}
        return self.ex.create_order(ccxt_symbol(sym), "market", close_side, qty, None, params)

    def close_now(self, sym, trade_side, qty):
        return self._reduce(sym, trade_side, qty, {})

    # ------------------------------------------------------------------ limits

    @staticmethod
    def pnl_today(st):
        return sum(t.get("pnl_usd") or 0 for t in st.get("trades", [])
                   if t["status"] == "closed" and t.get("closed_day") == _today())

    @staticmethod
    def pnl_total(st):
        return sum(t.get("pnl_usd") or 0 for t in st.get("trades", []) if t["status"] == "closed")

    def can_trade(self, st, sym):
        se = st["settings"]
        if not se.get("auto"):
            return False, "автоторговля выключена (/autotrade on)"
        budget = se["balance"]
        if self.pnl_today(st) <= -budget * se["daily_loss_pct"] / 100:
            return False, f"дневной лимит потерь {se['daily_loss_pct']:g}% достигнут — до завтра (UTC) без входов"
        if self.mode == "live" and self.pnl_total(st) <= -budget * se["max_dd_pct"] / 100:
            se["auto"] = False
            return False, f"общая просадка больше {se['max_dd_pct']:g}% бюджета — автоторговля выключена"
        open_trades = [t for t in st.get("trades", []) if t["status"] == "open"]
        if any(t["symbol"] == sym for t in open_trades):
            return False, f"по {sym} уже есть позиция"
        if len(open_trades) >= se["max_positions"]:
            return False, f"уже открыто {len(open_trades)} позиции (лимит {se['max_positions']})"
        return True, ""

    # ------------------------------------------------------------------ open

    def open_trade(self, st, s, contract):
        """Open a position for signal `s`. Returns (trade | None, message)."""
        se = st["settings"]
        sym, side = s["symbol"], s["side"]
        csym = ccxt_symbol(sym)
        hedged = self.hedged(sym)
        equity, free = self.equity()
        budget = min(se["balance"], equity)
        risk_pct = min(se["risk"], MAX_RISK_PCT)

        stop_pct = abs(s["entry"] - s["stop"]) / s["entry"]
        lev_cap = math.floor(1 / (stop_pct * 1.3 + MMR))          # liquidation beyond the stop
        lev = int(max(1, min(se["leverage"], lev_cap, 50)))
        sz = sg.size(side, s["entry"], s["stop"], budget, risk_pct, lev, contract)
        if any("минимальный лот" in w for w in sz["warnings"]):
            return None, f"пропуск: минимальный лот {sym} даёт риск больше {risk_pct:g}% от {budget:.0f}$"
        if sz["margin"] > free * 0.95:
            return None, f"пропуск: не хватает свободной маржи ({free:.2f}$ < {sz['margin']:.2f}$)"

        try:
            self.ex.set_margin_mode("isolated", csym)
        except Exception as e:  # already isolated, or not changeable right now
            log.info("margin mode %s: %s", sym, e)
        self.ex.set_leverage(lev, csym, {"side": ("LONG" if side == "long" else "SHORT") if hedged else "BOTH"})

        open_side = "buy" if side == "long" else "sell"
        order = self.ex.create_order(csym, "market", open_side, sz["qty"], None,
                                     {"hedged": hedged, "stopLoss": {"triggerPrice": s["stop"]}})
        time.sleep(1.5)
        pos = self.position(sym, side)
        if not pos:
            return None, f"ошибка: ордер {order.get('id')} отправлен, но позиция не появилась — проверь BingX"

        # the stop must exist before anything else
        stop_id = self._find_stop(sym)
        if not stop_id:
            try:
                o = self._reduce(sym, side, pos["qty"], {"stopLossPrice": s["stop"]})
                stop_id = o.get("id")
            except Exception as e:
                log.error("stop placement failed %s: %s", sym, e)
        if not stop_id:
            self.close_now(sym, side, pos["qty"])
            return None, "АВАРИЯ: стоп не выставился, позиция сразу закрыта по рынку"

        n_tp = len(s["tps"])
        parts = sg.size(side, pos["entry"], s["stop"], budget, risk_pct, lev, contract, n_parts=n_tp)["parts"]
        if abs(sum(parts) - pos["qty"]) > 1e-9:
            parts = [pos["qty"]] + [0.0] * (n_tp - 1)
        tps, problems = [], []
        for price, q in zip(s["tps"], parts):
            if q <= 0:
                continue
            try:
                o = self._reduce(sym, side, q, {"takeProfitPrice": price})
                tps.append({"price": price, "qty": q, "id": o.get("id"), "filled": False})
            except Exception as e:
                problems.append(f"тейк {price:g}: {e}")
                log.error("tp failed %s %s: %s", sym, price, e)

        trade = {
            "id": f"{sym}-{side}-{int(time.time())}", "symbol": sym, "side": side,
            "qty": pos["qty"], "entry": pos["entry"], "stop": s["stop"], "stop_id": stop_id,
            "tps": tps, "be_moved": False, "leverage": lev, "risk_usd": sz["risk_usd"],
            "opened_at": time.time(), "status": "open", "pnl_usd": None, "mode": self.mode,
            "variant": s.get("variant"), "btc_htf": s.get("btc_htf"), "why": s.get("why"),
        }
        st.setdefault("trades", []).append(trade)
        msg = (f"🤖 Открыл {'ЛОНГ' if side == 'long' else 'ШОРТ'} {sym} ({self.mode}): "
               f"{pos['qty']:g} по {pos['entry']:g}, плечо {lev}x изол., стоп {s['stop']:g}, "
               f"тейков выставлено {len(tps)}/3, риск ≈ {sz['risk_usd']:.2f}$")
        if problems:
            msg += "\n⚠️ " + "; ".join(problems)
        return trade, msg

    def _find_stop(self, sym):
        for o in self.ex.fetch_open_orders(ccxt_symbol(sym)):
            if _is_stop(o):
                return o.get("id")
        return None

    # ------------------------------------------------------------------ manage

    def manage(self, st):
        """Follow open trades: break-even after TP1, stop safety net, close-out. Returns messages."""
        msgs = []
        for t in st.get("trades", []):
            if t["status"] != "open":
                continue
            sym, side = t["symbol"], t["side"]
            try:
                pos = self.position(sym, side)
                orders = self.ex.fetch_open_orders(ccxt_symbol(sym))
            except Exception as e:
                log.warning("manage %s: %s", sym, e)
                continue
            open_ids = {o.get("id") for o in orders}

            if not pos:
                for o in orders:                       # leftovers of a finished trade
                    try:
                        self.ex.cancel_order(o["id"], ccxt_symbol(sym))
                    except Exception as e:
                        log.warning("cancel leftover %s: %s", o.get("id"), e)
                t["pnl_usd"] = self._realized(t)
                t["status"], t["closed_at"], t["closed_day"] = "closed", time.time(), _today()
                hit = sum(1 for x in t["tps"] if x["id"] not in open_ids)
                msgs.append(f"🏁 {sym} {'ЛОНГ' if side == 'long' else 'ШОРТ'} закрыта "
                            f"(тейков сработало: {hit}). Итог ≈ {t['pnl_usd']:+.2f}$")
                continue

            for x in t["tps"]:
                if not x["filled"] and x["id"] not in open_ids:
                    x["filled"] = True
                    msgs.append(f"✅ {sym}: тейк {x['price']:g} исполнен ({x['qty']:g})")

            stops = [o for o in orders if _is_stop(o)]
            if any(x["filled"] for x in t["tps"]) and not t["be_moved"]:
                fee = 0.0005
                be = t["entry"] * (1 + 2 * fee) if side == "long" else t["entry"] * (1 - 2 * fee)
                try:
                    new = self._reduce(sym, side, pos["qty"], {"stopLossPrice": be})
                    for o in stops:
                        self.ex.cancel_order(o["id"], ccxt_symbol(sym))
                    t.update(stop=be, stop_id=new.get("id"), be_moved=True)
                    msgs.append(f"🛡 {sym}: стоп перенесён в безубыток {be:g} на остаток {pos['qty']:g}")
                except Exception as e:
                    log.error("BE move %s: %s", sym, e)
                    msgs.append(f"⚠️ {sym}: не смог перенести стоп в безубыток ({e}), попробую снова")
            elif not stops:
                try:                                    # safety net: position without a stop
                    new = self._reduce(sym, side, pos["qty"], {"stopLossPrice": t["stop"]})
                    t["stop_id"] = new.get("id")
                    msgs.append(f"⚠️ {sym}: стоп пропал — выставил заново {t['stop']:g}")
                except Exception as e:
                    self.close_now(sym, side, pos["qty"])
                    msgs.append(f"АВАРИЯ {sym}: позиция без стопа и стоп не ставится ({e}) — закрыл по рынку")
        return msgs

    def _realized(self, t):
        """Approximate realized P&L from closing fills since the trade opened."""
        sign = 1 if t["side"] == "long" else -1
        close_side = "sell" if t["side"] == "long" else "buy"
        try:
            orders = self.ex.fetch_closed_orders(ccxt_symbol(t["symbol"]), int(t["opened_at"] * 1000) - 60_000)
        except Exception as e:
            log.warning("closed orders %s: %s", t["symbol"], e)
            return 0.0
        pnl = fees = 0.0
        for o in orders:
            filled, avg = float(o.get("filled") or 0), float(o.get("average") or o.get("price") or 0)
            if not filled or not avg:
                continue
            fee = (o.get("fee") or {}).get("cost")
            fees += abs(float(fee)) if fee is not None else filled * avg * 0.0005
            if o.get("side") == close_side:
                pnl += sign * (avg - t["entry"]) * filled
        return pnl - fees

    # ------------------------------------------------------------------ kill switch

    def kill(self, st):
        """Cancel orders and close positions of the bot's own open trades, then switch auto off."""
        msgs = []
        symbols = sorted({t["symbol"] for t in st.get("trades", []) if t["status"] == "open"})
        if not symbols:
            msgs.append("открытых сделок бота нет")
        for sym in symbols:
            csym = ccxt_symbol(sym)
            try:
                self.ex.cancel_all_orders(csym)
            except Exception as e:
                msgs.append(f"{sym}: отмена ордеров — {e}")
            for side in {t["side"] for t in st["trades"] if t["status"] == "open" and t["symbol"] == sym}:
                try:
                    pos = self.position(sym, side)
                    if pos:
                        self.close_now(sym, side, pos["qty"])
                        msgs.append(f"{sym}: закрыл {side} {pos['qty']:g}")
                except Exception as e:
                    msgs.append(f"{sym}: закрытие {side} — {e}")
        for t in st.get("trades", []):
            if t["status"] == "open":
                t["pnl_usd"] = self._realized(t)
                t["status"], t["closed_at"], t["closed_day"] = "closed", time.time(), _today()
        st["settings"]["auto"] = False
        return msgs
