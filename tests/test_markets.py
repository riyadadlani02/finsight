"""Offline tests for the market layer: registry, India and Japan adapters, per-market grading."""
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, inspect, text

from core import db, markets, track
from core.markets import india

EQUITY_L = ("SYMBOL,NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE\n"
            "RELIANCE,Reliance Industries Limited,EQ,29-NOV-1995,10,1,INE002A01018,10\n"
            "TCS,Tata Consultancy Services Limited,EQ,25-AUG-2004,1,1,INE467B01029,1\n")


def test_registry():
    us, inn, jp = markets.get("US"), markets.get("IN"), markets.get("JP")
    assert (us.currency, us.benchmark) == ("USD", "SPY")
    assert (inn.currency, inn.symbol, inn.benchmark, inn.benchmark_name) == ("INR", "₹", "NIFTYBEES.NS", "Nifty 50")
    assert (jp.currency, jp.symbol, jp.benchmark, jp.benchmark_name) == ("JPY", "¥", "1306.T", "TOPIX")
    assert "deals" in inn.tools and "insider" not in inn.tools and "insider" in us.tools and "insider" not in jp.tools


def test_market_of_ticker():
    assert [markets.of_ticker(t) for t in ("AAPL", "RELIANCE.NS", "RELIANCE.BO", "7203.T", "BRK-B")] == \
        ["US", "IN", "IN", "JP", "US"]


def test_india_resolve(monkeypatch, tmp_path):
    monkeypatch.setattr(india, "_equity_csv", lambda: EQUITY_L)
    india._equity_list.cache_clear()
    c = markets.get("IN").resolve(" reliance ")
    assert (c.ticker, c.name, c.issuer_id, c.market) == ("RELIANCE.NS", "Reliance Industries Limited", "RELIANCE", "IN")
    assert markets.get("IN").resolve("TCS.NS").ticker == "TCS.NS"
    assert markets.get("IN").resolve("NOTLISTED") is None
    india._equity_list.cache_clear()


def test_market_column_migration(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    with create_engine(url).begin() as conn:  # calls and filing_changes as they were before markets existed
        conn.execute(text("CREATE TABLE calls (id INTEGER PRIMARY KEY, kind VARCHAR(20), ticker VARCHAR(20), "
                          "direction INTEGER, public_date DATE, source_ref VARCHAR(80), mode VARCHAR(10))"))
        conn.execute(text("INSERT INTO calls (kind, ticker, direction, public_date, source_ref, mode) "
                          "VALUES ('debate', 'AAPL', 1, '2026-01-02', '1', 'live')"))
        conn.execute(text("CREATE TABLE filing_changes (id INTEGER PRIMARY KEY, accession VARCHAR(25), section VARCHAR(40))"))
    eng = db.engine(url)
    for table in ("calls", "filing_changes"):
        assert "market" in {c["name"] for c in inspect(eng).get_columns(table)}
    with eng.connect() as conn:
        assert conn.execute(text("SELECT market FROM calls")).scalar() == "US"


def test_grade_uses_each_markets_benchmark():
    days = pd.bdate_range("2026-01-01", periods=200)
    closes = pd.DataFrame({"RELIANCE.NS": 100 * 1.01 ** np.arange(200), "NIFTYBEES.NS": 100 * 1.01 ** np.arange(200),
                           "SPY": 100.0}, index=days)
    calls = pd.DataFrame([{"kind": "deal", "ticker": "RELIANCE.NS", "market": "IN", "direction": 1, "strength": 1.0,
                           "public_date": date(2026, 1, 2), "source_ref": "x", "mode": "live"}])
    g = track.grade(calls, closes, today=date(2026, 12, 31))
    assert abs(g.loc[0, "excess_30"]) < 1e-9  # moved exactly with the Nifty: no excess, even though SPY was flat
    assert set(track.benchmarks_for(calls)) == {"NIFTYBEES.NS"}
