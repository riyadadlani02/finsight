"""Offline tests for prices, grading, calls and backtests: no network, no API key."""
from datetime import date

import pandas as pd

from core import prices


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
