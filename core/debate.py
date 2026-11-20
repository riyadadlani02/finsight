"""Bull vs Bear: two Claude agents argue over a stock from its SEC filings; a third judges.

Flow: build a dossier (filing excerpts + XBRL financials + market data), then
bull opening -> bear opening -> bull rebuttal -> bear rebuttal -> judge verdict.
Every agent sees the same dossier and is told to argue only from it.
"""
from __future__ import annotations

import json
from typing import Iterator

import anthropic
import yfinance as yf
from sqlalchemy import insert, select

from . import config, db, edgar

DISCLAIMER = "Research aid only. AI-generated argument, not investment advice."

RULES = """Rules:
- Argue ONLY from the dossier. Every factual claim needs a source tag such as
  [10-K Risk Factors], [10-Q MD&A], [Financials] or [Market data].
- Never invent numbers. If the dossier lacks something, say so rather than guessing.
- Be specific: quote figures and short phrases from the filings.
- Under 350 words. Plain prose with short paragraphs, no headings."""

BULL = f"""You are the BULL in a structured investment debate about {{name}} ({{ticker}}).
Make the strongest honest case that the stock is an attractive investment today.
{RULES}"""

BEAR = f"""You are the BEAR in a structured investment debate about {{name}} ({{ticker}}).
Make the strongest honest case that the stock is unattractive or too risky today.
{RULES}"""

JUDGE = """You are the impartial JUDGE of an investment debate about {name} ({ticker}).
You have the same dossier the debaters had. Score each side on how well its
argument is supported by the dossier, not on which conclusion you like.
Penalise any claim that is unsupported by or contradicts the dossier, and name it.
Deciding who argued better is not a recommendation to buy or sell.
Return your decision in the required JSON format."""

# Forced tool_choice is rejected on current models, so the verdict comes back as structured JSON.
VERDICT_SCHEMA = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "winner": {"type": "string", "enum": ["bull", "bear", "draw"]},
            "bull_score": {"type": "integer", "description": "0-100"},
            "bear_score": {"type": "integer", "description": "0-100"},
            "summary": {"type": "string", "description": "3-5 sentences explaining the decision"},
            "strongest_bull_point": {"type": "string"},
            "strongest_bear_point": {"type": "string"},
            "unsupported_claims": {
                "type": "array", "items": {"type": "string"},
                "description": "Claims by either side not backed by the dossier, prefixed with Bull: or Bear:",
            },
            "what_would_change_the_verdict": {"type": "string"},
        },
        "required": ["winner", "bull_score", "bear_score", "summary",
                     "strongest_bull_point", "strongest_bear_point",
                     "unsupported_claims", "what_would_change_the_verdict"],
}

# On a safety decline the API reruns the request on a fallback model instead of failing the debate.
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}


# ---------------------------------------------------------------- dossier

