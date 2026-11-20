"""Track record: every FinSight signal becomes a dated call, graded against SPY.

Calls are dated by when the information became public and entered at the first
close after that, so nothing here uses information a reader didn't have yet.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Callable

import numpy as np
import pandas as pd
from sqlalchemy import insert, select

from . import db

HORIZONS = (30, 90, 180)
BENCHMARK = "SPY"
MIN_CALLS, MIN_MONTHS = 20, 6


def entry_date(public_date: date) -> date:
    """The first trading day after the news (business days; holidays are handled by grading's next-close rule)."""
    return (pd.Timestamp(public_date) + pd.offsets.BDay(1)).date()


def record(kind: str, ticker: str, direction: int, strength: float, public_date: date, source_ref: str,
           mode: str = "live", issuer_cik: str | None = None, engine=None, today: date | None = None) -> bool:
    """Store a call once. A live call must be recorded by its entry date, or it would be graded with hindsight."""
    engine, today, t = engine or db.engine(), today or date.today(), db.calls
    if mode == "live" and today > entry_date(public_date):
        return False
    with engine.begin() as conn:
        if conn.execute(select(t.c.id).where(t.c.kind == kind, t.c.source_ref == source_ref, t.c.mode == mode)).first():
            return False
        conn.execute(insert(t).values(kind=kind, ticker=ticker.upper(), issuer_cik=issuer_cik, direction=int(direction),
                                      strength=float(strength), public_date=public_date, source_ref=source_ref,
                                      mode=mode, recorded_at=datetime.now()))
    return True


def load_calls(engine=None, mode: str | None = None) -> pd.DataFrame:
    engine, t = engine or db.engine(), db.calls
    q = select(t) if mode is None else select(t).where(t.c.mode == mode)
    df = pd.read_sql(q, engine)
    df["public_date"] = pd.to_datetime(df["public_date"]).dt.date
    return df


def grade(calls: pd.DataFrame, closes: pd.DataFrame, today: date, horizons=HORIZONS) -> pd.DataFrame:
    """Excess return over SPY from the first close after public_date to the first close h calendar days later."""
    out = calls.copy()
    idx = closes.index
    out["entry"] = pd.NaT
    for h in horizons:
        out[f"excess_{h}"], out[f"hit_{h}"], out[f"status_{h}"] = np.nan, pd.NA, "no_price"
    for i, row in calls.iterrows():
        pos = idx.searchsorted(pd.Timestamp(row["public_date"]), side="right")
        if pos >= len(idx):
            for h in horizons:
                out.at[i, f"status_{h}"] = "pending"
            continue
        entry = idx[pos]
        out.at[i, "entry"] = entry
        series = closes[row["ticker"]] if row["ticker"] in closes else pd.Series(np.nan, index=idx)
        bench = closes[BENCHMARK] if BENCHMARK in closes else pd.Series(np.nan, index=idx)
        for h in horizons:
            target = entry + pd.Timedelta(days=h)
            if target.date() > today:
                out.at[i, f"status_{h}"] = "pending"
                continue
            end = idx.searchsorted(target, side="left")
            if end >= len(idx):
                out.at[i, f"status_{h}"] = "pending"
                continue
            p0, p1, b0, b1 = series.iloc[pos], series.iloc[end], bench.iloc[pos], bench.iloc[end]
            if pd.isna(p0) or pd.isna(p1) or pd.isna(b0) or pd.isna(b1):
                continue  # stays no_price: delisted, or never listed on Yahoo
            excess = (p1 / p0 - 1) - (b1 / b0 - 1)
            out.at[i, f"excess_{h}"] = excess
            out.at[i, f"hit_{h}"] = bool(np.sign(excess) == np.sign(row["direction"]))
            out.at[i, f"status_{h}"] = "graded"
    return out


def block_ci(values: pd.Series, months: pd.Series, stat: Callable = np.mean, n: int = 2000,
             seed: int = 0) -> tuple[float, float]:
    """95% interval resampling whole months: calls in the same month share a market, so they aren't independent."""
    groups = [g.to_numpy(dtype=float) for _, g in pd.Series(values.to_numpy()).groupby(months.to_numpy())]
    if len(groups) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    stats = [stat(np.concatenate([groups[k] for k in rng.integers(0, len(groups), len(groups))])) for _ in range(n)]
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def _month(d) -> str:
    return f"{d:%Y-%m}"


def scoreboard(graded: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """One row per (kind, direction, mode). Neutral calls (direction 0) aren't graded."""
    cols = ["kind", "direction", "mode", "n", "months", "hit_rate", "hit_lo", "hit_hi", "mean", "mean_lo", "mean_hi",
            "median", "too_few", "dropped"]
    rows = []
    status, excess, hit = f"status_{horizon}", f"excess_{horizon}", f"hit_{horizon}"
    for (kind, direction, mode), g in graded[graded["direction"] != 0].groupby(["kind", "direction", "mode"]):
        ok = g[g[status] == "graded"]
        months = ok["public_date"].map(_month)
        hits = ok[hit].astype(float)
        row = {"kind": kind, "direction": int(direction), "mode": mode, "n": len(ok), "months": months.nunique(),
               "dropped": int((g[status] == "no_price").sum())}
        if len(ok):
            row.update(hit_rate=hits.mean(), mean=ok[excess].mean(), median=ok[excess].median())
            row["hit_lo"], row["hit_hi"] = block_ci(hits, months)
            row["mean_lo"], row["mean_hi"] = block_ci(ok[excess], months)
        row["too_few"] = row["n"] < MIN_CALLS or row["months"] < MIN_MONTHS
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def event_curve(calls: pd.DataFrame, closes: pd.DataFrame, days: int = 180) -> pd.DataFrame:
    """Average cumulative excess return by trading day since entry, signed by each call's direction."""
    idx, paths, months = closes.index, [], []
    for _, row in calls[calls["direction"] != 0].iterrows():
        pos = idx.searchsorted(pd.Timestamp(row["public_date"]), side="right")
        if pos >= len(idx) or row["ticker"] not in closes or BENCHMARK not in closes:
            continue
        p = closes[row["ticker"]].iloc[pos:pos + days + 1].to_numpy()
        b = closes[BENCHMARK].iloc[pos:pos + days + 1].to_numpy()
        if not len(p) or np.isnan(p[0]) or np.isnan(b[0]):
            continue
        path = np.full(days + 1, np.nan)
        path[:len(p)] = row["direction"] * ((p / p[0] - 1) - (b / b[0] - 1))
        paths.append(path)
        months.append(_month(row["public_date"]))
    if not paths:
        return pd.DataFrame(columns=["mean", "lo", "hi"])
    m = np.vstack(paths)
    out = pd.DataFrame({"mean": np.nanmean(m, axis=0)}, index=pd.RangeIndex(days + 1, name="day"))
    out["lo"], out["hi"] = np.nan, np.nan
    month_s = pd.Series(months)
    for d in range(0, days + 1, 5):
        col = pd.Series(m[:, d])
        keep = col.notna()
        if keep.sum() >= 2:
            out.loc[d, ["lo", "hi"]] = block_ci(col[keep], month_s[keep], n=500)
    out[["lo", "hi"]] = out[["lo", "hi"]].interpolate(limit_area="inside")
    return out


def filing_cutoffs(engine=None) -> tuple[float, float] | None:
    """20th and 80th percentiles of backtest filing-change scores; live calls use these, never a screen's own spread."""
    t = db.calls
    with (engine or db.engine()).connect() as conn:
        s = [r[0] for r in conn.execute(select(t.c.strength).where(t.c.kind == "filing_change", t.c.mode == "backtest"))]
    if not s:
        return None
    lo, hi = np.percentile(s, [20, 80])
    return float(lo), float(hi)


def filing_direction(score: float, cutoffs: tuple[float, float]) -> int:
    """Lazy Prices: big changers underperform (-1), near-identical filings outperform (+1)."""
    lo, hi = cutoffs
    return -1 if score >= hi else 1 if score <= lo else 0
