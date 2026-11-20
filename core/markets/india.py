"""India: NSE's own published files only (company list, daily bulk and block deals).

These download as plain files; the NSE and BSE web pages that hold promoter and pledge data block
scripts, so those wait for the Phase 2 data feed (design doc).
"""
from __future__ import annotations

import argparse
import time
from datetime import date
from functools import lru_cache
from io import StringIO
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


# ------------------------------------------------------------ bulk and block deals

MIN_NET_VALUE = 5e7    # ₹5 crore of combined net buying (or selling)
MIN_CLIENTS = 2        # by at least two clients on the same side
COOLDOWN_DAYS = 30     # a run of buying days is one signal, not one per day
_COLS = {"Date": "date", "Symbol": "symbol", "Security Name": "name", "Client Name": "client", "Buy/Sell": "side",
         "Quantity Traded": "quantity", "Trade Price / Wght. Avg. Price": "price"}


def parse_deals(text: str, kind: str) -> pd.DataFrame:
    """One NSE deals file (bulk.csv or block.csv) as rows with a rupee value."""
    df = pd.read_csv(StringIO(text), dtype=str)
    df.columns = [c.strip() for c in df.columns]
    df = df.rename(columns=_COLS)[list(_COLS.values())]
    df["date"] = pd.to_datetime(df["date"].str.strip(), format="%d-%b-%Y").dt.date
    for col in ("symbol", "client", "side"):
        df[col] = df[col].str.strip().str.upper()
    df["name"] = df["name"].str.strip()
    df["quantity"] = pd.to_numeric(df["quantity"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df.dropna(subset=["quantity", "price"])
    df["kind"] = kind
    df["value"] = df["quantity"] * df["price"]
    return df.reset_index(drop=True)


def ingest(engine=None) -> int:
    """Store the latest bulk and block deal files (NSE publishes each trading day's in the evening). Idempotent."""
    from sqlalchemy import insert, select
    from .. import db
    engine, t = engine or db.engine(), db.in_deals
    df = pd.concat([parse_deals(_get(ARCHIVE + f"{kind}.csv").text, kind) for kind in ("bulk", "block")],
                   ignore_index=True)
    key = ["date", "symbol", "client", "side", "quantity", "price", "kind"]
    with engine.connect() as conn:
        seen = {tuple(r) for r in conn.execute(select(*[t.c[k] for k in key]).where(t.c.date.in_(set(df["date"]))))}
    new = [r for r in df[key + ["name"]].to_dict("records") if tuple(r[k] for k in key) not in seen]
    if new:
        with engine.begin() as conn:
            conn.execute(insert(t), new)
    return len(new)


def load_deals(engine=None, since: date | None = None) -> pd.DataFrame:
    from sqlalchemy import select
    from .. import db
    engine, t = engine or db.engine(), db.in_deals
    q = select(t) if since is None else select(t).where(t.c.date >= since)
    df = pd.read_sql(q, engine)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["value"] = df["quantity"] * df["price"]
    return df


def day_summary(deals: pd.DataFrame, min_net_value: float = MIN_NET_VALUE, min_clients: int = MIN_CLIENTS) -> pd.DataFrame:
    """Every stock and day: net value from clients that didn't churn, how many bought or sold, and the signal (+1/-1/0)."""
    cols = ["date", "name", "net_value", "symbol", "ticker", "buyers", "sellers", "churners", "signal", "clients", "churner_names"]
    rows = []
    for (day, symbol), g in deals.groupby(["date", "symbol"]):
        sides = g.groupby("client")["side"].nunique()
        churners = sorted(sides[sides > 1].index)   # bought and sold the same day: trading, not conviction
        kept = g[~g["client"].isin(churners)]
        per_client = kept["value"].where(kept["side"] == "BUY", -kept["value"]).groupby(kept["client"]).sum()
        net, buyers, sellers = float(per_client.sum()), int((per_client > 0).sum()), int((per_client < 0).sum())
        signal = 1 if net >= min_net_value and buyers >= min_clients else -1 if -net >= min_net_value and sellers >= min_clients else 0
        side = per_client > 0 if signal >= 0 else per_client < 0
        rows.append({"date": day, "name": g["name"].iloc[0], "net_value": net, "symbol": symbol, "ticker": f"{symbol}.NS",
                     "buyers": buyers, "sellers": sellers, "churners": len(churners), "signal": signal,
                     "clients": sorted(per_client[side].index), "churner_names": churners})
    return pd.DataFrame(rows, columns=cols)


def deal_signals(deals: pd.DataFrame, min_net_value: float = MIN_NET_VALUE, min_clients: int = MIN_CLIENTS,
                 cooldown_days: int = COOLDOWN_DAYS) -> pd.DataFrame:
    """Days where clients that didn't churn net-bought (+1) or net-sold (-1) a stock in size; one per run."""
    cols = ["symbol", "ticker", "name", "date", "direction", "net_value", "clients", "churners"]
    days = day_summary(deals, min_net_value, min_clients)
    out, last = [], {}
    for d in days[days["signal"] != 0].sort_values("date").to_dict("records"):
        k = (d["symbol"], d["signal"])
        if k in last and (d["date"] - last[k]).days < cooldown_days:
            continue
        out.append({"symbol": d["symbol"], "ticker": d["ticker"], "name": d["name"], "date": d["date"],
                    "direction": d["signal"], "net_value": d["net_value"], "clients": d["clients"],
                    "churners": d["churner_names"]})
        last[k] = d["date"]
    return pd.DataFrame(out, columns=cols)


def record_live_calls(engine=None, today: date | None = None) -> int:
    """Deal signals as live calls, graded against the Nifty 50."""
    from .. import track
    signals = deal_signals(load_deals(engine))
    return sum(track.record("deal", s.ticker, s.direction, abs(s.net_value) / 1e7, s.date, f"{s.symbol}:{s.date}",
                            engine=engine, today=today, market="IN") for s in signals.itertuples())


def main() -> None:
    parser = argparse.ArgumentParser(description="India: NSE bulk and block deals")
    parser.add_argument("command", choices=["ingest"])
    parser.parse_args()
    print(f"{ingest()} new deal lines stored, {record_live_calls()} new calls recorded")


if __name__ == "__main__":
    main()
