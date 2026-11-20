"""Daily adjusted closes from Yahoo Finance, cached in the database so each ticker is fetched once a day."""
from __future__ import annotations

from datetime import date
from typing import Callable

import pandas as pd
from sqlalchemy import delete, insert, select

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
    engine, fetch, t, f = engine or db.engine(), fetch or _yahoo, db.prices, db.price_fetches
    tickers = list(dict.fromkeys(tickers))
    with engine.connect() as conn:
        known = {r.ticker: r for r in conn.execute(select(f).where(f.c.ticker.in_(tickers)))}
    # Refetch once a day, or when asked for an earlier start than last time (a late listing is not a gap).
    stale = [x for x in tickers if x not in known or known[x].fetched < date.today() or start < known[x].start]
    if stale:
        got = fetch(stale, start)
        with engine.begin() as conn:
            for x in stale:
                s = got[x].dropna() if x in got else pd.Series(dtype=float)
                if s.empty and x in known:
                    continue  # a failed or rate-limited download keeps yesterday's history and retries next time
                conn.execute(delete(t).where(t.c.ticker == x))
                if not s.empty:
                    conn.execute(insert(t), [{"ticker": x, "date": d.date(), "close": float(v), "fetched": date.today()}
                                             for d, v in s.items()])
                conn.execute(delete(f).where(f.c.ticker == x))
                conn.execute(insert(f).values(ticker=x, fetched=date.today(), start=min(start, known[x].start) if x in known else start))
    with engine.connect() as conn:
        df = pd.read_sql(select(t.c.ticker, t.c.date, t.c.close).where(t.c.ticker.in_(tickers), t.c.date >= start), conn)
    wide = df.pivot_table(index="date", columns="ticker", values="close") if not df.empty else pd.DataFrame()
    wide.index = pd.to_datetime(wide.index)
    return wide.reindex(columns=tickers).dropna(how="all").sort_index()
