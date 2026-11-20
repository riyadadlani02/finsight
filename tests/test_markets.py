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


# ---------------------------------------------------------------- India deals

BULK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
        "20-NOV-2026,ACME,Acme Ltd,ALPHA FUND,BUY,100000,300.00,-\n"        # ₹3 cr
        "20-NOV-2026,ACME,Acme Ltd,BETA CAPITAL,BUY,100000,300.00,-\n"      # ₹3 cr: two buyers, ₹6 cr net
        "20-NOV-2026,ACME,Acme Ltd,FAST TRADER LLP,BUY,500000,300.00,-\n"   # churner: bought and sold
        "20-NOV-2026,ACME,Acme Ltd,FAST TRADER LLP,SELL,500000,301.00,-\n"
        "20-NOV-2026,SOLO,Solo Ltd,ONE BIG FUND,BUY,1000000,100.00,-\n"     # ₹10 cr but only one buyer
        "20-NOV-2026,DUMP,Dump Ltd,SELLER ONE,SELL,300000,100.00,-\n"
        "20-NOV-2026,DUMP,Dump Ltd,SELLER TWO,SELL,300000,100.00,-\n")      # ₹6 cr net sold by two
BLOCK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price\n"
         "21-NOV-2026,ACME,Acme Ltd,GAMMA LIFE,BUY,200000,310.00\n"
         "21-NOV-2026,ACME,Acme Ltd,DELTA MF,BUY,200000,310.00\n")


def test_parse_deals():
    df = india.parse_deals(BULK, "bulk")
    assert len(df) == 7 and set(df["side"]) == {"BUY", "SELL"} and (df["kind"] == "bulk").all()
    row = df.iloc[0]
    assert (row["date"], row["symbol"], row["client"], row["value"]) == (date(2026, 11, 20), "ACME", "ALPHA FUND", 30_000_000.0)


def test_ingest_deals_is_idempotent(engine, monkeypatch):
    monkeypatch.setattr(india, "_get", lambda url: type("R", (), {"text": BULK if url.endswith("bulk.csv") else BLOCK})())
    assert india.ingest(engine=engine) == 9
    assert india.ingest(engine=engine) == 0
    assert len(india.load_deals(engine=engine)) == 9


def test_deal_signals():
    deals = pd.concat([india.parse_deals(BULK, "bulk"), india.parse_deals(BLOCK, "block")], ignore_index=True)
    sig = india.deal_signals(deals).set_index("symbol")
    assert sorted(sig.index) == ["ACME", "DUMP"]  # SOLO has one buyer; FAST TRADER's churn is ignored
    assert sig.loc["ACME", "direction"] == 1 and sig.loc["ACME", "date"] == date(2026, 11, 20)  # next day's buying is the same run
    assert sig.loc["ACME", "net_value"] == 60_000_000.0 and sig.loc["ACME", "churners"] == ["FAST TRADER LLP"]
    assert sig.loc["DUMP", "direction"] == -1 and sig.loc["DUMP", "ticker"] == "DUMP.NS"


def test_deal_signals_become_live_calls(engine, monkeypatch):
    monkeypatch.setattr(india, "_get", lambda url: type("R", (), {"text": BULK if url.endswith("bulk.csv") else BLOCK})())
    india.ingest(engine=engine)
    assert india.record_live_calls(engine=engine, today=date(2026, 11, 21)) == 2
    calls = track.load_calls(engine).set_index("ticker")
    assert calls.loc["ACME.NS", "market"] == "IN" and calls.loc["ACME.NS", "kind"] == "deal"
    assert calls.loc["DUMP.NS", "direction"] == -1 and calls.loc["ACME.NS", "source_ref"] == "ACME:2026-11-20"


def test_day_summary_shows_every_stock_and_why():
    s = india.day_summary(india.parse_deals(BULK, "bulk")).set_index("symbol")
    assert list(s.columns[:3]) == ["date", "name", "net_value"]
    assert (s.loc["ACME", "buyers"], s.loc["ACME", "churners"], s.loc["ACME", "signal"]) == (2, 1, 1)
    assert (s.loc["SOLO", "net_value"], s.loc["SOLO", "buyers"], s.loc["SOLO", "signal"]) == (100_000_000.0, 1, 0)
    assert s.loc["DUMP", "signal"] == -1
