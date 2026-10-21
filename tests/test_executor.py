import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import executor as exm  # noqa: E402

BTC = {"min_qty": 0.0001, "qty_step": 0.0001, "taker_fee": 0.0005}


class FakeExchange:
    """Minimal in-memory BingX: positions, reduce-only conditional orders, fills on demand."""

    def __init__(self, hedged=True, equity=100000.0, attach_stop=True, fail_stop=False):
        self.hedged_mode, self.eq = hedged, equity
        self.attach_stop, self.fail_stop = attach_stop, fail_stop
        self.pos = {}            # (symbol, side) -> {"qty", "entry"}
        self.orders = {}         # id -> order dict
        self.closed = []
        self.n = 0
        self.leverage = None
        self.calls = []

    def _id(self):
        self.n += 1
        return str(self.n)

    def fetch_position_mode(self, symbol):
        return {"hedged": self.hedged_mode}

    def fetch_balance(self):
        return {"USDT": {"total": self.eq, "free": self.eq}}

    def set_margin_mode(self, mode, symbol):
        self.calls.append(("margin", mode))

    def set_leverage(self, lev, symbol, params):
        self.leverage = lev
        self.calls.append(("leverage", lev, params["side"]))

    def fetch_positions(self, symbols):
        return [{"contracts": p["qty"], "entryPrice": p["entry"], "side": side, "unrealizedPnl": 0}
                for (sym, side), p in self.pos.items() if sym in symbols and p["qty"] > 0]

    def create_order(self, symbol, type_, side, qty, price, params):
        self.calls.append(("order", side, qty, dict(params)))
        if params.get("reduceOnly"):
            pos_side = "long" if side == "sell" else "short"
            if "stopLossPrice" in params:
                if self.fail_stop:
                    raise RuntimeError("stop rejected")
                oid = self._id()
                self.orders[oid] = {"id": oid, "type": "STOP_MARKET", "qty": qty, "side": side,
                                    "trigger": params["stopLossPrice"], "pos_side": pos_side}
                return {"id": oid}
            if "takeProfitPrice" in params:
                oid = self._id()
                self.orders[oid] = {"id": oid, "type": "TAKE_PROFIT_MARKET", "qty": qty, "side": side,
                                    "trigger": params["takeProfitPrice"], "pos_side": pos_side}
                return {"id": oid}
            p = self.pos[(symbol, pos_side)]                     # market close
            p["qty"] = round(p["qty"] - qty, 10)
            self.closed.append({"side": side, "filled": qty, "average": 100.0, "fee": {"cost": 0}})
            return {"id": self._id()}
        pos_side = "long" if side == "buy" else "short"
        self.pos[(symbol, pos_side)] = {"qty": qty, "entry": 100.0}
        if self.attach_stop and "stopLoss" in params:
            oid = self._id()
            self.orders[oid] = {"id": oid, "type": "STOP_MARKET", "qty": qty, "side": "sell",
                                "trigger": params["stopLoss"]["triggerPrice"], "pos_side": pos_side}
        self.closed.append({"side": side, "filled": qty, "average": 100.0, "fee": {"cost": 0}})
        return {"id": self._id()}

    def fetch_open_orders(self, symbol):
        return [{"id": o["id"], "type": o["type"].lower(), "info": {"type": o["type"]}} for o in self.orders.values()]

    def cancel_order(self, oid, symbol):
        self.orders.pop(oid, None)

    def cancel_all_orders(self, symbol):
        self.orders.clear()

    def fetch_closed_orders(self, symbol, since):
        return list(self.closed)

    # test helper: fill a conditional order at its trigger
    def fill(self, oid, symbol="BTC/USDT:USDT"):
        o = self.orders.pop(oid)
        p = self.pos[(symbol, o["pos_side"])]
        q = min(o["qty"], p["qty"])
        p["qty"] = round(p["qty"] - q, 10)
        self.closed.append({"side": o["side"], "filled": q, "average": o["trigger"], "fee": {"cost": 0}})


def state(**over):
    se = {"balance": 50.0, "risk": 2.0, "leverage": 20.0, "auto": True, "max_positions": 2,
          "daily_loss_pct": 6.0, "max_dd_pct": 30.0}
    se.update(over)
    return {"settings": se, "trades": []}


SIG = {"symbol": "BTC-USDT", "side": "long", "entry": 100.0, "stop": 98.0, "tps": [102.0, 104.0, 106.0]}


