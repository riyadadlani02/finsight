"""SEC EDGAR client shared by the debate and the insider radar.

EDGAR is free and needs no key, but it requires a descriptive User-Agent and
caps clients at 10 requests/second. All requests go through `_get`, which
throttles to stay under that.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

from . import config

CACHE = Path(__file__).resolve().parent.parent / "data" / "cache"
_MIN_INTERVAL = 0.12  # seconds between requests -> ~8 req/s
_lock = threading.Lock()
_last_request = 0.0


class EdgarError(RuntimeError):
    pass


def _get(url: str) -> requests.Response:
    global _last_request
    ua = config.sec_user_agent()
    if not ua:
        raise EdgarError('SEC_USER_AGENT is not set. Put "Your Name you@example.com" in .env.')
    for attempt in range(4):
        with _lock:
            wait = _MIN_INTERVAL - (time.monotonic() - _last_request)
            if wait > 0:
                time.sleep(wait)
            _last_request = time.monotonic()
        resp = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}, timeout=30)
        if resp.status_code in (429, 503):  # throttled by the SEC: back off and retry
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 404:
            raise EdgarError(f"Not found: {url}")
        resp.raise_for_status()
        return resp
    raise EdgarError(f"SEC kept throttling {url}")


# ---------------------------------------------------------------- companies

def ticker_map() -> dict[str, dict]:
    """{TICKER: {"cik": "0000320193", "name": "Apple Inc."}}, cached for a day."""
    path = CACHE / "company_tickers.json"
    if not path.exists() or time.time() - path.stat().st_mtime > 86400:
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(_get("https://www.sec.gov/files/company_tickers.json").text)
    raw = json.loads(path.read_text())
    return {
        row["ticker"].upper(): {"cik": str(row["cik_str"]).zfill(10), "name": row["title"]}
        for row in raw.values()
    }


def lookup(ticker: str) -> dict:
    hit = ticker_map().get(ticker.strip().upper())
    if not hit:
        raise EdgarError(f"No SEC registrant found for ticker '{ticker}'.")
    return hit


@dataclass
class Filing:
    cik: str
    accession: str
    form: str
    filed: str
    report_date: str
    primary_doc: str

    @property
    def _base(self) -> str:
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/{self.accession.replace('-', '')}"

    @property
    def url(self) -> str:
        return f"{self._base}/{self.primary_doc}"

    @property
    def txt_url(self) -> str:
        return f"{self._base}/{self.accession}.txt"


def recent_filings(cik: str, forms: tuple[str, ...], limit: int = 50) -> list[Filing]:
    """Most recent filings of the given form types, newest first."""
    filings = _get(f"https://data.sec.gov/submissions/CIK{cik}.json").json()["filings"]
    out: list[Filing] = []
    # "recent" holds the last ~1,000 filings; heavy filers (banks issuing notes daily) push older ones into files[].
    pages = [filings["recent"]] + [f["name"] for f in filings.get("files", [])]
    for page in pages:
        cols = page if isinstance(page, dict) else _get(f"https://data.sec.gov/submissions/{page}").json()
        for i, form in enumerate(cols["form"]):
            if form in forms:
                out.append(Filing(cik, cols["accessionNumber"][i], form, cols["filingDate"][i],
                                  cols["reportDate"][i], cols["primaryDocument"][i]))
                if len(out) >= limit:
                    return out
    return out


# ------------------------------------------------------- 10-K / 10-Q text

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def extract_section(text: str, start: str, ends: tuple[str, ...], max_chars: int) -> str:
    """Pull one 'Item' section out of a filing.

    The heading appears at least twice (table of contents, then the real one),
    so we take every match and keep the longest span that follows it.
    """
    end_re = re.compile(r"\n\s*(?:" + "|".join(ends) + r")", re.I)
    best = ""
    for m in re.finditer(r"\n\s*" + start, text, re.I):
        end = end_re.search(text, m.end())
        span = text[m.start(): end.start() if end else len(text)].strip()
        if len(span) > len(best):
            best = span
    if len(best) > max_chars:
        best = best[:max_chars] + "\n[... section truncated ...]"
    return best


def filing_sections(filing: Filing, max_chars: int = 30000) -> dict[str, str]:
    """Risk Factors and MD&A from a 10-K or 10-Q; falls back to the opening text."""
    text = "\n" + html_to_text(_get(filing.url).text)
    dot = r"\s*[\.:\-—]?\s*"
    if filing.form.startswith("10-K"):
        wanted = {
            "Risk Factors": (rf"Item\s*1A{dot}Risk\s+Factors", (r"Item\s*1B", r"Item\s*1C", r"Item\s*2\b")),
            "MD&A": (rf"Item\s*7{dot}Management.s\s+Discussion", (r"Item\s*7A", r"Item\s*8\b")),
        }
    else:
        wanted = {
            "MD&A": (rf"Item\s*2{dot}Management.s\s+Discussion", (r"Item\s*3\b", r"Item\s*4\b")),
            "Risk Factors": (rf"Item\s*1A{dot}Risk\s+Factors", (r"Item\s*2\b", r"Item\s*5\b", r"Item\s*6\b")),
        }
    sections = {name: extract_section(text, start, ends, max_chars) for name, (start, ends) in wanted.items()}
    sections = {k: v for k, v in sections.items() if len(v) > 500}
    if not sections:  # 20-F, 40-F or an unusual layout
        sections = {"Filing text (opening)": text[:max_chars]}
    return sections


# ------------------------------------------------------- XBRL financials

# label -> candidate us-gaap tags, first one with data wins
FACT_TAGS = {
    "Revenue": ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"],
    "Gross profit": ["GrossProfit"],
    "Operating income": ["OperatingIncomeLoss"],
    "Net income": ["NetIncomeLoss"],
    "Diluted EPS": ["EarningsPerShareDiluted"],
    "Operating cash flow": ["NetCashProvidedByUsedInOperatingActivities"],
    "Capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "Cash & equivalents": ["CashAndCashEquivalentsAtCarryingValue"],
    "Total assets": ["Assets"],
    "Total liabilities": ["Liabilities"],
    "Long-term debt": ["LongTermDebt", "LongTermDebtNoncurrent"],
    "Stockholders' equity": ["StockholdersEquity"],
}


def annual_facts(cik: str, years: int = 4) -> dict[str, dict[str, float]]:
    """{label: {fiscal-year-end: value}} for the last few 10-K periods."""
    gaap = _get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json").json().get("facts", {}).get("us-gaap", {})
    out: dict[str, dict[str, float]] = {}
    for label, tags in FACT_TAGS.items():
        best: dict[str, float] = {}
        for tag in tags:
            series: dict[str, float] = {}
            for unit_rows in gaap.get(tag, {}).get("units", {}).values():
                for row in unit_rows:
                    if row.get("form") not in ("10-K", "10-K/A", "20-F", "40-F"):
                        continue
                    if "start" in row:  # flow items: keep full-year periods only
                        span = (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days
                        if not 350 <= span <= 380:
                            continue
                    series[row["end"]] = row["val"]  # later filings restate earlier ones
            # prefer whichever tag has the most recent data
            if series and (not best or max(series) > max(best)):
                best = series
        if best:
            out[label] = dict(sorted(best.items())[-years:])
    return out


# ------------------------------------------------------------- Form 4

def _text(node, path: str) -> str:
    found = node.find(path) if node is not None else None
    return (found.text or "").strip() if found is not None else ""


def _flag(node, path: str) -> bool:
    return _text(node, path).lower() in ("1", "true")


def _num(node, path: str) -> float | None:
    try:
        return float(_text(node, path))
    except ValueError:
        return None


def parse_form4(raw: str, accession: str) -> list[dict]:
    """Rows for every non-derivative transaction in a Form 4.

    `raw` may be the bare ownership XML or the full .txt submission that wraps it.
    """
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", raw, re.S)
    if not m:
        return []
    try:
        doc = ET.fromstring(m.group(0))
    except ET.ParseError:
        return []
    owner = doc.find("reportingOwner")
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    base = {
        "accession": accession,
        "issuer_cik": _text(doc, "issuer/issuerCik").zfill(10),
        "issuer_name": _text(doc, "issuer/issuerName")[:200],
        "ticker": _text(doc, "issuer/issuerTradingSymbol").upper()[:20],
        "insider_cik": _text(owner, "reportingOwnerId/rptOwnerCik").zfill(10),
        "insider_name": _text(owner, "reportingOwnerId/rptOwnerName")[:200],
        "is_director": _flag(rel, "isDirector"),
        "is_officer": _flag(rel, "isOfficer"),
        "is_ten_pct": _flag(rel, "isTenPercentOwner"),
        "officer_title": _text(rel, "officerTitle")[:200],
        "planned_10b5_1": _flag(doc, "aff10b5One"),
    }
    rows = []
    for i, tx in enumerate(doc.findall("nonDerivativeTable/nonDerivativeTransaction")):
        shares = _num(tx, "transactionAmounts/transactionShares/value")
        price = _num(tx, "transactionAmounts/transactionPricePerShare/value")
        try:
            tx_date = datetime.strptime(_text(tx, "transactionDate/value")[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        rows.append({
            **base,
            "line_no": i,
            "tx_date": tx_date,
            "code": _text(tx, "transactionCoding/transactionCode"),
            "acquired": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode/value") == "A",
            "shares": shares,
            "price": price,
            "value": (shares or 0) * (price or 0),
            "shares_after": _num(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction/value"),
            "ownership": _text(tx, "ownershipNature/directOrIndirectOwnership/value")[:1],
        })
    return rows


def fetch_form4(txt_url: str, accession: str) -> list[dict]:
    return parse_form4(_get(txt_url).text, accession)


def daily_index(day: date, form: str) -> list[tuple[str, str, str]]:
    """[(cik10, accession, txt_url)] for every filing of exactly `form` on `day` (empty on weekends/holidays)."""
    quarter = (day.month - 1) // 3 + 1
    url = f"https://www.sec.gov/Archives/edgar/daily-index/{day.year}/QTR{quarter}/form.{day:%Y%m%d}.idx"
    try:
        text = _get(url).text
    except (EdgarError, requests.HTTPError):
        return []
    seen: dict[str, tuple[str, str, str]] = {}
    for line in text.splitlines():
        parts = line.split()
        if not parts or parts[0] != form or not parts[-1].startswith("edgar/data/"):
            continue  # exact form only: "4" excludes 4/A and 424B, "10-K" excludes 10-K/A
        path = parts[-1]  # edgar/data/<cik>/<accession>.txt
        accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
        seen.setdefault(accession, (path.split("/")[2].zfill(10), accession, f"https://www.sec.gov/Archives/{path}"))
    return list(seen.values())  # a filing is listed once per filer; keep the first


def daily_form4_index(day: date) -> list[tuple[str, str]]:
    """[(accession, txt_url)] for every Form 4 filed on `day`."""
    return [(accession, url) for _, accession, url in daily_index(day, "4")]
