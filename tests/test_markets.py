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


# ---------------------------------------------------------------- Japan (EDINET)

import io
import zipfile

import pytest

from core.markets import japan

CODES = ("ダウンロード実行日,2026年11月20日現在,件数,2件\n"
         "ＥＤＩＮＥＴコード,提出者種別,上場区分,連結の有無,資本金,決算日,提出者名,提出者名（英字）,提出者名（ヨミ）,所在地,提出者業種,証券コード,提出者法人番号\n"
         '"E02144","内国法人・組合","上場","有","635401","3月31日","トヨタ自動車株式会社","TOYOTA MOTOR CORPORATION","","","輸送用機器","72030",\n'
         '"E99999","内国法人・組合","非上場","無","1","3月31日","非上場株式会社","","","","サービス業","",\n')


def test_japan_resolve(monkeypatch):
    monkeypatch.setattr(japan, "_code_csv", lambda: CODES)
    japan._codes.cache_clear()
    c = markets.get("JP").resolve("7203.t")
    assert (c.ticker, c.name, c.issuer_id, c.market) == ("7203.T", "TOYOTA MOTOR CORPORATION", "E02144", "JP")
    assert markets.get("JP").resolve("9999") is None and markets.get("JP").resolve("TOYOTA") is None
    japan._codes.cache_clear()


def test_japan_needs_a_key(monkeypatch):
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    with pytest.raises(japan.JapanError, match="EDINET API key"):
        japan._api("documents.json", {"date": "2026-06-20", "type": 2})


def test_scan_indexes_annual_reports_once_per_day(engine, monkeypatch):
    calls = []
    def api(path, params):
        calls.append(params["date"])
        return {"results": [
            {"docID": "S100AAAA", "edinetCode": "E02144", "secCode": "72030", "filerName": "トヨタ自動車株式会社",
             "docTypeCode": "120", "submitDateTime": "2026-06-20 15:00", "periodEnd": "2026-03-31", "csvFlag": "1"},
            {"docID": "S100BBBB", "edinetCode": "E02144", "secCode": "72030", "filerName": "トヨタ自動車株式会社",
             "docTypeCode": "140", "submitDateTime": "2026-06-20 15:00", "periodEnd": "2026-06-30", "csvFlag": "1"}]}
    monkeypatch.setattr(japan, "_api", api)
    assert japan.scan(3, engine=engine, today=date(2026, 6, 22)) == 1
    assert japan.scan(3, engine=engine, today=date(2026, 6, 22)) == 0  # the last two days are rescanned; older ones aren't
    assert calls == ["2026-06-22", "2026-06-21", "2026-06-20", "2026-06-22", "2026-06-21"]
    (r,) = japan.annual_reports("E02144", engine=engine)
    assert (r["doc_id"], r["filed"], r["period_end"]) == ("S100AAAA", date(2026, 6, 20), "2026-03-31")


def test_sections_from_edinet_csv():
    rows = ["要素ID\t項目名\tコンテキストID\t相対年度\t連結・個別\t期間・時点\tユニットID\t単位\t値",
            "jpcrp_cor:BusinessRisksTextBlock\t事業等のリスク\tFilingDateInstant\t提出日時点\tその他\t時点\t－\t－\t<p>為替の変動は業績に影響します。</p>",
            "jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock\t経営者による分析\tFilingDateInstant\t提出日時点\tその他\t時点\t－\t－\t<p>売上高は増加しました。</p>"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRL_TO_CSV/jpcrp030000-asr-001.csv", ("\n".join(rows)).encode("utf-16"))
    s = japan.parse_sections(buf.getvalue())
    assert s == {"Risk Factors": "為替の変動は業績に影響します。", "MD&A": "売上高は増加しました。"}


def test_edinet_errors_never_carry_the_key(monkeypatch):
    import requests
    monkeypatch.setenv("EDINET_API_KEY", "SECRET-KEY-123")
    def fail(url, params=None, **kw):
        raise requests.HTTPError(f"403 Client Error for url: {url}?Subscription-Key={params['Subscription-Key']}")
    monkeypatch.setattr(japan.requests, "get", fail)
    with pytest.raises(japan.JapanError) as err:
        japan._api("documents.json", {"date": "2026-06-20", "type": 2})
    assert "SECRET-KEY-123" not in str(err.value) and err.value.__cause__ is None and err.value.__suppress_context__


# ---------------------------------------------------------------- review fixes

def test_grade_skips_days_the_stocks_exchange_was_shut():
    days = pd.bdate_range("2025-11-20", "2026-03-31")
    closes = pd.DataFrame({"AAPL": 100.0, "SPY": 100.0, "RELIANCE.NS": 100.0, "NIFTYBEES.NS": 100.0}, index=days)
    closes.loc["2025-11-27", ["AAPL", "SPY"]] = np.nan  # Thanksgiving: NYSE shut, NSE open
    calls = pd.DataFrame([{"kind": "debate", "ticker": "AAPL", "market": "US", "direction": 1, "strength": 1.0,
                           "public_date": date(2025, 11, 26), "source_ref": "a", "mode": "live"}])
    g = track.grade(calls, closes, today=date(2026, 6, 30))
    assert g.loc[0, "entry"] == pd.Timestamp("2025-11-28") and g.loc[0, "status_30"] == "graded"


def test_scoreboard_never_mixes_markets():
    rows = [{"kind": "filing_change", "direction": 1, "mode": "backtest", "market": m, "status_30": "graded",
             "public_date": date(2025, 1 + i % 12, 5), "excess_30": 0.01, "hit_30": True} for m in ("US", "JP") for i in range(3)]
    board = track.scoreboard(pd.DataFrame(rows), 30)
    assert sorted(board["market"]) == ["JP", "US"] and (board["n"] == 3).all()


def test_japan_filings_hidden_until_reports_can_be_indexed(monkeypatch):
    assert "filings" not in markets.get("JP").tools  # no scan UI yet: say "not available", don't fail later
    monkeypatch.delenv("EDINET_API_KEY", raising=False)
    monkeypatch.setattr(japan, "resolve", lambda t: markets.Company("7203.T", "Toyota", "E02144", "JP"))
    with pytest.raises(japan.JapanError, match="EDINET API key"):
        japan.reports_with_sections("7203.T", 4)


def test_scan_leaves_recent_and_failed_days_for_later(engine, monkeypatch):
    def api(path, params):
        return {"metadata": {"status": "500"}, "results": []} if params["date"] == "2026-06-10" else {"metadata": {"status": "200"}, "results": []}
    monkeypatch.setattr(japan, "_api", api)
    japan.scan(15, engine=engine, today=date(2026, 6, 20))
    with engine.connect() as conn:
        done = {r[0] for r in conn.execute(db.jp_scanned.select())}
    assert date(2026, 6, 20) not in done and date(2026, 6, 19) not in done  # filings can still arrive
    assert date(2026, 6, 10) not in done and date(2026, 6, 15) in done


def test_sections_never_cache_an_empty_or_broken_report(monkeypatch, tmp_path):
    monkeypatch.setattr(japan, "DATA", tmp_path)
    monkeypatch.setattr(japan, "_api", lambda path, params, raw=False: b"not a zip")
    with pytest.raises(japan.JapanError):
        japan.sections("S100XXXX")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("x.csv", "要素ID\t値\njpcrp_cor:Other\tfoo".encode("utf-16"))
    monkeypatch.setattr(japan, "_api", lambda path, params, raw=False: buf.getvalue())
    with pytest.raises(japan.JapanError):
        japan.sections("S100YYYY")
    assert not list(tmp_path.rglob("*.json"))
