"""India: NSE's own published files only (company list, daily bulk and block deals).

These download as plain files; the NSE and BSE web pages that hold promoter and pledge data block
scripts, so those wait for the Phase 2 data feed (design doc).
"""
from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

import pandas as pd
import requests

from . import Company

ARCHIVE = "https://nsearchives.nseindia.com/content/equities/"
HEADERS = {"User-Agent": "Mozilla/5.0 (FinSight research client)"}
DATA = Path(__file__).resolve().parent.parent.parent / "data"


def _get(url: str) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp


def _equity_csv() -> str:
    """NSE's list of listed companies, cached for a week."""
    path = DATA / "nse_equity_list.csv"
    if not path.exists() or time.time() - path.stat().st_mtime > 7 * 86400:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_get(ARCHIVE + "EQUITY_L.csv").text)
    return path.read_text()


@lru_cache(maxsize=1)
def _equity_list() -> dict[str, str]:
    from io import StringIO
    df = pd.read_csv(StringIO(_equity_csv()), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    return dict(zip(df["SYMBOL"].str.strip().str.upper(), df["NAME OF COMPANY"].str.strip()))


def resolve(text: str) -> Company | None:
    symbol = text.strip().upper().removesuffix(".NS").removesuffix(".BO")
    try:
        name = _equity_list().get(symbol)
    except requests.RequestException:
        return None
    return Company(f"{symbol}.NS", name, symbol, "IN") if name else None
