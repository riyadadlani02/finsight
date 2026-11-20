"""Offline tests: no network, no API key. DB tests use TEST_DATABASE_URL or in-memory SQLite."""
import io
import os
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine, insert

from core import db, debate, edgar, insider
from core.mule import data, gnn, graph

FORM4 = """<SEC-DOCUMENT>junk<XML>
<ownershipDocument>
  <aff10b5One>0</aff10b5One>
  <issuer><issuerCik>0000123456</issuerCik><issuerName>Acme Corp</issuerName><issuerTradingSymbol>acme</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0000999</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>true</isDirector><isOfficer>1</isOfficer><officerTitle>Chief Executive Officer</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-02</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>25.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
      <ownershipNature><directOrIndirectOwnership><value>D</value></directOrIndirectOwnership></ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-11-03</value></transactionDate>
      <transactionCoding><transactionCode>F</transactionCode></transactionCoding>
      <transactionAmounts><transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><footnoteId id="F1"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument></XML></SEC-DOCUMENT>"""


def test_parse_form4():
    rows = edgar.parse_form4(FORM4, "0001-26-000001")
    assert len(rows) == 2
    buy, tax = rows
    assert (buy["ticker"], buy["issuer_cik"], buy["insider_name"]) == ("ACME", "0000123456", "Doe Jane")
    assert buy["is_director"] and buy["is_officer"] and not buy["is_ten_pct"] and not buy["planned_10b5_1"]
    assert buy["code"] == "P" and buy["acquired"] and buy["value"] == 25500 and buy["tx_date"] == date(2026, 11, 2)
    assert tax["code"] == "F" and not tax["acquired"] and tax["price"] is None and tax["value"] == 0
    assert edgar.parse_form4("not a form 4", "x") == []


def test_extract_section_skips_table_of_contents():
    text = ("\nItem 1A. Risk Factors 12\nItem 1B. Unresolved 30\n" + "filler\n" * 20
            + "\nItem 1A. Risk Factors\n" + "Real risk text. " * 100 + "\nItem 1B. Unresolved Staff Comments\nnone")
    section = edgar.extract_section(text, r"Item\s*1A\.\s*Risk\s+Factors", (r"Item\s*1B",), 10_000)
    assert section.count("Real risk text.") == 100 and "Unresolved" not in section
    assert edgar.extract_section(text, r"Item\s*1A\.\s*Risk\s+Factors", (r"Item\s*1B",), 50).endswith("truncated ...]")


def test_daily_index_and_facts(monkeypatch):
    idx = ("Form Type   Company Name   CIK   Date Filed  File Name\n---\n"
           "4           Acme Corp      123456  20261102  edgar/data/123456/0001-26-000001.txt\n"
           "4           Doe Jane       999     20261102  edgar/data/999/0001-26-000001.txt\n"
           "4/A         Other          5       20261102  edgar/data/5/0009-26-000009.txt\n"
           "424B2       Bank           6       20261102  edgar/data/6/0008-26-000008.txt\n")
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            {"start": "2024-01-01", "end": "2024-12-31", "val": 100, "form": "10-K"},
            {"start": "2025-01-01", "end": "2025-12-31", "val": 120, "form": "10-K"},
            {"start": "2025-10-01", "end": "2025-12-31", "val": 35, "form": "10-K"},   # quarter: ignored
            {"start": "2025-01-01", "end": "2025-03-31", "val": 28, "form": "10-Q"}]}},
        "Assets": {"units": {"USD": [{"end": "2025-12-31", "val": 900, "form": "10-K"}]}}}}}

    class Resp:
        def __init__(self, payload): self.text, self._p = payload if isinstance(payload, str) else "", payload
        def json(self): return self._p

    monkeypatch.setattr(edgar, "_get", lambda url: Resp(idx if url.endswith(".idx") else facts))
    assert edgar.daily_form4_index(date(2026, 11, 2)) == [
        ("0001-26-000001", "https://www.sec.gov/Archives/edgar/data/123456/0001-26-000001.txt")]
    out = edgar.annual_facts("0000123456")
    assert out["Revenue"] == {"2024-12-31": 100, "2025-12-31": 120} and out["Total assets"] == {"2025-12-31": 900}


