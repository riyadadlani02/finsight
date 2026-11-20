"""Offline tests for prices, grading, calls and backtests: no network, no API key."""
import zipfile
from datetime import date

import numpy as np
import pandas as pd

from core import bulk_insider, edgar, prices, track


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


# ---------------------------------------------------------------- SEC bulk insider data


def test_quarter_url_paths():
    base = "https://www.sec.gov/files/{}/data/insider-transactions-data-sets/{}_form345.zip"
    assert bulk_insider.quarter_url(2025, 4) == base.format("structureddata", "2025q4")
    assert bulk_insider.quarter_url(2026, 2) == base.format("datastandardsinnovation", "2026q2")


def test_download_quarter_skips_missing(tmp_path, monkeypatch):
    def missing(url):
        raise edgar.EdgarError(f"Not found: {url}")
    monkeypatch.setattr(edgar, "_get", missing)
    assert bulk_insider.download(2031, 1, dest=tmp_path) is None and not list(tmp_path.iterdir())


def test_load_quarter_joins_tables(tmp_path):
    path = tmp_path / "2025q4_form345.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SUBMISSION.tsv", "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\tAFF10B5ONE\n"
                                     "0001-25-1\t31-OCT-2025\t4\t0000000007\tAcme Corp\tacme\tfalse\n"
                                     "0001-25-2\t31-OCT-2025\t4/A\t0000000007\tAcme Corp\tACME\t0\n")
        z.writestr("REPORTINGOWNER.tsv", "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
                                         "0001-25-1\t0000000099\tDoe Jane\tDirector,Officer\tCFO\n"
                                         "0001-25-2\t0000000098\tRoe Rick\tDirector\t\n")
        z.writestr("NONDERIV_TRANS.tsv", "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\n"
                                         "0001-25-1\t29-OCT-2025\tP\t1000\t25.5\tA\n"
                                         "0001-25-1\t29-OCT-2025\tS\t500\t26\tD\n"
                                         "0001-25-2\t29-OCT-2025\tP\t10\t25\tA\n")
    df = bulk_insider.load_quarter(path)
    assert len(df) == 1  # the sale and the amendment are left out
    row = df.iloc[0]
    assert (row["ticker"], row["issuer_cik"], row["insider_cik"], row["officer_title"]) == ("ACME", "0000000007", "0000000099", "CFO")
    assert row["is_director"] and row["is_officer"] and not row["is_ten_pct"] and not row["planned_10b5_1"]
    assert row["value"] == 25_500 and row["filed"] == date(2025, 10, 31) and row["tx_date"] == pd.Timestamp("2025-10-29")


# ---------------------------------------------------------------- backtests

def test_backtest_filings_quintiles(engine, monkeypatch):
    from core import changes
    scores = {f"T{i}": i * 10 for i in range(10)}  # combined change scores 0..90, all filed the same year
    monkeypatch.setattr(changes, "score_company", lambda t, years=6, engine=None: [
        changes.YearChange(f"acc-{t}", "prev", "2024-02-01", {"MD&A": scores[t]}, scores[t])])
    assert track.backtest_filings(list(scores), engine=engine) == 10
    calls = track.load_calls(engine, mode="backtest").set_index("ticker")["direction"]
    assert calls[["T8", "T9"]].tolist() == [-1, -1] and calls[["T0", "T1"]].tolist() == [1, 1]
    assert (calls.drop(["T0", "T1", "T8", "T9"]) == 0).all()
    assert track.filing_cutoffs(engine) == (18.0, 72.0)


def test_backtest_insider_records_cluster_events(engine, monkeypatch):
    def buy(n, tx, filed, planned=False):
        return dict(accession=f"a{n}", issuer_cik="0000000007", ticker="ACME", issuer_name="Acme", insider_cik=str(n),
                    insider_name=f"I{n}", officer_title="", tx_date=pd.Timestamp(tx), value=60_000.0,
                    planned_10b5_1=planned, filed=date.fromisoformat(filed))
    rows = pd.DataFrame([buy(1, "2025-11-03", "2025-11-04"), buy(2, "2025-11-04", "2025-11-05"),
                         buy(3, "2025-11-05", "2025-11-06", planned=True), buy(4, "2025-11-06", "2025-11-07")])
    monkeypatch.setattr(bulk_insider, "download", lambda y, q, dest=None: f"{y}q{q}" if (y, q) == (2025, 4) else None)
    monkeypatch.setattr(bulk_insider, "load_quarter", lambda path: rows)
    result = track.backtest_insider(years=1, engine=engine, today=date(2026, 3, 1))
    assert result["recorded"] == 1 and "2025 Q4" not in result["skipped"] and "2025 Q3" in result["skipped"]
    (call,) = track.load_calls(engine, mode="backtest").itertuples()
    assert (call.kind, call.ticker, call.public_date) == ("insider_cluster", "ACME", date(2025, 11, 7))  # 10b5-1 buy excluded


