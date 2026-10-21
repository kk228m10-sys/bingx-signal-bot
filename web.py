"""Telegram Mini App backend: serves the dashboard and a read-only JSON view of the bot state.

Runs in a daemon thread next to the bot. Reads state.json from disk (written
atomically by the bot), so it never touches the bot's in-memory objects.
Every API call must carry Telegram WebApp initData signed with the bot token
and belonging to ADMIN_ID.
"""
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import learner as ln
import signals as sg
import tracker as tr

log = logging.getLogger("web")
ROOT = Path(__file__).resolve().parent
MAX_AGE_S = 24 * 3600


def check_init_data(init_data, token, admin_id, now=None):
    """Validate Telegram WebApp initData (HMAC-SHA256). Returns user dict or None."""
    try:
        pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    if (now or time.time()) - int(pairs.get("auth_date", "0")) > MAX_AGE_S:
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None
    return user if user.get("id") == admin_id else None


# --------------------------------------------------------------------------- view model

def _cum(points):
    out, acc = [], 0.0
    for t, v in sorted(points):
        acc += v
        out.append([int(t), round(acc, 4)])
    return out


def build_view(st, mode):
    se = st.get("settings", {})
    signals = st.get("signals", [])
    trades = st.get("trades", [])
    closed_sig = [s for s in signals if s.get("status") == "closed"]

    table = ln.table(signals)
    buckets = []
    for (kind, val), e in table.items():
        mean, se_ = ln.summary(e)
        need = ln.SYMBOL_MIN_N if kind == "symbol" else ln.MIN_N
        buckets.append({"kind": kind, "value": val, "n": e["n"], "winrate": e["wins"] / e["n"] * 100,
                        "mean_r": mean, "upper": mean + ln.Z * se_ if se_ != float("inf") else None,
                        "blocked": e["n"] >= need and mean + ln.Z * se_ < 0,
                        "need": need})

    def trade_view(t):
        return {k: t.get(k) for k in ("id", "symbol", "side", "qty", "entry", "stop", "be_moved", "leverage",
                                      "risk_usd", "opened_at", "closed_at", "status", "pnl_usd", "variant",
                                      "btc_htf", "why", "tps", "mode")}

    def sig_view(s):
        return {k: s.get(k) for k in ("id", "symbol", "side", "kind", "variant", "btc_htf", "entry", "stop0",
                                      "tps", "sent_at", "status", "result", "realized_r", "tp_hit",
                                      "confidence", "why", "silent", "advice")}

    stats = tr.stats(signals) or {}
    advised = [s for s in closed_sig if s.get("advice")]
    advice_check = {"advised": tr.stats(advised), "not_advised": tr.stats([s for s in closed_sig if not s.get("advice")])}
    return {
        "generated_at": time.time(),
        "mode": mode,
        "settings": {k: se.get(k) for k in ("balance", "risk", "leverage", "auto", "max_positions",
                                            "daily_loss_pct", "max_dd_pct", "universe_n", "watch")},
        "live": st.get("live", {}),
        "universe": (st.get("universe") or {}).get("alts", []),
        "trades_open": [trade_view(t) for t in trades if t.get("status") == "open"],
        "trades_closed": [trade_view(t) for t in sorted(
            (t for t in trades if t.get("status") == "closed"), key=lambda t: -(t.get("closed_at") or 0))[:100]],
        "equity_usd": _cum((t.get("closed_at") or 0, t.get("pnl_usd") or 0) for t in trades
                           if t.get("status") == "closed"),
        "equity_r": _cum((s.get("sent_at") or 0, s.get("realized_r") or 0) for s in closed_sig),
        "signal_stats": stats,
        "advice_check": advice_check,
        "signals_recent": [sig_view(s) for s in sorted(signals, key=lambda s: -(s.get("sent_at") or 0))[:150]],
        "buckets": buckets,
        "decisions": list(reversed(st.get("decisions", [])))[:150],
        "learning_rules": {"min_n": ln.MIN_N, "symbol_min_n": ln.SYMBOL_MIN_N, "z": ln.Z, "window": ln.WINDOW,
                           "proven_n": ln.PROVEN_N, "proven_mean": ln.PROVEN_MEAN},
        "variants": [{"name": n, "params": {k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()}}
                     for n, p in sg.VARIANTS],
    }


# --------------------------------------------------------------------------- server

class Handler(BaseHTTPRequestHandler):
    state_file = None
    token = ""
    admin_id = 0
    mode = "off"

    def log_message(self, fmt, *args):  # keep the bot log clean
        pass

    def _send(self, code, body, ctype):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, (ROOT / "webapp" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/health":
            self._send(200, "ok", "text/plain")
        elif path == "/api/state":
            user = check_init_data(self.headers.get("X-Init-Data", ""), self.token, self.admin_id)
            if not user:
                self._send(403, json.dumps({"error": "forbidden"}), "application/json")
                return
            try:
                st = json.loads(Path(self.state_file).read_text(encoding="utf-8"))
                body = json.dumps(build_view(st, self.mode), ensure_ascii=False, default=float)
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as e:
                log.error("api/state: %s", e)
                self._send(500, json.dumps({"error": str(e)}), "application/json")
        else:
            self._send(404, "not found", "text/plain")


def start(port, state_file, token, admin_id, mode):
    Handler.state_file, Handler.token, Handler.admin_id, Handler.mode = str(state_file), token, admin_id, mode
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("web app listening on 0.0.0.0:%d", port)
    return srv
