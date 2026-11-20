"""Japan: annual securities reports (有価証券報告書) from EDINET, the FSA's official filing system.

The company list downloads freely; filings need a free EDINET API key (EDINET_API_KEY), from an
account registered on EDINET. EDINET lists filings by day, so annual reports are indexed by
scanning days once and remembering which were scanned.
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from datetime import date, timedelta
from functools import lru_cache
from html import unescape
from pathlib import Path

import pandas as pd
import requests
from sqlalchemy import insert, select

from .. import config, db
from . import Company

API = "https://api.edinet-fsa.go.jp/api/v2/"
CODE_LIST = "https://disclosure2dl.edinet-fsa.go.jp/searchdocument/codelist/Edinetcode.zip"
HEADERS = {"User-Agent": "Mozilla/5.0 (FinSight research client)"}
DATA = Path(__file__).resolve().parent.parent.parent / "data"
ANNUAL = "120"                    # docTypeCode of an annual securities report
SECTIONS = {  # EDINET taxonomy text blocks -> FinSight's canonical section names
    "jpcrp_cor:BusinessRisksTextBlock": "Risk Factors",
    "jpcrp_cor:ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock": "MD&A",
}
_CODE = re.compile(r"^[0-9][0-9A-Z]{3}$")   # securities code, e.g. 7203 or 130A
_last = 0.0


class JapanError(RuntimeError):
    pass


def _code_csv() -> str:
    """EDINET's code list (EDINET code, securities code, names), cached for a month."""
    path = DATA / "edinet_codes.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > 30 * 86400:
        raw = requests.get(CODE_LIST, headers=HEADERS, timeout=60)
        raw.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(raw.content)) as z:
            text = z.read(z.namelist()[0]).decode("cp932")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return path.read_text()


@lru_cache(maxsize=1)
def _codes() -> dict[str, tuple[str, str]]:
    df = pd.read_csv(io.StringIO(_code_csv()), skiprows=1, dtype=str).fillna("")
    out = {}
    for r in df.itertuples(index=False):
        edinet, name_ja, name_en, sec = r[0], r[6], r[7], r[11]
        if sec:
            out[sec[:4].upper()] = (edinet, name_en or name_ja)
    return out


def resolve(text: str) -> Company | None:
    code = text.strip().upper().removesuffix(".T")
    if not _CODE.match(code):
        return None
    try:
        hit = _codes().get(code)
    except requests.RequestException:
        return None
    return Company(f"{code}.T", hit[1], hit[0], "JP") if hit else None


def _api(path: str, params: dict, raw: bool = False):
    global _last
    key = config.get("EDINET_API_KEY")
    if not key:
        raise JapanError("Japan needs a free EDINET API key: register on EDINET (https://api.edinet-fsa.go.jp) "
                         "and set EDINET_API_KEY in .env or the app's secrets.")
    wait = 0.5 - (time.monotonic() - _last)   # polite pacing
    if wait > 0:
        time.sleep(wait)
    _last = time.monotonic()
    try:
        resp = requests.get(API + path, params={**params, "Subscription-Key": key}, headers=HEADERS, timeout=60)
        resp.raise_for_status()
    except requests.RequestException as exc:
        # EDINET takes the key in the URL, so the original error text would carry it: never pass that on.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        raise JapanError(f"EDINET request for {path} failed" + (f" (HTTP {status})" if status else "")) from None
    return resp.content if raw else resp.json()


def scan(days: int, engine=None, today: date | None = None, progress=None) -> int:
    """Index annual reports filed in the last `days` days; days already scanned are skipped (resumable)."""
    engine, today = engine or db.engine(), today or date.today()
    with engine.connect() as conn:
        done = {r[0] for r in conn.execute(select(db.jp_scanned.c.day))}
    todo = [today - timedelta(days=n) for n in range(days) if today - timedelta(days=n) not in done]
    added = 0
    for i, day in enumerate(todo, 1):
        results = _api("documents.json", {"date": day.isoformat(), "type": 2}).get("results") or []
        rows = [{"doc_id": r["docID"], "edinet_code": r["edinetCode"], "sec_code": r.get("secCode"), "filer": r.get("filerName"),
                 "filed": date.fromisoformat(r["submitDateTime"][:10]), "period_end": r.get("periodEnd")}
                for r in results if r.get("docTypeCode") == ANNUAL and r.get("edinetCode")]
        with engine.begin() as conn:
            if rows:
                known = {x[0] for x in conn.execute(select(db.jp_reports.c.doc_id).where(
                    db.jp_reports.c.doc_id.in_([r["doc_id"] for r in rows])))}
                new = [r for r in rows if r["doc_id"] not in known]
                if new:
                    conn.execute(insert(db.jp_reports), new)
                added += len(new)
            conn.execute(insert(db.jp_scanned).values(day=day))
        if progress:
            progress(i, len(todo))
    return added


def annual_reports(edinet_code: str, limit: int = 6, engine=None) -> list[dict]:
    """Indexed annual reports for a company, newest first."""
    t = db.jp_reports
    with (engine or db.engine()).connect() as conn:
        rows = conn.execute(select(t).where(t.c.edinet_code == edinet_code).order_by(t.c.filed.desc()).limit(limit))
        return [dict(r._mapping) for r in rows]


def parse_sections(zip_bytes: bytes) -> dict[str, str]:
    """Risk and MD&A text from EDINET's CSV rendition (a zip of UTF-16 tab-separated files)."""
    found: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            if not name.endswith(".csv"):
                continue
            df = pd.read_csv(io.BytesIO(z.read(name)), sep="\t", encoding="utf-16", dtype=str).fillna("")
            for element, value in zip(df.iloc[:, 0], df.iloc[:, -1]):
                if element in SECTIONS and SECTIONS[element] not in found:
                    text = unescape(re.sub(r"<[^>]+>", "\n", value))
                    found[SECTIONS[element]] = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    return found


def sections(doc_id: str) -> dict[str, str]:
    path = DATA / "edinet" / f"{doc_id}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(parse_sections(_api(f"documents/{doc_id}", {"type": 5}, raw=True)), ensure_ascii=False))
    return json.loads(path.read_text())


def reports_with_sections(ticker: str, years: int) -> tuple[str, str, list[tuple[str, str, dict]]]:
    company = resolve(ticker)
    if company is None:
        raise ValueError(f"{ticker} isn't a listed Japanese securities code.")
    reports = annual_reports(company.issuer_id, years)
    if len(reports) < 2:
        raise ValueError(f"Fewer than two annual reports for {company.name} are indexed yet. Scan EDINET on the "
                         "Filing changes page (Japan) first.")
    return company.name, "JP", [(r["doc_id"], str(r["filed"]), sections(r["doc_id"])) for r in reversed(reports)]
