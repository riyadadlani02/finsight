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
    assert track.backtest_insider(years=1, engine=engine, today=date(2026, 3, 1)) == 1
    (call,) = track.load_calls(engine, mode="backtest").itertuples()
    assert (call.kind, call.ticker, call.public_date) == ("insider_cluster", "ACME", date(2025, 11, 7))  # 10b5-1 buy excluded


def test_spread_between_low_and_high_change_filings():
    rng = np.random.default_rng(2)
    rows = [{"direction": d, "public_date": date(2024, m, 15), "status_90": "graded",
             "excess_90": 0.05 * d + rng.normal(0, 0.01)} for m in range(1, 9) for d in (1, -1, 1, -1)]
    s = track.spread(pd.DataFrame(rows), 90)
    assert abs(s["spread"] - 0.10) < 0.02 and s["lo"] < s["spread"] < s["hi"] and (s["n"], s["months"]) == (32, 8)
    assert track.spread(pd.DataFrame([r for r in rows if r["direction"] == 1]), 90) is None  # one side only