def _money(v: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(v) >= size:
            return f"{v / size:,.2f}{unit}"
    return f"{v:,.2f}"


def market_snapshot(ticker: str) -> dict:
    """Price and valuation from Yahoo Finance. Best effort: returns {} on failure."""
    try:
        tk = yf.Ticker(ticker)
        hist = tk.history(period="1y")["Close"].dropna()
        if hist.empty:
            return {}
        snap = {
            "Last close": round(float(hist.iloc[-1]), 2),
            "1-year change %": round((hist.iloc[-1] / hist.iloc[0] - 1) * 100, 1),
            "52-week high": round(float(hist.max()), 2),
            "52-week low": round(float(hist.min()), 2),
        }
        try:
            info = tk.info
            for label, key in (("Market cap", "marketCap"), ("Trailing P/E", "trailingPE"),
                               ("Forward P/E", "forwardPE"), ("Price/Sales", "priceToSalesTrailing12Months"),
                               ("Dividend yield", "dividendYield"), ("Beta", "beta")):
                if info.get(key) is not None:
                    snap[label] = _money(info[key]) if key == "marketCap" else round(float(info[key]), 2)
        except Exception:  # noqa: BLE001 - .info is flaky; prices alone are fine
            pass
        snap["_history"] = hist
        return snap
    except Exception:  # noqa: BLE001
        return {}


def build_dossier(ticker: str, max_section_chars: int = 30000) -> dict:
    """Gather everything the agents may argue from. Returns the text plus pieces for the UI."""
    ticker = ticker.strip().upper()
    company = edgar.lookup(ticker)
    filings = edgar.recent_filings(company["cik"], ("10-K", "10-Q", "20-F", "40-F"), limit=12)
    if not filings:
        raise edgar.EdgarError(f"{ticker} has no annual or quarterly reports on EDGAR.")
    annual = next((f for f in filings if f.form != "10-Q"), None)
    quarterly = next((f for f in filings if f.form == "10-Q"), None)
    if annual and quarterly and quarterly.filed < annual.filed:
        quarterly = None  # the 10-K already covers that quarter

    parts, sources = [], []
    for filing in (annual, quarterly):
        if not filing:
            continue
        sources.append(filing)
        for name, body in edgar.filing_sections(filing, max_section_chars).items():
            parts.append(f"===== [{filing.form} {name}] period {filing.report_date}, filed {filing.filed} =====\n{body}")

    facts = edgar.annual_facts(company["cik"])
    if facts:
        lines = [f"{label}: " + "; ".join(
            f"FY ending {end} = {v:,.2f}" if abs(v) < 1000 else f"FY ending {end} = {_money(v)}"
            for end, v in series.items()) for label, series in facts.items()]
        parts.append("===== [Financials] annual XBRL data reported to the SEC =====\n" + "\n".join(lines))

    market = market_snapshot(ticker)
    history = market.pop("_history", None)
    if market:
        parts.append("===== [Market data] Yahoo Finance =====\n" + "\n".join(f"{k}: {v}" for k, v in market.items()))

    return {"ticker": ticker, "name": company["name"], "text": "\n\n".join(parts),
            "sources": sources, "facts": facts, "market": market, "history": history}


# ----------------------------------------------------------------- agents

def _client() -> anthropic.Anthropic:
    if not config.anthropic_key():
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env.")
    return anthropic.Anthropic(api_key=config.anthropic_key())


def _dossier_block(dossier: dict) -> dict:
    # Cached so the four later calls don't pay full price for the same filings.
    return {"type": "text", "text": f"DOSSIER for {dossier['name']} ({dossier['ticker']}):\n\n{dossier['text']}",
            "cache_control": {"type": "ephemeral"}}


def _transcript_text(transcript: list[dict]) -> str:
    return "\n\n".join(f"--- {t['speaker'].upper()} ({t['round']}) ---\n{t['text']}" for t in transcript)


ROUNDS = [  # (speaker, round name, instruction)
    ("bull", "opening", "Give your opening argument."),
    ("bear", "opening", "Give your opening argument. You may respond to the bull's opening."),
    ("bull", "rebuttal", "Rebut the bear's strongest points directly, then restate your best evidence."),
    ("bear", "rebuttal", "Rebut the bull's strongest points directly, then restate your best evidence."),
]


def stream_turn(dossier: dict, transcript: list[dict], speaker: str, instruction: str) -> Iterator[str]:
    """Yield one debater's turn as text chunks."""
    system = (BULL if speaker == "bull" else BEAR).format(name=dossier["name"], ticker=dossier["ticker"])
    content = [_dossier_block(dossier)]
    if transcript:
        content.append({"type": "text", "text": "DEBATE SO FAR:\n\n" + _transcript_text(transcript)})
    content.append({"type": "text", "text": instruction})
    # Thinking is on by default and counts toward max_tokens; the 350-word limit lives in the prompt.
    with _client().beta.messages.stream(model=config.claude_model(), max_tokens=16000, system=system,
                                        messages=[{"role": "user", "content": content}], **FALLBACK) as stream:
        yield from stream.text_stream


def judge(dossier: dict, transcript: list[dict]) -> dict:
    resp = _client().beta.messages.create(
        model=config.claude_model(), max_tokens=16000,
        system=JUDGE.format(name=dossier["name"], ticker=dossier["ticker"]),
        output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}}, **FALLBACK,
        messages=[{"role": "user", "content": [
            _dossier_block(dossier),
            {"type": "text", "text": "FULL DEBATE:\n\n" + _transcript_text(transcript)},
        ]}],
    )
    if resp.stop_reason != "end_turn":
        raise RuntimeError(f"The judge stopped early ({resp.stop_reason}) without a verdict.")
    return json.loads(next(b.text for b in resp.content if b.type == "text"))


# ---------------------------------------------------------------- history

def save(dossier: dict, transcript: list[dict], verdict: dict) -> None:
    with db.engine().begin() as conn:
        conn.execute(insert(db.debates).values(
            ticker=dossier["ticker"], model=config.claude_model(), winner=verdict["winner"],
            bull_score=verdict["bull_score"], bear_score=verdict["bear_score"],
            verdict_json=json.dumps(verdict), transcript_json=json.dumps(transcript)))


def history(limit: int = 50) -> list[dict]:
    with db.engine().connect() as conn:
        rows = conn.execute(select(db.debates).order_by(db.debates.c.created_at.desc()).limit(limit))
        return [dict(r._mapping) for r in rows]
