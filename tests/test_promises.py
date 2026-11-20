"""Offline tests for Said vs did: promise extraction, checking against later reports, credibility."""
import json
from types import SimpleNamespace as NS

from core import debate, promises

FY24 = ("We expect to reduce net debt below $10 billion by fiscal 2025.\n"
        "Our goal is to open 3 new factories in Asia by the end of fiscal 2026.\n"
        "We remain committed to delivering value to shareholders.")
FY25 = "Net debt ended the year at $13.4 billion, above our target.\nWe opened two of the three planned Asian factories."
FY26 = "All three Asian factories are now operating."


def _fake_claude(monkeypatch, replies):
    sent = []

    def create(**kw):
        sent.append(kw)
        return NS(stop_reason="end_turn", content=[NS(type="text", text=json.dumps(replies.pop(0)))])
    monkeypatch.setattr(debate, "_client", lambda: NS(beta=NS(messages=NS(create=create))))
    return sent


def test_extract_keeps_only_quotes_found_in_the_report(monkeypatch):
    sent = _fake_claude(monkeypatch, [{"promises": [
        {"quote": "We expect to reduce net debt below $10 billion by fiscal 2025.", "promise": "Net debt under $10B", "deadline": "FY2025"},
        {"quote": "We will double revenue by 2030.", "promise": "Invented", "deadline": "FY2030"}]}])
    got = promises.extract("Acme", "acc-24", "2024-02-01", {"MD&A": FY24})
    assert [p["promise"] for p in got] == ["Net debt under $10B"] and got[0]["made_in"] == "acc-24"
    assert sent[0]["output_config"]["format"]["type"] == "json_schema" and "tool_choice" not in sent[0]


def test_check_requires_real_evidence(monkeypatch):
    made = [{"id": "p1", "quote": "q1", "promise": "Net debt under $10B", "deadline": "FY2025", "made_in": "acc-24", "made_filed": "2024-02-01"},
            {"id": "p2", "quote": "q2", "promise": "3 Asian factories", "deadline": "FY2026", "made_in": "acc-24", "made_filed": "2024-02-01"},
            {"id": "p3", "quote": "q3", "promise": "Launch in Brazil", "deadline": "FY2025", "made_in": "acc-24", "made_filed": "2024-02-01"},
            {"id": "p4", "quote": "q4", "promise": "Cut costs 10%", "deadline": "FY2025", "made_in": "acc-24", "made_filed": "2024-02-01"}]
    later = [("acc-25", "2025-02-01", {"MD&A": FY25}), ("acc-26", "2026-02-01", {"MD&A": FY26})]
    _fake_claude(monkeypatch, [{"verdicts": [
        {"id": "p1", "verdict": "missed", "evidence_quote": "Net debt ended the year at $13.4 billion, above our target.", "evidence_filed": "2025-02-01"},
        {"id": "p2", "verdict": "kept", "evidence_quote": "All three Asian factories are now operating.", "evidence_filed": "2026-02-01"},
        {"id": "p3", "verdict": "dropped", "evidence_quote": "", "evidence_filed": ""},
        {"id": "p4", "verdict": "kept", "evidence_quote": "Costs fell 12%.", "evidence_filed": "2025-02-01"}]}])  # not in any report
    v = {r["id"]: r["verdict"] for r in promises.check("Acme", made, later)}
    assert v == {"p1": "missed", "p2": "kept", "p3": "dropped", "p4": "unchecked"}


def test_score():
    rows = [{"verdict": v} for v in ["kept"] * 3 + ["partly"] * 2 + ["missed"] + ["dropped"] + ["open", "unchecked"]]
    s = promises.score(rows)
    assert (s["kept"], s["partly"], s["missed"], s["dropped"], s["open"], s["unchecked"]) == (3, 2, 1, 1, 1, 1)
    assert s["judged"] == 6 and abs(s["credibility"] - 4 / 6) < 1e-9 and not s["too_few"]
    assert promises.score([{"verdict": "kept"}] * 4)["too_few"]


