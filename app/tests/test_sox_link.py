"""費半 → 台積電, measured rather than asserted.

The 💡 line used to claim a strong 費半 close was "偏正向" for 台股電子 with no
number behind it. This measures it — and always alongside the unconditional
base rate, because a hit rate without its base is not evidence. (Real 2-year
bars on first run: base 52% of 485 days, 費半 ≥ +2% → 78% of 102 days,
費半 ≤ −2% → 9% of 82.)
"""
import us_market as um

DAY = 86400
T0 = 1700000000          # a Monday-ish anchor; only ordering matters


def _sox(changes):
    """Daily SOX closes that produce `changes` (percent) day over day."""
    rows, close = [(T0, 100.0, 100.0)], 100.0
    for i, ch in enumerate(changes, start=1):
        close = close * (1 + ch / 100)
        rows.append((T0 + i * DAY, close, close))
    return rows


def _tw(gaps):
    """2330 daily bars where `gaps[i]` says whether day i+1 opened above the
    previous close. Each TW day is one day AFTER the matching SOX day."""
    rows, close = [(T0 + DAY // 2, 100.0, 100.0)], 100.0
    for i, up in enumerate(gaps, start=1):
        opn = close * (1.01 if up else 0.99)
        rows.append((T0 + i * DAY + DAY // 2, opn, opn))
        close = opn
    return rows


def test_counts_hits_against_the_previous_tw_close():
    link = um.measure_link(_sox([3.0, 3.0, 3.0]), _tw([True, True, False]),
                           threshold=2.0)
    assert link["up"]["n"] == 3 and link["up"]["hits"] == 2
    assert link["up"]["pct"] == 67


def test_down_side_is_measured_separately():
    link = um.measure_link(_sox([-3.0, -3.0]), _tw([False, False]), threshold=2.0)
    assert link["down"]["n"] == 2 and link["down"]["pct"] == 0
    assert link["up"]["n"] == 0


def test_moves_inside_the_threshold_are_excluded():
    link = um.measure_link(_sox([0.5, -0.5, 1.9]), _tw([True, True, True]),
                           threshold=2.0)
    assert link["up"]["n"] == 0 and link["down"]["n"] == 0
    assert link["base"]["n"] == 3          # base still counts every TW day


def test_base_rate_is_unconditional():
    link = um.measure_link(_sox([3.0, 3.0]), _tw([True, False]), threshold=2.0)
    assert link["base"]["n"] == 2 and link["base"]["pct"] == 50


def test_empty_input_does_not_raise():
    link = um.measure_link([], [], threshold=2.0)
    assert link["up"]["n"] == 0 and link["base"]["pct"] is None


# ── the sentence ─────────────────────────────────────────────────────────────
LINK = {"threshold": 2.0, "base": {"hits": 253, "n": 485, "pct": 52},
        "up": {"hits": 80, "n": 102, "pct": 78},
        "down": {"hits": 7, "n": 82, "pct": 9}}


def _snap(sox_pct):
    return {"indices": [{"symbol": "^SOX", "label": "費城半導體",
                         "price": 11000.0, "chg_pct": sox_pct}]}


def test_sentence_carries_sample_size_AND_base_rate():
    s = um.link_sentence(_snap(3.0), LINK)
    assert "102 次" in s and "78%" in s and "52%" in s


def test_sentence_uses_the_down_sample_for_a_down_day():
    s = um.link_sentence(_snap(-3.0), LINK)
    assert "跌逾" in s and "82 次" in s and "9%" in s


def test_silent_when_today_is_an_ordinary_day():
    assert um.link_sentence(_snap(0.4), LINK) == ""


def test_silent_when_the_measurement_is_unavailable():
    """No claim is better than an unmeasured one."""
    assert um.link_sentence(_snap(3.0), {}) == ""
    assert um.link_sentence(_snap(3.0), None) == ""


def test_silent_when_sox_is_missing():
    assert um.link_sentence({"indices": []}, LINK) == ""


def test_never_predicts_or_advises():
    for pct in (3.0, -3.0):
        s = um.link_sentence(_snap(pct), LINK)
        for banned in ("會", "必", "建議", "買進", "賣出", "保證", "預期"):
            assert banned not in s, f"{banned} in {s}"


def test_session_dates_are_keyed_in_utc_for_both_series():
    """The bug that survived every synthetic test above.

    Yahoo stamps a daily bar at its market's LOCAL open: 09:30 New York and
    09:00 台北. Keying on New York dates shifts every 台股 bar back a day, so TW
    sessions pair with the wrong US close — which moved the measured hit rate
    from 78% to 56% while leaving the SAMPLE SIZES almost identical. Sample
    counts are not a check on this; only the rate moves.
    """
    ny_open = 1706625000        # 2024-01-30 09:30 New York  → 14:30 UTC
    tw_open = 1706575800        # 2024-01-30 09:00 台北       → 2024-01-30 01:00 UTC
    assert um._d(ny_open) == "2024-01-30"
    assert um._d(tw_open) == "2024-01-30"      # NOT 2024-01-29


def test_a_tw_session_pairs_with_the_us_close_that_preceded_it():
    """End to end on realistic stamps: US Tue close → TW Wed open."""
    us_tue = 1706625000                     # Tue 2024-01-30, 14:30 UTC
    tw_wed = us_tue + 10 * 3600 + 3600      # Wed 2024-01-31, 01:30 UTC
    tw_tue = tw_wed - 86400                 # Tue 2024-01-30, 01:30 UTC
    sox = [(us_tue - 86400, 100.0, 100.0), (us_tue, 105.0, 105.0)]   # +5%
    tw = [(tw_tue, 50.0, 50.0), (tw_wed, 51.0, 51.0)]               # opened up
    link = um.measure_link(sox, tw, threshold=2.0)
    assert link["up"]["n"] == 1 and link["up"]["hits"] == 1
