"""Markets FinSight covers. Each answers the same questions, so scoring, grading and the cockpit stay shared.

A security is always keyed by its Yahoo Finance symbol, which is unique across markets:
AAPL (US), RELIANCE.NS (India), 7203.T (Japan).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Company:
    ticker: str      # Yahoo symbol
    name: str
    issuer_id: str   # CIK in the US, NSE symbol in India, EDINET code in Japan ("" until known)
    market: str


@dataclass(frozen=True)
class Market:
    code: str
    name: str
    currency: str
    symbol: str
    benchmark: str          # total-return, like SPY: stock closes are dividend-adjusted
    benchmark_name: str
    tools: frozenset[str]   # filings, debate, insider, deals
    resolve: Callable[[str], "Company | None"]


def _us(text: str) -> Company | None:
    import requests
    from .. import edgar
    ticker = text.strip().upper()
    try:
        hit = edgar.lookup(ticker)
    except (edgar.EdgarError, requests.RequestException):
        return None
    return Company(ticker, hit["name"], hit["cik"], "US")


def _in(text: str) -> Company | None:
    from . import india
    return india.resolve(text)


def _jp(text: str) -> Company | None:
    from . import japan
    return japan.resolve(text)


MARKETS = {
    "US": Market("US", "United States", "USD", "$", "SPY", "S&P 500", frozenset({"filings", "debate", "insider"}), _us),
    "IN": Market("IN", "India", "INR", "₹", "NIFTYBEES.NS", "Nifty 50", frozenset({"deals"}), _in),
    "JP": Market("JP", "Japan", "JPY", "¥", "1306.T", "TOPIX", frozenset({"filings", "debate"}), _jp),
}


def get(code: str) -> Market:
    return MARKETS[code]


def of_ticker(ticker: str) -> str:
    t = ticker.upper()
    return "IN" if t.endswith((".NS", ".BO")) else "JP" if t.endswith(".T") else "US"
