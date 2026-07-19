"""📱 LINE push — the 台股 messages, delivered to family on LINE.

LINE Notify was shut down (2025-03-31), so this uses the LINE Messaging API
through a LINE Official Account instead. The design is deliberately the
simplest thing that works for "my dad reads it on his phone":

  · By default we BROADCAST — the message goes to everyone who has added
    the Official Account as a friend. No webhook, no user-ID discovery;
    dad just scans the QR code once. Keep the OA private (don't share the
    QR) and the audience is exactly the family.
  · Set LINE_TO (comma-separated userIds) to push to specific people
    instead of broadcasting.

Quota: the free plan allows 200 push messages/month, counted PER RECIPIENT.
One daily digest (~22 trading days) plus occasional SL/TP-hit alerts fits
2–3 family members comfortably — which is why only tw_stocks (the 14:00
digest) and tw_intraday level hits go to LINE, not every mover alert.

Everything here is failure-safe: a LINE outage logs and returns False,
never raises into the scanner loop. `_post` is the single network
choke-point (stubbed by the tests' autouse guard).
"""
import time

import requests

import config

API = "https://api.line.me/v2/bot/message"
MAX_LEN = 4900          # LINE text-message limit is 5000 chars
BATCH = 5               # API limit: 5 message objects per call


def enabled() -> bool:
    return bool(config.LINE_CHANNEL_ACCESS_TOKEN)


def _post(path: str, payload: dict) -> tuple:
    """(status_code, body) — the only place that talks to LINE."""
    r = requests.post(
        f"{API}/{path}", json=payload, timeout=15,
        headers={"Authorization": f"Bearer {config.LINE_CHANNEL_ACCESS_TOKEN}"})
    return r.status_code, r.text


def _chunks(text: str) -> list:
    """Split on newline boundaries so no chunk exceeds MAX_LEN."""
    text = text.strip()
    if len(text) <= MAX_LEN:
        return [text]
    out, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + 1 + len(ln) > MAX_LEN:
            out.append(cur)
            cur = ln
        else:
            cur = f"{cur}\n{ln}" if cur else ln
        while len(cur) > MAX_LEN:          # single monster line
            out.append(cur[:MAX_LEN])
            cur = cur[MAX_LEN:]
    if cur:
        out.append(cur)
    return out


def send(text: str) -> bool:
    """Broadcast (or push, when LINE_TO is set) a plain-text message."""
    if not enabled() or not (text or "").strip():
        return False
    msgs = [{"type": "text", "text": c} for c in _chunks(text)]
    ok = True
    for i in range(0, len(msgs), BATCH):
        batch = msgs[i:i + BATCH]
        for to in (config.LINE_TO or [None]):
            path = "broadcast" if to is None else "push"
            payload = {"messages": batch} if to is None else {"to": to, "messages": batch}
            try:
                code, body = _post(path, payload)
                if code == 429:            # monthly quota is 4xx too — retry once only helps rate limits
                    time.sleep(2)
                    code, body = _post(path, payload)
                if code != 200:
                    print(f"[line] {path} failed {code}: {body[:200]}")
                    ok = False
            except Exception as exc:  # noqa: BLE001 — never kill the scanner
                print(f"[line] {path} error: {exc}")
                ok = False
    return ok