def test_run_caches_per_latest_report(engine, monkeypatch):
    reports = [("acc-24", "2024-02-01", {"MD&A": FY24}), ("acc-25", "2025-02-01", {"MD&A": FY25}),
               ("acc-26", "2026-02-01", {"MD&A": FY26})]
    monkeypatch.setattr(promises, "_reports", lambda ticker, years: ("Acme", "US", reports))
    calls = {"extract": 0, "check": 0}
    monkeypatch.setattr(promises, "extract", lambda name, acc, filed, secs, language="en": calls.__setitem__("extract", calls["extract"] + 1) or (
        [{"quote": "q", "promise": "Net debt under $10B", "deadline": "FY2025", "made_in": acc, "made_filed": filed}] if acc == "acc-24" else []))
    monkeypatch.setattr(promises, "check", lambda name, made, later, language="en": calls.__setitem__("check", calls["check"] + 1) or
                        [{**p, "verdict": "missed", "evidence_quote": "", "evidence_filed": ""} for p in made])
    first = promises.run("ACME", engine=engine)
    again = promises.run("ACME", engine=engine)
    assert calls == {"extract": 3, "check": 1}  # the second run reads the stored ledger
    assert [p["verdict"] for p in first["promises"]] == [p["verdict"] for p in again["promises"]] == ["missed"]
    assert again["latest"] == "acc-26" and again["score"]["missed"] == 1


def test_stored_reads_the_latest_ledger_without_claude(engine):
    from sqlalchemy import insert
    from core import db
    assert promises.stored("ACME", engine=engine) is None
    with engine.begin() as conn:
        conn.execute(insert(db.promises), [{"ticker": "ACME", "latest": "acc-26", "pid": "p1", "promise": "x", "verdict": "kept"},
                                           {"ticker": "ACME", "latest": "acc-26", "pid": "p2", "promise": "y", "verdict": "open"}])
    s = promises.stored("ACME", engine=engine)
    assert s["latest"] == "acc-26" and [p["pid"] for p in s["promises"]] == ["p1", "p2"] and s["score"]["kept"] == 1


def test_a_fragment_of_a_negated_sentence_is_not_a_promise(monkeypatch):
    src = {"MD&A": "We do not expect to open 50 new stores by fiscal 2026 given current demand conditions."}
    _fake_claude(monkeypatch, [{"promises": [
        {"quote": "expect to open 50 new stores by fiscal 2026", "promise": "Open 50 stores", "deadline": "FY2026"},
        {"quote": "MD&A", "promise": "header", "deadline": "none stated"}]}])
    assert promises.extract("Acme", "acc", "2024-02-01", src) == []


def test_extract_stores_the_whole_sentence(monkeypatch):
    src = {"MD&A": "Revenue grew 8%. We plan to open 3 new factories in Asia by the end of fiscal 2026, subject to approvals."}
    _fake_claude(monkeypatch, [{"promises": [{"quote": "open 3 new factories in Asia by the end of fiscal 2026",
                                              "promise": "3 Asian factories", "deadline": "FY2026"}]}])
    (p,) = promises.extract("Acme", "acc", "2024-02-01", src)
    assert p["quote"] == "We plan to open 3 new factories in Asia by the end of fiscal 2026, subject to approvals."


def test_japanese_quotes_match_across_line_breaks_and_widths(monkeypatch):
    src = {"MD&A": "当社は２０２６年度までに売上高を\n１兆円に拡大することを目指します。"}
    _fake_claude(monkeypatch, [{"promises": [{"quote": "当社は2026年度までに売上高を1兆円に拡大することを目指します。",
                                              "promise": "Revenue of ¥1tn by FY2026", "deadline": "FY2026"}]}])
    assert len(promises.extract("トヨタ", "doc", "2024-06-20", src, language="ja")) == 1


def test_evidence_must_come_from_the_report_it_names(monkeypatch):
    made = [{"id": "p1", "quote": "q", "promise": "x", "deadline": "FY2025", "made_in": "a", "made_filed": "2024-02-01"}]
    later = [("acc-25", "2025-02-01", {"MD&A": "Net debt ended the year at $13.4 billion, above our target."}),
             ("acc-26", "2026-02-01", {"MD&A": "Nothing about debt here at all this year."})]
    _fake_claude(monkeypatch, [{"verdicts": [{"id": "p1", "verdict": "missed", "evidence_filed": "2026-02-01",
                                              "evidence_quote": "Net debt ended the year at $13.4 billion, above our target."}]}])
    assert promises.check("Acme", made, later)[0]["verdict"] == "unchecked"


def test_credibility_leaves_unmentioned_promises_out():
    s = promises.score([{"verdict": v} for v in ["kept"] * 4 + ["missed"] * 2 + ["dropped"] * 6])
    assert abs(s["credibility"] - 4 / 6) < 1e-9 and s["dropped"] == 6 and s["judged"] == 6 and not s["too_few"]
