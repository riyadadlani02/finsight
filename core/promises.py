"""Said vs did: what management promised in past annual reports, and whether it happened.

Claude extracts promises and judges them against later reports; code checks every quote it cites
really appears in the source, so a verdict can't rest on a sentence nobody wrote.
"""
from __future__ import annotations

import json
import math

from sqlalchemy import insert, select

from . import config, db, debate, markets

YEARS = 4                 # reports read: promises made in the older ones are checked against the newer ones
MAX_CHARS = 60_000        # per report sent to Claude
RESOLVED = ("kept", "partly", "missed", "dropped")
MIN_RESOLVED = 5          # fewer resolved promises than this: too few to judge credibility

EXTRACT = """You read the MD&A and risk sections of {name}'s annual report filed {filed}. List the concrete
forward-looking promises management makes: a target, plan or commitment with something checkable (a number,
a date or a milestone). Skip vague aspirations, standard risk wording and statements about the past.
For each, copy the sentence exactly as written (quote), restate the promise in one short line, and give the
deadline as a fiscal year like FY2026, or "none stated". The text may be in Japanese: quote it as written and
write the promise line in English."""

CHECK = """Below are promises {name}'s management made in earlier annual reports, each with an id, then the text
of later reports, each tagged with its filing date. For each promise decide:
kept, partly (partly kept), missed, dropped (never mentioned again), or open (its deadline is after the latest report).
For kept, partly and missed, copy the later sentence that shows it exactly as written (evidence_quote) and give
that report's filing date (evidence_filed). Judge only from the text given."""

_P = {"type": "object", "additionalProperties": False, "required": ["quote", "promise", "deadline"],
      "properties": {"quote": {"type": "string"}, "promise": {"type": "string"}, "deadline": {"type": "string"}}}
EXTRACT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["promises"],
                  "properties": {"promises": {"type": "array", "items": _P}}}
_V = {"type": "object", "additionalProperties": False, "required": ["id", "verdict", "evidence_quote", "evidence_filed"],
      "properties": {"id": {"type": "string"},
                     "verdict": {"type": "string", "enum": ["kept", "partly", "missed", "dropped", "open"]},
                     "evidence_quote": {"type": "string"}, "evidence_filed": {"type": "string"}}}
CHECK_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdicts"],
                "properties": {"verdicts": {"type": "array", "items": _V}}}


def _norm(text: str) -> str:
    return " ".join(text.replace("’", "'").replace("“", '"').replace("”", '"').split())


def _text(sections: dict[str, str]) -> str:
    return "\n\n".join(f"[{name}]\n{body[:MAX_CHARS]}" for name, body in sections.items())


def _ask(system: str, content: str, schema: dict) -> dict:
    resp = debate._client().beta.messages.create(
        model=config.claude_model(), max_tokens=16000, system=system, messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}}, **debate.FALLBACK)
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"Claude stopped early ({resp.stop_reason}).")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def extract(name: str, accession: str, filed: str, sections: dict[str, str]) -> list[dict]:
    """Promises in one report; any whose quote isn't in the report is dropped."""
    source = _norm(_text(sections))
    found = _ask(EXTRACT.format(name=name, filed=filed), _text(sections), EXTRACT_SCHEMA)["promises"]
    return [{**p, "made_in": accession, "made_filed": filed} for p in found if p["quote"] and _norm(p["quote"]) in source]


def check(name: str, made: list[dict], later: list[tuple[str, str, dict]]) -> list[dict]:
    """Verdicts for promises against later reports. A verdict citing evidence that isn't there becomes 'unchecked'."""
    listing = "\n".join(f"{p['id']}: {p['promise']} (deadline {p['deadline']}; said in report filed {p['made_filed']}: "
                        f"\"{p['quote']}\")" for p in made)
    reports = "\n\n".join(f"===== Report filed {filed} =====\n{_text(secs)}" for _, filed, secs in later)
    verdicts = {v["id"]: v for v in _ask(CHECK.format(name=name), f"PROMISES\n{listing}\n\nLATER REPORTS\n{reports}",
                                         CHECK_SCHEMA)["verdicts"]}
    later_text = _norm(" ".join(_text(secs) for _, _, secs in later))
    out = []
    for p in made:
        v = verdicts.get(p["id"], {"verdict": "unchecked", "evidence_quote": "", "evidence_filed": ""})
        if v["verdict"] in ("kept", "partly", "missed") and (not v["evidence_quote"] or _norm(v["evidence_quote"]) not in later_text):
            v = {**v, "verdict": "unchecked"}
        out.append({**p, "verdict": v["verdict"], "evidence_quote": v.get("evidence_quote", ""),
                    "evidence_filed": v.get("evidence_filed", "")})
    return out