def test_spread_between_low_and_high_change_filings():
    rng = np.random.default_rng(2)
    rows = [{"direction": d, "public_date": date(2024, m, 15), "status_90": "graded",
             "excess_90": 0.05 * d + rng.normal(0, 0.01)} for m in range(1, 9) for d in (1, -1, 1, -1)]
    s = track.spread(pd.DataFrame(rows), 90)
    assert abs(s["spread"] - 0.10) < 0.02 and s["lo"] < s["spread"] < s["hi"] and (s["n"], s["months"]) == (32, 8)
    assert track.spread(pd.DataFrame([r for r in rows if r["direction"] == 1]), 90) is None  # one side only


# ---------------------------------------------------------------- cockpit

from sqlalchemy import insert

from core import cockpit, db


def _fake_prices(monkeypatch):
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    monkeypatch.setattr(prices, "closes", lambda tickers, start, engine=None, fetch=None:
                        pd.DataFrame({tickers[0]: np.linspace(100, 130, 300)}, index=idx))
    monkeypatch.setattr(edgar, "lookup", lambda t: {"cik": "0000000042", "name": "Acme <b>Corp</b>"})


def test_clean_ticker():
    assert cockpit.clean_ticker(" aapl ") == "AAPL" and cockpit.clean_ticker("BRK.B") == "BRK.B"
    assert cockpit.clean_ticker("<script>") is None and cockpit.clean_ticker("") is None
    assert cockpit.clean_ticker("relianceind.ns") == "RELIANCEIND.NS" and cockpit.clean_ticker("130A.T") == "130A.T"


def test_card_data_empty_states(engine, monkeypatch):
    _fake_prices(monkeypatch)
    d = cockpit.card_data("ACME", engine=engine)
    assert d["cards"] == {"said": None, "insiders": None, "deals": None, "filing": None, "record": None}
    assert (d["market"], d["symbol"]) == ("US", "$") and "insider" in d["tools"]
    assert d["name"] == "Acme <b>Corp</b>"  # raw: the component sets it as text, never as HTML
    assert d["last"] == 130.0 and len(d["daily_1y"]) == 252 and d["chg_1y"] > 0


def test_card_data_fills_cards(engine, monkeypatch):
    _fake_prices(monkeypatch)
    today = date.today()
    with engine.begin() as conn:
        conn.execute(insert(db.promises).values(ticker="ACME", latest="acc-26", pid="p1", promise="Cut debt",
                                                verdict="kept", made_filed="2024-02-01"))
        conn.execute(insert(db.insider_tx), [dict(accession=f"a{i}", line_no=0, issuer_cik="0000000042", ticker="ACME",
                                                  insider_cik=str(i), insider_name=f"I{i}", tx_date=today, code="P",
                                                  acquired=True, value=50_000.0) for i in (1, 2)])
        conn.execute(insert(db.filing_changes), [dict(issuer_cik="0000000042", ticker="ACME", accession=a, filed=f,
                                                      section="MD&A", change_score=s)
                                                 for a, f, s in (("x1", date(2025, 2, 1), 10), ("x2", date(2026, 2, 1), 30))])
    track.record("debate", "ACME", -1, 10, today, "1", engine=engine)
    cards = cockpit.card_data("ACME", engine=engine)["cards"]
    assert (cards["said"]["judged"], cards["said"]["kept"], cards["said"]["too_few"]) == (1, 1, True)
    assert (cards["insiders"]["insiders"], cards["insiders"]["total"]) == (2, 100_000.0)
    assert cards["filing"]["combined"] == 30 and cards["filing"]["history"] == [10, 30]
    assert cards["record"]["calls"] == 1


# ---------------------------------------------------------------- review fixes

def test_closes_keeps_cache_when_refetch_fails(engine):
    good = lambda tickers, start: pd.DataFrame({"AAA": [10.0, 11.0]}, index=pd.bdate_range("2026-01-05", periods=2))
    prices.closes(["AAA"], date(2026, 1, 1), engine=engine, fetch=good)
    with engine.begin() as conn:  # pretend that was yesterday
        conn.execute(db.prices.update().values(fetched=date(2000, 1, 1)))
        conn.execute(db.price_fetches.update().values(fetched=date(2000, 1, 1)))
    out = prices.closes(["AAA"], date(2026, 1, 1), engine=engine, fetch=lambda t, s: pd.DataFrame())  # Yahoo failed
    assert out["AAA"].tolist() == [10.0, 11.0]


def test_closes_late_listing_fetched_once(engine):
    calls = []
    def fetch(tickers, start):
        calls.append(tickers)
        return pd.DataFrame({"NEW": [5.0, 6.0]}, index=pd.bdate_range("2026-03-02", periods=2))  # listed in March
    for _ in range(2):
        prices.closes(["NEW"], date(2026, 1, 1), engine=engine, fetch=fetch)
    assert len(calls) == 1


def test_download_quarter_survives_network_errors(tmp_path, monkeypatch):
    import requests
    def down(url):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(edgar, "_get", down)
    assert bulk_insider.download(2025, 4, dest=tmp_path) is None


