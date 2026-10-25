"""Read trade setups from public Telegram channels (web preview t.me/s/<name>).

Posts are treated purely as data: only ticker, direction and price levels are
extracted with regular expressions. Nothing in a post is ever executed or
followed as an instruction. A setup is accepted only if its numbers are
internally consistent (stop / entry / targets on the right sides, sane distances).
"""
import html
import re
import time
import urllib.request
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (bingx-signal-bot channel reader)"}
NUM = r"(\d+(?:[.,]\d+)?)"


def fetch_page(channel, before=None):
    url = f"https://t.me/s/{channel}" + (f"?before={before}" if before else "")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def parse_posts(page):
    """[(post_id, epoch_seconds, text)] from a t.me/s page, oldest first."""
    out = []
    for chunk in re.split(r'<div class="tgme_widget_message_wrap', page)[1:]:
        pid = re.search(r'data-post="[^"/]+/(\d+)"', chunk)
        dt = re.search(r'<time datetime="([^"]+)"', chunk)
        txt = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', chunk, re.S)
        if not (pid and dt and txt):
            continue
        body = re.sub(r"<br\s*/?>", "\n", txt.group(1))
        body = html.unescape(re.sub(r"<[^>]+>", "", body)).strip()
        ts = datetime.fromisoformat(dt.group(1)).timestamp()
        out.append((int(pid.group(1)), ts, body))
    return out


def fetch_history(channel, pages=20, pause=1.0):
    """Walk back through the channel; returns posts oldest first."""
    posts, before = {}, None
    for _ in range(pages):
        batch = parse_posts(fetch_page(channel, before))
        if not batch:
            break
        for p in batch:
            posts[p[0]] = p
        oldest = min(p[0] for p in batch)
        if before is not None and oldest >= before:
            break
        before = oldest
        time.sleep(pause)
    return [posts[k] for k in sorted(posts)]


def _f(x):
    return float(x.replace(",", "."))


def _nums(s):
    return [_f(x) for x in re.findall(NUM, s)]


def parse_setup(text):
    """Extract {ticker, side, entry, stop, tps} or None. Only numbers and keywords are read."""
    t = text.replace(" ", " ")
    low = t.lower()
    tick = re.search(r"\$([A-Z0-9]{2,15})\b", t) or re.search(r"\b([A-Z0-9]{2,12})USDT\b", t)
    if not tick:
        return None
    ticker = tick.group(1)

    long_kw = len(re.findall(r"\bлонг|от лонга|под лонг|в лонг", low))
    short_kw = len(re.findall(r"\bшорт|от шорта|под шорт|в шорт|для шорта", low))
    if long_kw == short_kw:
        return None
    side = "long" if long_kw > short_kw else "short"

    entry = None
    for pat in (r"цена\s+" + NUM, r"около\s+" + NUM, r"стоит у\s+" + NUM, r"от\s+" + NUM + r"\$?\s+до стопа"):
        m = re.search(pat, low)
        if m:
            entry = _f(m.group(1))
            break

    stop = None
    for pat in (r"стоп(?:\s+за)?\s+" + NUM, r"до стопа\s+" + NUM, r"стоп[^\n\d]{0,20}" + NUM):
        m = re.search(pat, low)
        if m:
            stop = _f(m.group(1))
            break

    tps = []
    m = re.search(r"(?:цели|тейки)\s*:?\s*((?:" + NUM + r"\$?\s*(?:/|,|и)?\s*){1,4})", low)
    if m:
        tps = _nums(m.group(1))
    else:
        for pat in (r"(?:первая цель|первый тейк|ближайшая цель|ближайшая остановка|локальная цель[^\d]{0,15}|цель)\s*[-–:]?\s*" + NUM,):
            m = re.search(pat, low)
            if m:
                tps = [_f(m.group(1))]
        m2 = re.search(r"дальше\s+" + NUM, low)
        if tps and m2:
            tps.append(_f(m2.group(1)))
    if entry is None or stop is None or not tps:
        return None
    return {"ticker": ticker, "side": side, "entry": entry, "stop": stop, "tps": tps[:3]}


def valid(s, max_stop_pct=30.0):
    """Reject setups whose numbers contradict each other (typos, mixed-up coins)."""
    e, st, tps = s["entry"], s["stop"], s["tps"]
    sign = 1 if s["side"] == "long" else -1
    if sign * (e - st) <= 0:
        return False
    if any(sign * (tp - e) <= 0 for tp in tps):
        return False
    if abs(e - st) / e * 100 > max_stop_pct:
        return False
    if any(abs(tp - e) / e > 1.0 for tp in tps):          # target more than +100% away: likely a typo
        return False
    return True


def setups(posts, dedupe_h=24):
    """Parsed, validated and de-duplicated setups (reposts of the same idea are dropped)."""
    out, seen = [], {}
    for pid, ts, text in posts:
        s = parse_setup(text)
        if not s or not valid(s):
            continue
        key = (s["ticker"], s["side"], round(s["stop"], 10), round(s["tps"][0], 10))
        if key in seen and ts - seen[key] < dedupe_h * 3600:
            continue
        seen[key] = ts
        s.update(post_id=pid, ts=ts, text=text[:600])
        out.append(s)
    return out
