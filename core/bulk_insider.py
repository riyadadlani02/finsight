"""SEC insider transaction data sets: every Form 3/4/5 since 2006, one zip per quarter.

Used for the insider backtest; fetching Form 4s one by one, as the radar does, would take
hundreds of thousands of requests for a few years.
https://www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import requests

from . import edgar

DATA = Path(__file__).resolve().parent.parent / "data"
_DATE = "%d-%b-%Y"  # e.g. 31-OCT-2025


def quarter_url(year: int, q: int) -> str:
    folder = "datastandardsinnovation" if (year, q) >= (2026, 2) else "structureddata"  # the SEC moved them
    return f"https://www.sec.gov/files/{folder}/data/insider-transactions-data-sets/{year}q{q}_form345.zip"


def download(year: int, q: int, dest: Path = DATA / "sec_bulk") -> Path | None:
    """The quarter's zip, cached on disk. None if the SEC hasn't published it."""
    path = Path(dest) / f"{year}q{q}_form345.zip"
    if path.exists():
        return path
    try:
        content = edgar._get(quarter_url(year, q)).content
    except (edgar.EdgarError, requests.RequestException):  # not published, refused, or offline
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(content)
    tmp.replace(path)  # a half-written zip never sits in the cache
    return path


def load_quarter(zip_path: Path) -> pd.DataFrame:
    """Open-market purchases (code P) from original Form 4s, in the shape of insider_tx rows plus `filed`."""
    with zipfile.ZipFile(zip_path) as z:
        # usecols as a filter: columns a quarter doesn't have are simply absent (AFF10B5ONE only exists from 2023).
        read = lambda name, cols: pd.read_csv(z.open(name), sep="\t", dtype=str, usecols=lambda c: c in cols,
                                              keep_default_na=False)
        sub = read("SUBMISSION.tsv", ["ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME",
                                      "ISSUERTRADINGSYMBOL", "AFF10B5ONE"])
        own = read("REPORTINGOWNER.tsv", ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME", "RPTOWNER_RELATIONSHIP",
                                          "RPTOWNER_TITLE"])
        tx = read("NONDERIV_TRANS.tsv", ["ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
                                         "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD"])
    if "AFF10B5ONE" not in sub:
        sub["AFF10B5ONE"] = ""  # not reported before 2023: such trades can't be told apart, so none are excluded
    sub = sub[sub["DOCUMENT_TYPE"] == "4"]
    tx = tx[(tx["TRANS_CODE"] == "P") & (tx["TRANS_ACQUIRED_DISP_CD"] == "A")]
    own = own.drop_duplicates("ACCESSION_NUMBER")  # joint filings: keep the first reporting owner
    df = tx.merge(sub, on="ACCESSION_NUMBER").merge(own, on="ACCESSION_NUMBER", how="left")
    shares = pd.to_numeric(df["TRANS_SHARES"], errors="coerce")
    price = pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce")
    rel = df["RPTOWNER_RELATIONSHIP"].fillna("")
    out = pd.DataFrame({
        "accession": df["ACCESSION_NUMBER"],
        "line_no": df.groupby("ACCESSION_NUMBER").cumcount(),
        "issuer_cik": df["ISSUERCIK"].str.zfill(10),
        "issuer_name": df["ISSUERNAME"].str[:200],
        "ticker": df["ISSUERTRADINGSYMBOL"].str.upper().str.strip().str[:20],
        "insider_cik": df["RPTOWNERCIK"].fillna("").str.zfill(10),
        "insider_name": df["RPTOWNERNAME"].fillna("").str[:200],
        "is_director": rel.str.contains("Director"),
        "is_officer": rel.str.contains("Officer"),
        "is_ten_pct": rel.str.contains("TenPercentOwner"),
        "officer_title": df["RPTOWNER_TITLE"].fillna("").str[:200],
        "tx_date": pd.to_datetime(df["TRANS_DATE"], format=_DATE, errors="coerce"),
        "code": "P",
        "acquired": True,
        "shares": shares,
        "price": price,
        "value": shares * price,
        "planned_10b5_1": df["AFF10B5ONE"].str.lower().isin(["1", "true"]),
        "filed": pd.to_datetime(df["FILING_DATE"], format=_DATE, errors="coerce").dt.date,
    })
    return out[(out["value"] > 0) & out["tx_date"].notna() & out["filed"].notna()].reset_index(drop=True)
