import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import learner as ln  # noqa: E402


def sig(r, symbol="DOGE-USDT", side="long", btc=4, variant="base"):
    return {"symbol": symbol, "side": side, "btc_htf": btc, "variant": variant,
            "status": "closed", "realized_r": r}


class Learner(unittest.TestCase):
    def test_too_few_samples_never_block(self):
        hist = [sig(-1)] * 5
        self.assertTrue(ln.decide(sig(0), hist)[0])

    def test_consistent_losses_block_context(self):
        hist = [sig(-1.0, symbol=f"X{i}-USDT") for i in range(10)]
        ok, why = ln.decide(sig(0, symbol="NEW-USDT"), hist)
        self.assertFalse(ok)
        self.assertIn("стабильно в минусе", why)

    def test_noisy_mix_is_not_blocked(self):
        hist = [sig(r, symbol=f"X{i}-USDT") for i, r in enumerate([-1, 2.5, -1, -1, 1.8, -1, -1, 3, -1, -1])]
        self.assertTrue(ln.decide(sig(0, symbol="NEW-USDT"), hist)[0])

    def test_other_side_unaffected(self):
        hist = [sig(-1.0, side="short", symbol=f"X{i}-USDT", btc=4, variant="v2") for i in range(12)]
        self.assertTrue(ln.decide(sig(0, side="long", symbol="NEW-USDT"), hist)[0])
        self.assertFalse(ln.decide(sig(0, side="short", symbol="NEW-USDT"), hist)[0])
        # a losing variant is blocked for both sides
        self.assertFalse(ln.decide(sig(0, side="long", symbol="NEW-USDT", variant="v2"), hist)[0])

    def test_live_requires_proven_variant(self):
        hist = [sig(0.5, symbol=f"X{i}-USDT") for i in range(10)]
        self.assertFalse(ln.decide(sig(0, symbol="NEW-USDT"), hist, live=True)[0])
        hist = [sig(r, symbol=f"X{i}-USDT") for i, r in enumerate([1.5, -1, 0.8] * 8)]
        self.assertTrue(ln.decide(sig(0, symbol="NEW-USDT"), hist, live=True)[0])

    def test_report_renders(self):
        hist = [sig(-1.0, symbol=f"X{i % 3}-USDT") for i in range(12)]
        text = ln.report(hist)
        self.assertIn("⛔", text)
        self.assertIn("variant", text)


if __name__ == "__main__":
    unittest.main()
