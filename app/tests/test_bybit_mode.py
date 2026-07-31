"""Bybit position mode — one-way (positionIdx 0) vs hedge (1 long / 2 short).

The property that matters most is negative: positionIdx must follow the
POSITION, never the order side. Closing a long in hedge mode is
`side=sell, positionIdx=1`; deriving the index from the order side gives 2,
Bybit reads the reduce-only sell against the SHORT book, and the close closes
nothing. Every helper takes pos_side for that reason, and the tests below pin
it at each call site rather than trusting the convention to hold.

Background: on 2026-07-09 a hedge-mode XAUT rejected S3's entry with 10001
"position idx not match position mode" and the long was simply missed.
"""
import pytest

import bybit_mode as B


# ── index selection ──────────────────────────────────────────────────────────
def test_one_way_is_always_zero():
    assert B.idx_for(B.ONE_WAY, "long") == 0
    assert B.idx_for(B.ONE_WAY, "short") == 0


@pytest.mark.parametrize("side,want", [
    ("long", 1), ("Buy", 1), ("buy", 1), ("LONG", 1),
    ("short", 2), ("Sell", 2), ("sell", 2), ("SHORT", 2),
])
def test_hedge_index_follows_the_position_side(side, want):
    assert B.idx_for(B.HEDGE, side) == want


def test_default_mode_is_one_way():
    """The bot's symbols really are one-way, so the common path stays a plain
    positionIdx 0 with no probing and no extra requests."""
    assert B.mode_of("NEVER/SEEN:USDT") == B.ONE_WAY
    assert B.idx("NEVER/SEEN:USDT", "short") == 0


# ── learning + persistence ───────────────────────────────────────────────────
def test_learn_persists_across_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(B, "STATE_FILE", str(tmp_path / "m.json"))
    monkeypatch.setattr(B, "_cache", {"loaded": False, "modes": {}})
    B.learn("XAUT/USDT:USDT", B.HEDGE)
    monkeypatch.setattr(B, "_cache", {"loaded": False, "modes": {}})   # "restart"
    assert B.mode_of("XAUT/USDT:USDT") == B.HEDGE
    assert B.idx("XAUT/USDT:USDT", "long") == 1


def test_learn_ignores_nonsense():
    B.learn("X/USDT:USDT", "sideways")
    assert B.mode_of("X/USDT:USDT") == B.ONE_WAY


@pytest.mark.parametrize("live_idx,want", [
    (0, B.ONE_WAY), ("0", B.ONE_WAY),
    (1, B.HEDGE), ("1", B.HEDGE), (2, B.HEDGE), ("2", B.HEDGE),
])
def test_learn_from_a_live_position_index(live_idx, want):
    B.learn_from_idx("L/USDT:USDT", live_idx)
    assert B.mode_of("L/USDT:USDT") == want


def test_learn_from_idx_ignores_junk():
    B.learn_from_idx("J/USDT:USDT", None)
    B.learn_from_idx("J/USDT:USDT", "")
    assert B.mode_of("J/USDT:USDT") == B.ONE_WAY


# ── the mismatch retry ───────────────────────────────────────────────────────
MISMATCH = ('bybit {"retCode":10001,"retMsg":"position idx not match position '
            'mode","result":{},"retExtInfo":{},"time":1783607414132}')


def test_is_mismatch_matches_on_the_message_not_the_code():
    """10001 is Bybit's GENERIC parameter error. Retrying on the bare code
    would resend orders that failed for entirely unrelated reasons."""
    assert B.is_mismatch(RuntimeError(MISMATCH)) is True
    assert B.is_mismatch(RuntimeError('{"retCode":10001,"retMsg":"qty invalid"}')) is False
    assert B.is_mismatch(RuntimeError("insufficient balance")) is False


def test_retry_flips_the_mode_and_succeeds():
    """The 2026-07-09 XAUT case: one wasted request instead of a missed long."""
    seen = []

    def send(pidx):
        seen.append(pidx)
        if pidx == 0:
            raise RuntimeError(MISMATCH)
        return "filled"

    assert B.send_with_mode("XAUT/USDT:USDT", "long", send) == "filled"
    assert seen == [0, 1]                       # one-way tried, then hedge long
    assert B.mode_of("XAUT/USDT:USDT") == B.HEDGE


def test_retry_uses_the_short_index_for_a_short():
    seen = []

    def send(pidx):
        seen.append(pidx)
        if pidx == 0:
            raise RuntimeError(MISMATCH)
        return "ok"

    B.send_with_mode("S/USDT:USDT", "short", send)
    assert seen == [0, 2]


def test_learned_mode_means_no_second_wasted_request():
    B.learn("K/USDT:USDT", B.HEDGE)
    seen = []
    B.send_with_mode("K/USDT:USDT", "long", lambda p: seen.append(p))
    assert seen == [1]                          # straight to hedge, no reject


def test_retry_also_works_from_hedge_back_to_one_way():
    """Modes change in both directions — a symbol switched back to one-way must
    re-learn, not keep sending index 1 forever."""
    B.learn("R/USDT:USDT", B.HEDGE)
    seen = []

    def send(pidx):
        seen.append(pidx)
        if pidx != 0:
            raise RuntimeError(MISMATCH)
        return "ok"

    B.send_with_mode("R/USDT:USDT", "long", send)
    assert seen == [1, 0]
    assert B.mode_of("R/USDT:USDT") == B.ONE_WAY


def test_unrelated_errors_are_never_retried():
    """A retry on a non-mode failure would place a SECOND live order."""
    seen = []

    def send(pidx):
        seen.append(pidx)
        raise RuntimeError("insufficient available balance")

    with pytest.raises(RuntimeError, match="insufficient"):
        B.send_with_mode("U/USDT:USDT", "long", send)
    assert len(seen) == 1                       # exactly one attempt


def test_a_failing_retry_propagates_rather_than_looping():
    def send(pidx):
        raise RuntimeError(MISMATCH)

    with pytest.raises(RuntimeError, match="position idx"):
        B.send_with_mode("V/USDT:USDT", "long", send)


# ── follower isolation ───────────────────────────────────────────────────────
def test_follower_modes_never_leak_between_accounts():
    """Copy-trading followers own their Bybit accounts and set their own modes.
    Learning hedge from ours must not aim orders at index 1 on theirs."""
    B.learn("ETH/USDT:USDT", B.HEDGE)                    # the owner's account
    assert B.mode_of(B.scope_key("follower-7", "ETH/USDT:USDT")) == B.ONE_WAY
    B.learn(B.scope_key("follower-7", "ETH/USDT:USDT"), B.HEDGE)
    assert B.mode_of(B.scope_key("follower-9", "ETH/USDT:USDT")) == B.ONE_WAY
    assert B.mode_of("ETH/USDT:USDT") == B.HEDGE         # ours is untouched
