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


def test_explain_drops_unknown_ids(monkeypatch):
    from types import SimpleNamespace as NS
    from core import debate
    sent = {}

    def create(**kw):
        sent.update(kw)
        body = '{"changes": [{"id": "p2", "section": "Risk Factors", "kind": "strengthened", "summary": "Adds a chip supplier risk."},' \
               ' {"id": "p99", "section": "Risk Factors", "kind": "new risk", "summary": "Invented."}]}'
        return NS(stop_reason="end_turn", content=[NS(type="text", text=body)])

    monkeypatch.setattr(debate, "_client", lambda: NS(beta=NS(messages=NS(create=create))))
    new = [BASE[0], "We depend on a small number of carriers and contract manufacturers, and a single supplier for our chips.", BASE[2]]
    diffs = {"Risk Factors": changes.diff_sections("\n".join(BASE), "\n".join(new))}
    out = changes.explain("Acme Corp", "ACME", diffs)
    assert [c["id"] for c in out] == ["p2"] and out[0]["kind"] == "strengthened"
    assert sent["output_config"]["format"]["type"] == "json_schema" and "tool_choice" not in sent


def test_score_company_records_live_call_once_cutoffs_exist(engine, monkeypatch):
    from core import track
    for i, strength in enumerate(range(0, 100, 10)):  # backtest distribution: cut-offs at 18 and 72
        track.record("filing_change", f"T{i}", 0, strength, date(2024, 3, 1), f"bt-{i}", mode="backtest", engine=engine)
    old = edgar.Filing("0000000042", "acc-old", "10-K", "2025-02-01", "2024-12-31", "doc.htm")
    new = edgar.Filing("0000000042", "acc-new", "10-K", date.today().isoformat(), "2025-12-31", "doc.htm")
    texts = {"acc-old": {"MD&A": MDNA}, "acc-new": {"MD&A": RISK}}
    monkeypatch.setattr(edgar, "lookup", lambda t: {"cik": "0000000042", "name": "Acme Corp"})
    monkeypatch.setattr(edgar, "recent_filings", lambda cik, forms, limit=50: [new, old])
    monkeypatch.setattr(changes, "sections", lambda f: texts[f.accession])
    (y,) = changes.score_company("acme", engine=engine)
    live = track.load_calls(engine, mode="live")
    assert y.combined >= 72 and live[["kind", "direction", "source_ref"]].values.tolist() == [["filing_change", -1, "acc-new"]]