@pytest.fixture
def engine():
    eng = create_engine(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
    db.metadata.drop_all(eng)
    db.metadata.create_all(eng)
    yield eng
    db.metadata.drop_all(eng)


def _buy(n, issuer, insider_id, days_ago, value, title="", planned=False, code="P"):
    return dict(accession=f"acc-{n}", line_no=0, issuer_cik=issuer, issuer_name=f"Co {issuer}", ticker=f"T{issuer}",
                insider_cik=str(insider_id), insider_name=f"Insider {insider_id}", is_director=True, is_officer=bool(title),
                is_ten_pct=False, officer_title=title, tx_date=date.today() - timedelta(days=days_ago), code=code,
                acquired=code == "P", shares=value / 10, price=10.0, value=value, shares_after=0.0, ownership="D",
                planned_10b5_1=planned)


def test_cluster_detection(engine):
    rows = [
        _buy(1, "1", 1, 3, 200_000, "Chief Executive Officer"), _buy(2, "1", 2, 5, 80_000), _buy(3, "1", 3, 10, 50_000),
        _buy(4, "2", 4, 2, 500_000), _buy(5, "2", 4, 4, 500_000),            # one insider twice: not a cluster
        _buy(6, "3", 5, 1, 90_000), _buy(7, "3", 6, 50, 90_000), _buy(8, "3", 7, 100, 90_000),  # too spread out
        _buy(9, "4", 8, 2, 90_000), _buy(10, "4", 9, 3, 90_000), _buy(11, "4", 10, 4, 90_000, planned=True),  # 10b5-1
        _buy(12, "5", 11, 2, 9e6, code="S"), _buy(13, "5", 12, 2, 9e6, code="S"), _buy(14, "5", 13, 2, 9e6, code="S"),
    ]
    with engine.begin() as conn:
        conn.execute(insert(db.insider_tx), rows)
    clusters = insider.find_clusters(insider.load_purchases(engine), window_days=30, min_insiders=3)
    assert list(clusters["issuer_cik"]) == ["1"]
    c = clusters.iloc[0]
    assert (c.insiders, c.top_execs, c.total_value, c.trades) == (3, 1, 330_000, 3) and 0 < c.score <= 100
    assert "Insider 1 (Chief Executive Officer)" in c.names
    # counting 10b5-1 trades brings company 4 in
    both = insider.find_clusters(insider.load_purchases(engine, exclude_10b5_1=False))
    assert set(both["issuer_cik"]) == {"1", "4"}
    assert insider.find_clusters(insider.load_purchases(engine), min_total_value=1e6).empty


def test_ingest_is_idempotent(engine, monkeypatch):
    monkeypatch.setattr(edgar, "daily_form4_index", lambda day: [("0001-26-000001", "u")] if day == date.today() else [])
    monkeypatch.setattr(edgar, "fetch_form4", lambda url, acc: edgar.parse_form4(FORM4, acc))
    assert insider.scan_market(2, engine) == 2
    assert insider.scan_market(2, engine) == 0


def test_debate_dossier_and_history(engine, monkeypatch):
    f10k = edgar.Filing("0000123456", "0001-26-000002", "10-K", "2026-02-01", "2025-12-31", "a.htm")
    f10q = edgar.Filing("0000123456", "0001-26-000003", "10-Q", "2026-05-01", "2026-03-31", "b.htm")
    monkeypatch.setattr(edgar, "lookup", lambda t: {"cik": "0000123456", "name": "Acme Corp"})
    monkeypatch.setattr(edgar, "recent_filings", lambda *a, **k: [f10q, f10k])
    monkeypatch.setattr(edgar, "filing_sections", lambda f, n: {"MD&A": f"{f.form} discussion"})
    monkeypatch.setattr(edgar, "annual_facts", lambda cik: {"Revenue": {"2025-12-31": 1.2e9}, "Diluted EPS": {"2025-12-31": 3.1}})
    monkeypatch.setattr(debate, "market_snapshot", lambda t: {"Last close": 10.0})
    d = debate.build_dossier("acme")
    assert d["ticker"] == "ACME" and [f.form for f in d["sources"]] == ["10-K", "10-Q"]
    for needle in ("[10-K MD&A]", "[10-Q MD&A]", "FY ending 2025-12-31 = 1.20B", "= 3.10", "Last close: 10.0"):
        assert needle in d["text"]

    monkeypatch.setattr(db, "engine", lambda url=None: engine)
    verdict = {"winner": "bear", "bull_score": 60, "bear_score": 70}
    debate.save(d, [{"speaker": "bull", "round": "opening", "text": "hi"}], verdict)
    assert debate.history()[0]["winner"] == "bear"


def test_mule_pipeline_finds_planted_rings():
    tx, accounts = data.generate(n_accounts=1500, n_rings=5, seed=3)
    truth = accounts.set_index("account")
    feats = graph.account_features(tx)
    labels = data.account_labels(tx)
    assert (labels.reindex(feats.index) == truth["is_mule"].reindex(feats.index)).mean() > 0.99

    model, metrics = gnn.train(gnn.to_data(feats, tx, labels), epochs=100)
    assert metrics["avg_precision"] > 0.8
    risk = pd.Series(gnn.predict(model, gnn.to_data(feats, tx)), index=feats.index)
    G = graph.build_graph(tx)
    rings = graph.find_rings(G, risk, 0.5)
    found = rings.merge(accounts, on="account")
    assert found["is_mule"].mean() > 0.7                       # ring members are mostly real mules
    assert found.loc[found.is_mule == 1, "ring_id"].nunique() >= 4  # at least 4 of 5 planted rings surfaced
    summary = graph.ring_summary(G, rings, risk)
    assert (summary["money_in"] > 0).all()
    view = graph.ring_subgraph(G, rings, 0)
    assert {d["role"] for _, d in view.nodes(data=True)} >= {"mule", "payer"}


def test_load_csv_layouts():
    paysim = "step,type,amount,nameOrig,nameDest,isFraud\n1,TRANSFER,100.0,C1,C2,1\n2,CASH_OUT,90.0,C2,C3,0\n3,PAYMENT,0,C4,C5,0\n"
    tx = data.load_csv(io.StringIO(paysim))
    assert len(tx) == 2 and list(tx.columns) == data.COLUMNS
    assert tx.timestamp.diff().iloc[1] == pd.Timedelta(hours=1)
    assert data.account_labels(tx).to_dict() == {"C1": 0, "C2": 1, "C3": 0}

    ibm = ("Timestamp,From Bank,Account,To Bank,Account.1,Amount Received,Receiving Currency,Amount Paid,Payment Currency,Payment Format,Is Laundering\n"
           "2022/09/01 00:20,10,8000EBD30,20,8000F5340,3697.34,US Dollar,3697.34,US Dollar,Reinvestment,0\n")
    tx = data.load_csv(io.StringIO(ibm))
    assert tx.iloc[0].sender == "10:8000EBD30" and tx.iloc[0].receiver == "20:8000F5340" and tx.iloc[0].amount == 3697.34

    plain = data.load_csv(io.StringIO("from,to,value\na,b,5\nb,c,4\n"))
    assert data.account_labels(plain) is None and len(plain) == 2
    with pytest.raises(ValueError):
        data.load_csv(io.StringIO("x,y\n1,2\n"))
