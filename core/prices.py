"""Daily adjusted closes from Yahoo Finance, cached in the database so each ticker is fetched once a day."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

import pandas as pd
from sqlalchemy import delete, func, insert, select

from . import db

Fetch = Callable[[list[str], date], pd.DataFrame]


def _yahoo(tickers: list[str], start: date) -> pd.DataFrame:
    import yfinance as yf
    data = yf.download(tickers, start=start, auto_adjust=True, progress=False, threads=True)
    if data.empty:
        return pd.DataFrame()
    close = data["Close"]
    return close.to_frame(tickers[0]) if isinstance(close, pd.Series) else close


def closes(tickers: list[str], start: date, engine=None, fetch: Fetch | None = None) -> pd.DataFrame:
    """One column per ticker (in the order given), indexed by date. A ticker with no data is an all-NaN column."""
    engine, fetch, t = engine or db.engine(), fetch or _yahoo, db.prices
    tickers = list(dict.fromkeys(tickers))
    with engine.connect() as conn:
        fresh = dict(conn.execute(select(t.c.ticker, func.max(t.c.fetched)).where(t.c.ticker.in_(tickers))
                                  .group_by(t.c.ticker)).all())
        first = dict(conn.execute(select(t.c.ticker, func.min(t.c.date)).where(t.c.ticker.in_(tickers))
                                  .group_by(t.c.ticker)).all())
    stale = [x for x in tickers if fresh.get(x) is None or fresh[x] < date.today() or first[x] > start + timedelta(days=7)]  # data starts at the first trading day after `start`
    if stale:
        got = fetch(stale, start)
        with engine.begin() as conn:
            conn.execute(delete(t).where(t.c.ticker.in_(stale)))
            for x in stale:
                s = got[x].dropna() if x in got else pd.Series(dtype=float)
                # A row with no close records that we looked today, so a delisted ticker isn't refetched.
                rows = [{"ticker": x, "date": d.date(), "close": float(v), "fetched": date.today()} for d, v in s.items()] \
                    or [{"ticker": x, "date": start, "close": None, "fetched": date.today()}]
                conn.execute(insert(t), rows)
    with engine.connect() as conn:
        df = pd.read_sql(select(t.c.ticker, t.c.date, t.c.close).where(t.c.ticker.in_(tickers), t.c.date >= start), conn)
    wide = df.pivot_table(index="date", columns="ticker", values="close", dropna=False) if not df.empty else pd.DataFrame()
    wide.index = pd.to_datetime(wide.index)
    return wide.reindex(columns=tickers).dropna(how="all").sort_index()
