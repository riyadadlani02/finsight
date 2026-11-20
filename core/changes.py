"""What a company changed in its 10-K since last year.

Scores are plain text similarity, so anyone can reproduce them; Claude only
explains paragraphs this module has already classified.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import astuple, dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from html import escape
from typing import Callable, Literal

import pandas as pd
import requests
from sqlalchemy import insert, select
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from . import config, db, debate, edgar

UNCHANGED, REWORDED = 0.9, 0.5  # paragraph match thresholds (design doc)
Kind = Literal["added", "removed", "reworded", "unchanged"]


@dataclass
class Para:
    id: str
    kind: Kind
    new: str | None
    old: str | None
    match: float


@dataclass
class SectionDiff:
    similarity: float
    change_score: int
    paras: list[Para] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {k: sum(p.kind == k for p in self.paras) for k in ("added", "removed", "reworded", "unchanged")}


def split_paragraphs(text: str, min_chars: int = 80) -> list[str]:
    """One paragraph per line (EDGAR text has no blank lines), whitespace collapsed; short lines dropped."""
    parts = (re.sub(r"\s+", " ", p).strip() for p in text.split("\n"))
    return [p for p in parts if len(p) >= min_chars]


def _cosine(docs: list[str]):
    return cosine_similarity(TfidfVectorizer(stop_words="english").fit_transform(docs))


def diff_sections(old: str, new: str) -> SectionDiff:
    try:
        similarity = float(_cosine([old, new])[0, 1])
    except ValueError:  # one side has no words
        similarity = 0.0
    similarity = min(max(similarity, 0.0), 1.0)
    old_p, new_p = split_paragraphs(old), split_paragraphs(new)

    # Greedy best-first matching: the most similar pairs claim each other first, each paragraph once.
    matched: dict[int, tuple[int, float]] = {}
    if old_p and new_p:
        sims = _cosine(old_p + new_p)[len(old_p):, :len(old_p)]
        used_old: set[int] = set()
        pairs = sorted(((sims[n, o], n, o) for n in range(len(new_p)) for o in range(len(old_p))), reverse=True)
        for score, n, o in pairs:
            if score < REWORDED:
                break
            if n not in matched and o not in used_old:
                matched[n] = (o, float(score))
                used_old.add(o)

    paras = []
    for n, text in enumerate(new_p):
        if n in matched:
            o, score = matched[n]
            paras.append(Para(f"p{n + 1}", "unchanged" if score >= UNCHANGED else "reworded", text, old_p[o], score))
        else:
            paras.append(Para(f"p{n + 1}", "added", text, None, 0.0))
    used = {o for o, _ in matched.values()}
    removed = [t for o, t in enumerate(old_p) if o not in used]
    paras += [Para(f"r{i + 1}", "removed", None, t, 0.0) for i, t in enumerate(removed)]
    return SectionDiff(similarity, round((1 - similarity) * 100), paras)


def redline_html(old: str, new: str) -> str:
    """Word-level diff with <del>/<ins>; every word is escaped."""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(escape(" ".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{escape(' '.join(a[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{escape(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out)


# ------------------------------------------------------------ companies

@dataclass
class YearChange:
    accession: str
    prev_accession: str
    filed: str
    scores: dict[str, int]
    combined: int  # mean of the sections found in both years


@lru_cache(maxsize=48)  # ponytail: in-process cache of full section text; a disk cache if reruns get slow
def _sections(key: tuple) -> dict[str, str]:
    return edgar.filing_sections(edgar.Filing(*key), max_chars=10**7)


def sections(filing: edgar.Filing) -> dict[str, str]:
    """Risk Factors and MD&A of one filing, untruncated."""
    return _sections(astuple(filing))


def _annual(ticker: str, years: int) -> tuple[dict, list[edgar.Filing]]:
    company = edgar.lookup(ticker)
    filings = edgar.recent_filings(company["cik"], ("10-K",), limit=years)
    if len(filings) < 2:
        raise edgar.EdgarError(f"{ticker.strip().upper()} has fewer than two 10-K filings on EDGAR.")
    return company, filings[::-1]  # oldest first


def _diffs(old: edgar.Filing, new: edgar.Filing) -> dict[str, SectionDiff]:
    a, b = sections(old), sections(new)
    return {name: diff_sections(a[name], b[name]) for name in b if name in a}


def score_company(ticker: str, years: int = 6, engine=None) -> list[YearChange]:
    """Change scores for each consecutive pair of the last `years` 10-Ks, oldest first. Stored once per filing."""
    engine = engine or db.engine()
    company, filings = _annual(ticker, years)
    t = db.filing_changes
    out = []
    for old, new in zip(filings, filings[1:]):
        with engine.connect() as conn:
            stored = {r.section: r.change_score for r in conn.execute(select(t).where(t.c.accession == new.accession))}
        if not stored:
            diffs = _diffs(old, new)
            stored = {name: d.change_score for name, d in diffs.items()}
            with engine.begin() as conn:
                for name, d in diffs.items():
                    conn.execute(insert(t).values(
                        issuer_cik=company["cik"], ticker=ticker.strip().upper(), accession=new.accession,
                        prev_accession=old.accession, filed=date.fromisoformat(new.filed), section=name,
                        similarity=d.similarity, change_score=d.change_score,
                        added=d.counts["added"], removed=d.counts["removed"], reworded=d.counts["reworded"]))
        if stored:  # neither section found in both years: nothing to compare
            out.append(YearChange(new.accession, old.accession, new.filed, stored,
                                  round(sum(stored.values()) / len(stored))))
    return out


def latest(ticker: str) -> dict:
    """The newest 10-K against the one before, with paragraph-level diffs per section."""
    company, filings = _annual(ticker, 2)
    old, new = filings[-2], filings[-1]
    return {"name": company["name"], "ticker": ticker.strip().upper(), "new": new, "old": old, "diffs": _diffs(old, new)}


def screen(days: int, engine=None, progress: Callable[[int, int], None] | None = None) -> pd.DataFrame:
    """Every company that filed a 10-K in the last `days` days, ranked by how much it changed."""
    names = {v["cik"]: (k, v["name"]) for k, v in edgar.ticker_map().items()}
    ciks = list(dict.fromkeys(cik for n in range(days)
                              for cik, _, _ in edgar.daily_index(date.today() - timedelta(days=n), "10-K")))
    rows = []
    for i, cik in enumerate(ciks, 1):
        if cik in names:
            ticker, name = names[cik]
            try:
                ys = score_company(ticker, years=2, engine=engine)
            except (edgar.EdgarError, requests.RequestException):
                ys = []
            if ys:
                y = ys[-1]
                rows.append({"ticker": ticker, "name": name, "filed": y.filed, "combined": y.combined,
                             "risk_factors": y.scores.get("Risk Factors"), "mdna": y.scores.get("MD&A"),
                             "accession": y.accession})
        if progress:
            progress(i, len(ciks))
    cols = ["ticker", "name", "filed", "combined", "risk_factors", "mdna", "accession"]
    return pd.DataFrame(rows, columns=cols).sort_values("combined", ascending=False).reset_index(drop=True)


# ------------------------------------------------------------ explanation

KINDS = ["new risk", "dropped risk", "softened", "strengthened", "other"]
EXPLAIN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["changes"],
    "properties": {"changes": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "section", "kind", "summary"],
        "properties": {"id": {"type": "string"}, "section": {"type": "string"},
                       "kind": {"type": "string", "enum": KINDS}, "summary": {"type": "string"}}}}},
}
EXPLAIN = """You compare two consecutive 10-K filings of {name} ({ticker}). You get only the paragraphs a
text diff found added, removed or reworded, each tagged [section id kind]. Explain the material ones:
a new or dropped risk, a risk made stronger or softer, a real change in what management says. Skip
trivial edits (dates, numbering, wording with the same meaning). Use only the ids given, one plain
sentence each, and quote nothing that is not in the paragraphs."""


def explain(name: str, ticker: str, diffs: dict[str, SectionDiff], max_chars: int = 60_000) -> list[dict]:
    """Claude's reading of the measured changes. Items citing an id the diff didn't produce are dropped."""
    changed = sorted(((section, p) for section, d in diffs.items() for p in d.paras if p.kind != "unchanged"),
                     key=lambda sp: sp[1].match)  # most changed first; added/removed have match 0
    parts, used = [], 0
    for section, p in changed:
        block = f"[{section} {p.id} {p.kind}]\nOLD: {p.old or '(none)'}\nNEW: {p.new or '(none)'}"
        if used + len(block) > max_chars:
            break
        parts.append(block)
        used += len(block)
    if not parts:
        return []
    resp = debate._client().beta.messages.create(
        model=config.claude_model(), max_tokens=16000, system=EXPLAIN.format(name=name, ticker=ticker),
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
        output_config={"format": {"type": "json_schema", "schema": EXPLAIN_SCHEMA}}, **debate.FALLBACK)
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"Claude stopped early ({resp.stop_reason}) without an explanation.")
    valid = {(section, p.id) for section, d in diffs.items() for p in d.paras}
    items = json.loads(next(b.text for b in resp.content if b.type == "text"))["changes"]
    kept = [c for c in items if (c["section"], c["id"]) in valid]
    if len(kept) < len(items):
        logging.getLogger(__name__).warning("explain: dropped %d of %d items citing unknown ids", len(items) - len(kept), len(items))
    return kept