def test_scoreboard_median_interval_and_spread_too_few():
    rng = np.random.default_rng(3)
    graded = pd.DataFrame({"kind": "debate", "direction": 1, "mode": "live", "status_30": "graded",
                           "public_date": [date(2026, m, 10) for m in range(1, 9) for _ in range(4)],
                           "excess_30": rng.normal(0.01, 0.05, 32)})
    graded["hit_30"] = graded["excess_30"] > 0
    row = track.scoreboard(graded, 30).iloc[0]
    assert row["median_lo"] < row["median"] < row["median_hi"]
    few = graded.iloc[:16]  # 16 calls a side over 4 months
    assert track.spread(pd.concat([few, few.assign(direction=-1)], ignore_index=True), 30)["too_few"]
    both = pd.concat([graded, graded.assign(direction=-1)], ignore_index=True)  # 32 a side over 8 months
    assert not track.spread(both, 30)["too_few"]


def test_source_url_points_at_the_exact_source():
    assert track.source_url({"kind": "debate", "ticker": "ACME", "source_ref": "7"}) == "/Bull_vs_Bear?ticker=ACME&debate=7"
    assert track.source_url({"kind": "filing_change", "ticker": "ACME", "issuer_cik": "0000000042",
                             "source_ref": "0001-26-000002"}) == \
        "https://www.sec.gov/Archives/edgar/data/42/000126000002/0001-26-000002-index.htm"
    url = track.source_url({"kind": "insider_cluster", "ticker": "ACME", "issuer_cik": "0000000042",
                            "source_ref": "0000000042:2026-11-02", "public_date": date(2026, 11, 9)})
    assert "CIK=0000000042" in url and "type=4" in url and "dateb=20261109" in url


def test_source_url_without_cik_from_a_dataframe_row():
    row = {"kind": "filing_change", "ticker": "ACME", "issuer_cik": np.nan, "source_ref": "0001-26-000002",
           "public_date": date(2026, 2, 1)}  # a missing CIK arrives as NaN from the calls table
    assert track.source_url(row) == "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=ACME&type=10-K"



def test_card_data_for_an_indian_stock(engine, monkeypatch):
    from core.markets import india
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=300)
    monkeypatch.setattr(prices, "closes", lambda tickers, start, engine=None, fetch=None:
                        pd.DataFrame({tickers[0]: np.linspace(2000, 2600, 300)}, index=idx))
    monkeypatch.setattr(india, "_equity_csv", lambda: "SYMBOL,NAME OF COMPANY\nRELIANCE,Reliance Industries Limited\n")
    india._equity_list.cache_clear()
    with engine.begin() as conn:
        conn.execute(insert(db.in_deals), [dict(date=date.today(), symbol="RELIANCE", name="Reliance", client=c, side="BUY",
                                                quantity=200_000, price=2600.0, kind="bulk") for c in ("FUND A", "FUND B")])
    d = cockpit.card_data("RELIANCE.NS", engine=engine)
    india._equity_list.cache_clear()
    assert (d["name"], d["market"], d["symbol"], d["last"]) == ("Reliance Industries Limited", "IN", "₹", 2600.0)
    assert "deals" in d["tools"] and "filings" not in d["tools"]
    assert d["cards"]["deals"]["net_crore"] == 104.0 and d["cards"]["deals"]["signals"] == 1


def test_clean_ticker_allows_ampersand_symbols():
    assert cockpit.clean_ticker("m&m.ns") == "M&M.NS" and cockpit.clean_ticker("J&KBANK.NS") == "J&KBANK.NS"
    assert "input.maxLength = 20" in cockpit.JS


def test_code_fingerprint_changes_when_code_changes(tmp_path):
    from core import ui
    a = tmp_path / "a.py"
    a.write_text("x = 1")
    first = ui.fingerprint([a])
    a.write_text("x = 2")
    assert ui.fingerprint([a]) != first and len(first) == 12 and len(ui.CODE_VERSION) == 12


def test_load_quarter_before_the_sec_added_10b5_1(tmp_path):
    # The SEC added AFF10B5ONE in 2023; older quarters don't have the column at all.
    path = tmp_path / "2022q2_form345.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("SUBMISSION.tsv", "ACCESSION_NUMBER\tFILING_DATE\tDOCUMENT_TYPE\tISSUERCIK\tISSUERNAME\tISSUERTRADINGSYMBOL\n"
                                     "0001-22-1\t15-APR-2022\t4\t0000000007\tAcme Corp\tACME\n")
        z.writestr("REPORTINGOWNER.tsv", "ACCESSION_NUMBER\tRPTOWNERCIK\tRPTOWNERNAME\tRPTOWNER_RELATIONSHIP\tRPTOWNER_TITLE\n"
                                         "0001-22-1\t0000000099\tDoe Jane\tDirector\t\n")
        z.writestr("NONDERIV_TRANS.tsv", "ACCESSION_NUMBER\tTRANS_DATE\tTRANS_CODE\tTRANS_SHARES\tTRANS_PRICEPERSHARE\tTRANS_ACQUIRED_DISP_CD\n"
                                         "0001-22-1\t13-APR-2022\tP\t1000\t20\tA\n")
    (row,) = bulk_insider.load_quarter(path).itertuples()
    assert row.ticker == "ACME" and row.value == 20_000 and not row.planned_10b5_1