@mock.patch("executor.time.sleep", lambda s: None)
class OpenTrade(unittest.TestCase):
    def test_happy_path_long(self):
        fx = FakeExchange()
        st = state()
        tr, msg = exm.Executor(fx, "demo").open_trade(st, SIG, {**BTC, "qty_step": 0.01, "min_qty": 0.01})
        self.assertIsNotNone(tr, msg)
        self.assertIn(("margin", "isolated"), fx.calls)
        entry = [c for c in fx.calls if c[0] == "order"][0]
        self.assertEqual(entry[1], "buy")
        self.assertEqual(entry[3]["stopLoss"]["triggerPrice"], 98.0)
        self.assertEqual(len(tr["tps"]), 3)
        kinds = sorted(o["type"] for o in fx.orders.values())
        self.assertEqual(kinds, ["STOP_MARKET"] + ["TAKE_PROFIT_MARKET"] * 3)
        self.assertLessEqual(tr["risk_usd"], 1.0 + 1e-9)        # 2% of 50$ budget, not of 100k equity

    def test_leverage_capped_so_liquidation_is_beyond_stop(self):
        fx = FakeExchange()
        s = {**SIG, "stop": 96.0}                                  # 4% stop
        exm.Executor(fx, "demo").open_trade(state(leverage=20), s, {**BTC, "qty_step": 0.01, "min_qty": 0.01})
        self.assertLessEqual(fx.leverage, 1 / (0.04 * 1.3 + 0.005))
        self.assertLess(fx.leverage, 20)

    def test_risk_capped_at_two_percent(self):
        fx = FakeExchange()
        tr, _ = exm.Executor(fx, "demo").open_trade(state(risk=5), SIG, {**BTC, "qty_step": 0.01, "min_qty": 0.01})
        self.assertLessEqual(tr["risk_usd"], 1.0 + 1e-9)

    def test_skip_when_min_lot_too_big(self):
        fx = FakeExchange()
        tr, msg = exm.Executor(fx, "demo").open_trade(state(), SIG, {**BTC, "qty_step": 1, "min_qty": 1})
        self.assertIsNone(tr)
        self.assertIn("пропуск", msg)
        self.assertFalse(fx.pos)

    def test_missing_stop_is_placed_separately(self):
        fx = FakeExchange(attach_stop=False)
        tr, _ = exm.Executor(fx, "demo").open_trade(state(), SIG, {**BTC, "qty_step": 0.01, "min_qty": 0.01})
        self.assertIsNotNone(tr)
        self.assertTrue(any(o["type"] == "STOP_MARKET" for o in fx.orders.values()))

    def test_no_stop_possible_closes_position(self):
        fx = FakeExchange(attach_stop=False, fail_stop=True)
        tr, msg = exm.Executor(fx, "demo").open_trade(state(), SIG, {**BTC, "qty_step": 0.01, "min_qty": 0.01})
        self.assertIsNone(tr)
        self.assertIn("АВАРИЯ", msg)
        self.assertEqual(fx.pos[("BTC/USDT:USDT", "long")]["qty"], 0)

    def test_one_way_mode_uses_both(self):
        fx = FakeExchange(hedged=False)
        exm.Executor(fx, "demo").open_trade(state(), SIG, {**BTC, "qty_step": 0.01, "min_qty": 0.01})
        self.assertIn(("leverage", fx.leverage, "BOTH"), fx.calls)


@mock.patch("executor.time.sleep", lambda s: None)
class Manage(unittest.TestCase):
    def setUp(self):
        self.fx = FakeExchange()
        self.ex = exm.Executor(self.fx, "demo")
        self.st = state()
        self.tr, _ = self.ex.open_trade(self.st, SIG, {**BTC, "qty_step": 0.01, "min_qty": 0.01})

    def test_tp1_moves_stop_to_breakeven(self):
        self.fx.fill(self.tr["tps"][0]["id"])
        msgs = self.ex.manage(self.st)
        self.assertTrue(self.tr["be_moved"])
        stops = [o for o in self.fx.orders.values() if o["type"] == "STOP_MARKET"]
        self.assertEqual(len(stops), 1)
        self.assertGreater(stops[0]["trigger"], 100.0)
        self.assertTrue(any("безубыток" in x for x in msgs))

    def test_stop_hit_closes_trade_and_cleans_orders(self):
        stop_id = next(o["id"] for o in self.fx.orders.values() if o["type"] == "STOP_MARKET")
        self.fx.fill(stop_id)
        msgs = self.ex.manage(self.st)
        self.assertEqual(self.tr["status"], "closed")
        self.assertFalse(self.fx.orders)                          # leftover TPs cancelled
        self.assertLess(self.tr["pnl_usd"], 0)
        self.assertTrue(any("закрыта" in x for x in msgs))

    def test_lost_stop_is_restored(self):
        stop_id = next(o["id"] for o in self.fx.orders.values() if o["type"] == "STOP_MARKET")
        self.fx.cancel_order(stop_id, None)
        self.ex.manage(self.st)
        self.assertTrue(any(o["type"] == "STOP_MARKET" for o in self.fx.orders.values()))

    def test_kill_closes_and_disables(self):
        msgs = self.ex.kill(self.st)
        self.assertEqual(self.fx.pos[("BTC/USDT:USDT", "long")]["qty"], 0)
        self.assertFalse(self.st["settings"]["auto"])
        self.assertFalse(self.fx.orders)
        self.assertTrue(msgs)


class Limits(unittest.TestCase):
    def test_limits(self):
        ex = exm.Executor(FakeExchange(), "demo")
        st = state()
        self.assertTrue(ex.can_trade(st, "BTC-USDT")[0])
        st["trades"].append({"symbol": "BTC-USDT", "status": "open"})
        self.assertFalse(ex.can_trade(st, "BTC-USDT")[0])           # one per symbol
        st["trades"].append({"symbol": "ETH-USDT", "status": "open"})
        self.assertFalse(ex.can_trade(st, "SOL-USDT")[0])           # max positions
        st["trades"] = [{"symbol": "X", "status": "closed", "pnl_usd": -3.5, "closed_day": exm._today()}]
        ok, why = ex.can_trade(st, "SOL-USDT")
        self.assertFalse(ok)
        self.assertIn("дневной", why)
        st["trades"] = [{"symbol": "X", "status": "closed", "pnl_usd": -16, "closed_day": "2000-01-01"}]
        self.assertFalse(ex.can_trade(st, "SOL-USDT")[0])           # total drawdown
        self.assertFalse(st["settings"]["auto"])
        st["settings"]["auto"] = False
        self.assertIn("выключена", ex.can_trade(state(auto=False), "SOL-USDT")[1])


if __name__ == "__main__":
    unittest.main()
