"""Offline tests for prices, grading, calls and backtests: no network, no API key."""
from datetime import date

import numpy as np
import pandas as pd

from core import prices, track


def test_closes_caches_and_fills_missing(engine):
    calls = []

    def fetch(tickers, start):
        calls.append(sorted(tickers))
        return pd.DataFrame({"AAA": [10.0, 11.0, 12.0]}, index=pd.bdate_range("2026-01-02", periods=3))

    out = prices.closes(["AAA", "GONE"], date(2026, 1, 1), engine=engine, fetch=fetch)
    assert calls == [["AAA", "GONE"]] and list(out.columns) == ["AAA", "GONE"]
    assert out["AAA"].tolist() == [10.0, 11.0, 12.0] and out["GONE"].isna().all()
    prices.closes(["AAA", "GONE"], date(2026, 1, 1), engine=engine, fetch=fetch)
    assert len(calls) == 1  # same day: served from the cache, delisted ticker included


# ---------------------------------------------------------------- grading

DAYS = pd.bdate_range("2025-12-29", periods=320)
CLOSES = pd.DataFrame({"XYZ": 100 * 1.01 ** np.arange(len(DAYS)), "SPY": 100.0, "GONE": np.nan}, index=DAYS)


def _calls(rows):
    base = {"kind": "debate", "strength": 1.0, "mode": "live", "source_ref": "x"}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_grade_uses_first_close_after_public_date():
    calls = _calls([{"ticker": "XYZ", "direction": 1, "public_date": date(2026, 1, 2)},
                    {"ticker": "XYZ", "direction": -1, "public_date": date(2026, 1, 2)}])
    g = track.grade(calls, CLOSES, today=date(2026, 12, 31))
    assert (g["entry"] == pd.Timestamp("2026-01-05")).all()  # Friday news, Monday close
    assert (g["excess_30"] > 0).all() and g["hit_30"].tolist() == [True, False]
    assert (g["status_180"] == "graded").all()


def test_grade_pending_before_horizon():
    g = track.grade(_calls([{"ticker": "XYZ", "direction": 1, "public_date": date(2026, 1, 2)}]), CLOSES,
                    today=date(2026, 1, 15))
    assert g.loc[0, "status_30"] == "pending" and pd.isna(g.loc[0, "excess_30"])


def test_grade_marks_missing_prices():
    calls = _calls([{"ticker": "GONE", "direction": 1, "public_date": date(2026, 1, 2)},
                    {"ticker": "XYZ", "direction": 1, "public_date": date(2026, 1, 2)}])
    g = track.grade(calls, CLOSES, today=date(2026, 12, 31))
    assert g.loc[0, "status_30"] == "no_price" and g.loc[1, "status_30"] == "graded"
    board = track.scoreboard(g, 30)
    assert board["dropped"].sum() == 1 and board["n"].sum() == 1


def test_record_live_window(engine):
    kw = dict(kind="debate", ticker="XYZ", direction=1, strength=6, public_date=date(2026, 1, 2), engine=engine)
    assert track.record(source_ref="1", today=date(2026, 1, 5), **kw)       # by the entry close: live
    assert not track.record(source_ref="2", today=date(2026, 1, 6), **kw)   # too late to be live
    assert not track.record(source_ref="1", today=date(2026, 1, 5), **kw)   # duplicate
    assert len(track.load_calls(engine)) == 1


def test_scoreboard_too_few_and_block_intervals():
    rng = np.random.default_rng(1)
    one_month = pd.DataFrame({"kind": "debate", "direction": 1, "mode": "live", "status_30": "graded",
                              "public_date": [date(2026, 3, 1 + i % 28) for i in range(25)],
                              "excess_30": rng.normal(0.02, 0.05, 25)})
    one_month["hit_30"] = one_month["excess_30"] > 0
    row = track.scoreboard(one_month, 30).iloc[0]
    assert row["n"] == 25 and row["months"] == 1 and row["too_few"]

    # Calls bunched in three months that each share a shock: month blocks must widen the interval.
    shocks = {1: 0.10, 2: -0.10, 3: 0.0}
    months = pd.Series([m for m in shocks for _ in range(8)])
    values = pd.Series([shocks[m] + rng.normal(0, 0.01) for m in months])
    lo, hi = track.block_ci(values, months)
    clo, chi = track.block_ci(values, pd.Series(range(len(values))))  # each call its own block
    assert hi - lo > 2 * (chi - clo)
    assert all(np.isnan(track.block_ci(values, pd.Series([1] * len(values)))))
