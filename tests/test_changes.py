"""Offline tests for 10-K change scoring: no network, no API key."""
from datetime import date

import pytest
from sqlalchemy import func, select

from core import changes, db, edgar

BASE = ["Supply chain disruption in Asia could reduce our ability to ship products on time to customers worldwide.",
        "We depend on a small number of carriers and contract manufacturers for most of our hardware output.",
        "Changes in foreign exchange rates may reduce reported revenue when the US dollar strengthens."]


def test_identical_sections_score_zero():
    d = changes.diff_sections("\n\n".join(BASE), "\n\n".join(BASE))
    assert d.change_score == 0 and d.counts == {"added": 0, "removed": 0, "reworded": 0, "unchanged": 3}


def test_added_removed_and_reworded():
    new = [BASE[0],
           "We depend on a small number of carriers and contract manufacturers, and a single supplier for our chips.",
           "Proposed tariffs on imported semiconductors could sharply raise component costs and compress margins."]
    d = changes.diff_sections("\n\n".join(BASE), "\n\n".join(new))
    assert d.counts == {"added": 1, "removed": 1, "reworded": 1, "unchanged": 1}
    assert 0 < d.change_score < 100 and [p.id for p in d.paras if p.kind == "removed"] == ["r1"]


def test_redline_html_escapes():
    out = changes.redline_html("risk <b>old</b>", "risk <script>x</script>")
    assert "<script>" not in out and "&lt;script&gt;" in out and "<ins>" in out and "<del>" in out


# ---------------------------------------------------------------- EDGAR-backed scoring

RISK = "Our business depends on demand for consumer electronics, which can fall sharply in a recession or downturn."
MDNA = "Revenue grew because of higher unit sales of our flagship devices and continued growth in subscription services."


def _fake_company(monkeypatch, years: list[dict[str, str]]):
    """years: oldest first, each {section: text}; recent_filings returns them newest first, like EDGAR."""
    filings = [edgar.Filing("0000000042", f"acc-{i}", "10-K", f"202{i}-02-01", f"202{i - 1}-12-31", "doc.htm")
               for i in range(len(years))]
    texts = {f.accession: y for f, y in zip(filings, years)}
    monkeypatch.setattr(edgar, "lookup", lambda t: {"cik": "0000000042", "name": "Acme Corp"})
    monkeypatch.setattr(edgar, "recent_filings", lambda cik, forms, limit=50: filings[::-1][:limit])
    monkeypatch.setattr(changes, "sections", lambda f: texts[f.accession])


def test_score_company_stores_and_is_idempotent(engine, monkeypatch):
    y = {"Risk Factors": RISK, "MD&A": MDNA}
    _fake_company(monkeypatch, [y, {**y, "MD&A": MDNA + "\n\n" + RISK}, y])
    out = changes.score_company("acme", engine=engine)
    assert [c.accession for c in out] == ["acc-1", "acc-2"] and out[0].prev_accession == "acc-0"
    assert out[0].scores["Risk Factors"] == 0 and out[0].scores["MD&A"] > 0
    changes.score_company("acme", engine=engine)
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.filing_changes)).scalar() == 4


def test_score_company_needs_two_annual_reports(engine, monkeypatch):
    _fake_company(monkeypatch, [{"MD&A": MDNA}])
    with pytest.raises(edgar.EdgarError, match="fewer than two 10-K"):
        changes.score_company("acme", engine=engine)


def test_score_company_skips_missing_section(engine, monkeypatch):
    _fake_company(monkeypatch, [{"Risk Factors": RISK, "MD&A": MDNA}, {"MD&A": MDNA + "\n\n" + RISK}])
    (only,) = changes.score_company("acme", engine=engine)
    assert set(only.scores) == {"MD&A"} and only.combined == only.scores["MD&A"]


def test_daily_index_10k(monkeypatch):
    idx = ("Form Type   Company Name   CIK   Date Filed  File Name\n---\n"
           "10-K        Acme Corp      123456  20261102  edgar/data/123456/0001-26-000001.txt\n"
           "10-K/A      Other          5       20261102  edgar/data/5/0009-26-000009.txt\n"
           "4           Doe Jane       999     20261102  edgar/data/999/0001-26-000002.txt\n")
    monkeypatch.setattr(edgar, "_get", lambda url: type("R", (), {"text": idx})())
    assert edgar.daily_index(date(2026, 11, 2), "10-K") == [
        ("0000123456", "0001-26-000001", "https://www.sec.gov/Archives/edgar/data/123456/0001-26-000001.txt")]


def test_split_paragraphs_on_single_newlines():
    # EDGAR HTML becomes one paragraph per line, with no blank lines between them.
    text = "\n".join(BASE + ["Short heading"])
    assert changes.split_paragraphs(text) == BASE
