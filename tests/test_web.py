import hashlib
import hmac
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web  # noqa: E402

TOKEN = "123456:TEST-token"
ADMIN = 894128186


def signed_init(user_id=ADMIN, token=TOKEN, auth_date=None):
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE",
              "user": json.dumps({"id": user_id, "first_name": "x"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(fields)


STATE = {
    "settings": {"balance": 50, "risk": 2, "leverage": 20, "auto": True, "max_positions": 5,
                 "daily_loss_pct": 15, "max_dd_pct": 30, "universe_n": 30, "watch": ["BTC-USDT"]},
    "signals": [{"id": "a", "symbol": "DOGE-USDT", "side": "long", "variant": "base", "btc_htf": 4,
                 "status": "closed", "realized_r": -1.0, "sent_at": 1, "why": ["уровень"]}] * 9,
    "trades": [{"id": "t", "symbol": "DOGE-USDT", "side": "long", "status": "closed", "pnl_usd": 1.5,
                "closed_at": 2, "opened_at": 1, "tps": [], "why": ["уровень"]}],
    "decisions": [{"t": 1, "symbol": "DOGE-USDT", "side": "long", "variant": "base", "action": "blocked", "reason": "r"}],
    "live": {"equity": 100000, "btc_htf": 4},
}


class InitData(unittest.TestCase):
    def test_valid_admin(self):
        self.assertEqual(web.check_init_data(signed_init(), TOKEN, ADMIN)["id"], ADMIN)

    def test_other_user_rejected(self):
        self.assertIsNone(web.check_init_data(signed_init(user_id=1), TOKEN, ADMIN))

    def test_wrong_token_rejected(self):
        self.assertIsNone(web.check_init_data(signed_init(token="999:other"), TOKEN, ADMIN))

    def test_tampered_rejected(self):
        self.assertIsNone(web.check_init_data(signed_init().replace("first_name", "first_namX"), TOKEN, ADMIN))

    def test_expired_rejected(self):
        self.assertIsNone(web.check_init_data(signed_init(auth_date=int(time.time()) - 3 * 86400), TOKEN, ADMIN))

    def test_garbage_rejected(self):
        self.assertIsNone(web.check_init_data("", TOKEN, ADMIN))
        self.assertIsNone(web.check_init_data("hash=abc", TOKEN, ADMIN))


class View(unittest.TestCase):
    def test_build_view(self):
        v = web.build_view(STATE, "demo")
        self.assertEqual(v["mode"], "demo")
        self.assertEqual(len(v["trades_closed"]), 1)
        blocked = [b for b in v["buckets"] if b["blocked"]]
        self.assertTrue(blocked)                        # 9 straight losses: context switched off
        self.assertEqual(v["equity_usd"][-1][1], 1.5)
        json.dumps(v)                                   # serialisable


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state.json"
        cls.state.write_text(json.dumps(STATE), encoding="utf-8")
        cls.srv = web.start(0, cls.state, TOKEN, ADMIN, "demo")
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def get(self, path, init=None):
        req = urllib.request.Request(self.base + path, headers={"X-Init-Data": init} if init else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_index_and_health(self):
        code, body = self.get("/")
        self.assertEqual(code, 200)
        self.assertIn("telegram-web-app.js".encode(), body)
        self.assertEqual(self.get("/health")[0], 200)

    def test_api_requires_signature(self):
        self.assertEqual(self.get("/api/state")[0], 403)
        self.assertEqual(self.get("/api/state", signed_init(user_id=5))[0], 403)
        code, body = self.get("/api/state", signed_init())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["mode"], "demo")


if __name__ == "__main__":
    unittest.main()
