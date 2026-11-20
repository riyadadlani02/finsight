"""Said vs did: what management promised in past annual reports, and whether it happened.

Claude extracts promises and judges them against later reports; code checks every quote it cites
really appears in the source, so a verdict can't rest on a sentence nobody wrote.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata

from sqlalchemy import insert, select

from . import config, db, debate, markets

YEARS = 4                 # reports read: promises made in the older ones are checked against the newer ones
MAX_CHARS = 60_000        # per report sent to Claude
JUDGED = ("kept", "partly", "missed")   # "not mentioned again" is shown but isn't evidence of failure
MIN_JUDGED = 5            # fewer judged promises than this: too few to judge credibility
MIN_QUOTE = {"en": 40, "ja": 15}       # shorter "quotes" (a header, a phrase) prove nothing
NEGATIONS = re.compile(r"\b(not|no longer|never|cannot|n't|unlikely)\b", re.I)

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
that report's filing date (evidence_filed). Judge only from the text given. Text marked as truncated may continue
beyond what you see: don't call a promise dropped only because it is missing from a truncated report."""

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


def _norm(text: str, language: str = "en") -> str:
    text = unicodedata.normalize("NFKC", text).replace("’", "'").replace("“", '"').replace("”", '"')
    return "".join(text.split()) if language == "ja" else " ".join(text.split())  # Japanese has no spaces to keep


def _text(sections: dict[str, str]) -> str:
    return "\n\n".join(f"[{name}]\n{body[:MAX_CHARS]}" + ("\n[... text truncated here ...]" if len(body) > MAX_CHARS else "")
                       for name, body in sections.items())


def _bodies(sections: dict[str, str], language: str) -> str:
    """What quotes are checked against: the report text itself, never the section headers we add."""
    return _norm(" ".join(body[:MAX_CHARS] for body in sections.values()), language)


def _sentence(source: str, quote: str, language: str) -> str | None:
    """The whole sentence a quote comes from, or None if the quote isn't in the source."""
    q = _norm(quote, language)
    at = source.find(q)
    if at < 0:
        return None
    ends = "。！？" if language == "ja" else ".!?"
    start = max((source.rfind(c, 0, at) for c in ends), default=-1) + 1
    stops = [i for i in (source.find(c, at + len(q) - 1) for c in ends) if i >= 0]
    return source[start:(min(stops) + 1 if stops else len(source))].strip()


def _ask(system: str, content: str, schema: dict) -> dict:
    resp = debate._client().beta.messages.create(
        model=config.claude_model(), max_tokens=16000, system=system, messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}}, **debate.FALLBACK)
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"Claude stopped early ({resp.stop_reason}).")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


def extract(name: str, accession: str, filed: str, sections: dict[str, str], language: str = "en") -> list[dict]:
    """Promises in one report. Kept only if the quote is really there; stored as its whole sentence, so a
    "do not" can't be cut off, and dropped if the sentence negates what the quote says."""
    source = _bodies(sections, language)
    out = []
    for p in _ask(EXTRACT.format(name=name, filed=filed), _text(sections), EXTRACT_SCHEMA)["promises"]:
        if len(_norm(p["quote"], language)) < MIN_QUOTE[language]:
            continue
        sentence = _sentence(source, p["quote"], language)
        if sentence is None or (language == "en" and NEGATIONS.search(sentence) and not NEGATIONS.search(p["quote"])):
            continue
        out.append({**p, "quote": sentence, "made_in": accession, "made_filed": filed})
    return out


def check(name: str, made: list[dict], later: list[tuple[str, str, dict]], language: str = "en") -> list[dict]:
    """Verdicts for promises against later reports. A verdict citing evidence that isn't there becomes 'unchecked'."""
    listing = "\n".join(f"{p['id']}: {p['promise']} (deadline {p['deadline']}; said in report filed {p['made_filed']}: "
                        f"\"{p['quote']}\")" for p in made)
    reports = "\n\n".join(f"===== Report filed {filed} =====\n{_text(secs)}" for _, filed, secs in later)
    verdicts = {v["id"]: v for v in _ask(CHECK.format(name=name), f"PROMISES\n{listing}\n\nLATER REPORTS\n{reports}",
                                         CHECK_SCHEMA)["verdicts"]}
    by_date = {filed: _bodies(secs, language) for _, filed, secs in later}
    out = []
    for p in made:
        v = verdicts.get(p["id"], {"verdict": "unchecked", "evidence_quote": "", "evidence_filed": ""})
        if v["verdict"] in JUDGED:  # the evidence has to be in the very report it names
            named = by_date.get(v.get("evidence_filed", ""), "")
            quote = _norm(v.get("evidence_quote") or "", language)
            if len(quote) < MIN_QUOTE[language] // 2 or quote not in named:
                v = {**v, "verdict": "unchecked"}
        out.append({**p, "verdict": v["verdict"], "evidence_quote": v.get("evidence_quote", ""),
                    "evidence_filed": v.get("evidence_filed", "")})
    return out


def score(rows: list[dict]) -> dict:
    counts = {k: sum(r["verdict"] == k for r in rows) for k in ("kept", "partly", "missed", "dropped", "open", "unchecked")}
    judged = sum(counts[k] for k in JUDGED)
    credibility = (counts["kept"] + 0.5 * counts["partly"]) / judged if judged else math.nan
    return {**counts, "judged": judged, "credibility": credibility, "too_few": judged < MIN_JUDGED}


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
        language = "ja" if market == "JP" else "en"
        made = [p for acc, filed, secs in reports for p in extract(name, acc, filed, secs, language)]
        for i, p in enumerate(made, 1):
            p["id"] = f"p{i}"
        rows = []
        for i, (acc, _, _) in enumerate(reports):
            batch, later = [p for p in made if p["made_in"] == acc], reports[i + 1:]
            if batch:
                rows += check(name, batch, later, language) if later else [{**p, "verdict": "open", "evidence_quote": "",
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
