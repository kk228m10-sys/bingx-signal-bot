import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import signals as sg  # noqa: E402
import tracker as tr  # noqa: E402

BTC = {"min_qty": 0.0001, "qty_step": 0.0001, "taker_fee": 0.0005}


def bar(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": 1.0}


def sig(side="long", entry=100.0, stop=98.0, tps=(103.0, 105.0, 108.0), parts=(1, 1, 1)):
    return {"symbol": "X-USDT", "side": side, "entry": entry, "stop0": stop, "stop": stop,
            "tps": list(tps), "parts": list(parts), "fee": 0.0, "risk_usd": 1.0,
            "sent_at": 0, "last_t": 0, "tp_hit": 0, "realized_r": 0.0,
            "status": "open", "result": None}


class Sizing(unittest.TestCase):
    def test_risk_matches_budget_and_rounds_down(self):
        sz = sg.size("long", 82100, 81150, 50, 2, 20, BTC)
        self.assertEqual(sz["qty"], 0.0009)
        self.assertLessEqual(sz["risk_usd"], 1.0)
        self.assertAlmostEqual(sz["margin"], 0.0009 * 82100 / 20)
        self.assertEqual(sz["parts"], [0.0003, 0.0003, 0.0003])
        self.assertEqual(sz["warnings"], [])

    def test_min_lot_warning(self):
        sz = sg.size("long", 82100, 70000, 50, 0.5, 20, BTC)
        self.assertEqual(sz["qty"], 0.0001)
        self.assertTrue(any("минимальный лот" in w for w in sz["warnings"]))

    def test_cross_liquidation_side(self):
        long_ = sg.size("long", 100, 99, 50, 2, 20, None)
        short = sg.size("short", 100, 101, 50, 2, 20, None)
        self.assertLess(long_["liq"], 99)
        self.assertGreater(short["liq"], 101)


class Tracking(unittest.TestCase):
    def test_full_win(self):
        s = sig()
        ev = tr.update(s, [bar(1, 100, 103.5, 99, 103), bar(2, 103, 108.5, 102, 108)], now=0)
        self.assertEqual([e["type"] for e in ev], ["tp1", "tp2", "tp3"])
        self.assertEqual(s["result"], "tp3")
        self.assertAlmostEqual(s["realized_r"], (1.5 + 2.5 + 4.0) / 3)

    def test_stop_first_on_ambiguous_candle(self):
        s = sig()
        ev = tr.update(s, [bar(1, 100, 104, 97, 100)], now=0)
        self.assertEqual(ev[0]["type"], "stop")
        self.assertAlmostEqual(s["realized_r"], -1.0)

    def test_breakeven_after_tp1(self):
        s = sig()
        ev = tr.update(s, [bar(1, 100, 103.2, 99.5, 103), bar(2, 103, 103, 99.9, 100)], now=0)
        self.assertEqual([e["type"] for e in ev], ["tp1", "breakeven"])
        self.assertAlmostEqual(s["realized_r"], 1.5 / 3)

    def test_short_mirror(self):
        s = sig("short", 100, 102, (97, 95, 92))
        ev = tr.update(s, [bar(1, 100, 100.5, 96.5, 97)], now=0)
        self.assertEqual(ev[0]["type"], "tp1")
        self.assertEqual(s["stop"], 100)

    def test_stats(self):
        a, b = sig(), sig()
        tr.update(a, [bar(1, 100, 108.5, 99, 108)], now=0)
        tr.update(b, [bar(1, 100, 100, 97, 98)], now=0)
        st = tr.stats([a, b])
        self.assertEqual((st["wins"], st["losses"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