def score(rows: list[dict]) -> dict:
    counts = {k: sum(r["verdict"] == k for r in rows) for k in ("kept", "partly", "missed", "dropped", "open", "unchecked")}
    resolved = sum(counts[k] for k in RESOLVED)
    credibility = (counts["kept"] + 0.5 * counts["partly"]) / resolved if resolved else math.nan
    return {**counts, "resolved": resolved, "credibility": credibility, "too_few": resolved < MIN_RESOLVED}


def _reports(ticker: str, years: int) -> tuple[str, str, list[tuple[str, str, dict]]]:
    """(company name, market, [(report id, filed date, sections)] oldest first) from the market's adapter."""
    market = markets.of_ticker(ticker)
    if market == "US":
        from . import changes
        company, filings = changes._annual(ticker, years)
        return company["name"], market, [(f.accession, f.filed, changes.sections(f)) for f in filings]
    if market == "JP":
        from .markets import japan
        return japan.reports_with_sections(ticker, years)
    raise ValueError(f"Said vs did needs annual reports, which aren't available for {markets.get(market).name} yet.")


def run(ticker: str, engine=None, years: int = YEARS) -> dict:
    """The promise ledger for a company, built once per newest report and stored."""
    engine, t = engine or db.engine(), db.promises
    name, market, reports = _reports(ticker, years)
    latest = reports[-1][0]
    with engine.connect() as conn:
        stored = [dict(r._mapping) for r in conn.execute(select(t).where(t.c.ticker == ticker, t.c.latest == latest)
                                                          .order_by(t.c.id))]
    if not stored:
        made = [p for acc, filed, secs in reports for p in extract(name, acc, filed, secs)]
        for i, p in enumerate(made, 1):
            p["id"] = f"p{i}"
        rows = []
        for i, (acc, _, _) in enumerate(reports):
            batch, later = [p for p in made if p["made_in"] == acc], reports[i + 1:]
            if batch:
                rows += check(name, batch, later) if later else [{**p, "verdict": "open", "evidence_quote": "",
                                                                  "evidence_filed": ""} for p in batch]
        stored = [{"ticker": ticker, "market": market, "latest": latest, "pid": r["id"], "made_in": r["made_in"],
                   "made_filed": r["made_filed"], "quote": r["quote"], "promise": r["promise"], "deadline": r["deadline"],
                   "verdict": r["verdict"], "evidence_quote": r["evidence_quote"], "evidence_filed": r["evidence_filed"]}
                  for r in rows]
        if stored:
            with engine.begin() as conn:
                conn.execute(insert(t), stored)
    return {"name": name, "ticker": ticker, "market": market, "latest": latest, "promises": stored, "score": score(stored)}


def stored(ticker: str, engine=None) -> dict | None:
    """The most recently built ledger for a company, read from the database (no Claude, no network)."""
    engine, t = engine or db.engine(), db.promises
    with engine.connect() as conn:
        latest = conn.execute(select(t.c.latest).where(t.c.ticker == ticker).order_by(t.c.checked_at.desc(), t.c.id.desc())
                              .limit(1)).scalar()
        if latest is None:
            return None
        rows = [dict(r._mapping) for r in conn.execute(select(t).where(t.c.ticker == ticker, t.c.latest == latest).order_by(t.c.id))]
    return {"ticker": ticker, "latest": latest, "promises": rows, "score": score(rows)}
