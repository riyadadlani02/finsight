"""Insider cluster-buy radar.

Pulls Form 4 filings from EDGAR into Postgres and flags companies where several
different insiders made open-market purchases (transaction code P) inside a
short window. Routine grants, option exercises and tax withholding are ignored.

CLI (for cron):  python -m core.insider scan --days 2
"""
from __future__ import annotations

import argparse
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from typing import Callable

import pandas as pd
import requests
from sqlalchemy import insert, select

from . import config, db, edgar

Progress = Callable[[int, int], None]


# -------------------------------------------------------------- ingestion

def _store(engine, accession: str, rows: list[dict], filed: date | None = None) -> int:
    with engine.begin() as conn:
        if conn.execute(select(db.filings_seen.c.accession).where(db.filings_seen.c.accession == accession)).first():
            return 0
        conn.execute(insert(db.filings_seen).values(accession=accession))
        if rows:
            conn.execute(insert(db.insider_tx), [{**r, "filed": filed} for r in rows])
    return len(rows)


def _ingest(engine, todo: list[tuple[str, str, date]], progress: Progress | None) -> int:
    """Fetch and store [(accession, txt_url, filed)], skipping ones already seen."""
    with engine.connect() as conn:
        seen = {r[0] for r in conn.execute(select(db.filings_seen.c.accession))}
    todo = [t for t in todo if t[0] not in seen]

    filed = {t[0]: t[2] for t in todo}

    def fetch(item):
        accession, url, _ = item
        try:
            return accession, edgar.fetch_form4(url, accession)
        except (edgar.EdgarError, requests.RequestException):
            return accession, None  # leave unseen so the next scan retries it

    stored = 0
    with ThreadPoolExecutor(max_workers=4) as pool:  # edgar._get throttles globally
        for i, (accession, rows) in enumerate(pool.map(fetch, todo), 1):
            if rows is not None:
                stored += _store(engine, accession, rows, filed[accession])
            if progress:
                progress(i, len(todo))
    return stored


def scan_market(days: int = 3, engine=None, progress: Progress | None = None) -> int:
    """Ingest every Form 4 filed in the last `days` days. ~1-2k filings per trading day."""
    engine = engine or db.engine()
    todo: list[tuple[str, str, date]] = []
    for offset in range(days):
        day = date.today() - timedelta(days=offset)
        todo += [(accession, url, day) for accession, url in edgar.daily_form4_index(day)]
    stored = _ingest(engine, todo, progress)
    record_live_calls(engine)
    return stored


def scan_tickers(tickers: list[str], per_ticker: int = 60, engine=None, progress: Progress | None = None) -> int:
    """Ingest the most recent Form 4s for specific companies (fast; good for a watchlist)."""
    engine = engine or db.engine()
    todo = []
    for ticker in tickers:
        cik = edgar.lookup(ticker)["cik"]
        todo += [(f.accession, f.txt_url, date.fromisoformat(f.filed))
                 for f in edgar.recent_filings(cik, ("4",), per_ticker)]
    stored = _ingest(engine, todo, progress)
    record_live_calls(engine)
    return stored


# -------------------------------------------------------------- detection

def load_purchases(engine=None, lookback_days: int = 180, exclude_10b5_1: bool = True) -> pd.DataFrame:
    """Open-market buys only: code P, shares acquired, non-zero price."""
    engine = engine or db.engine()
    t = db.insider_tx
    q = select(t).where(t.c.code == "P", t.c.acquired.is_(True), t.c.value > 0,
                        t.c.tx_date >= date.today() - timedelta(days=lookback_days))
    df = pd.read_sql(q, engine)
    if exclude_10b5_1 and not df.empty:
        df = df[~df["planned_10b5_1"].fillna(False).astype(bool)]
    df["tx_date"] = pd.to_datetime(df["tx_date"])
    return df


def _is_top_exec(title: str) -> bool:
    title = (title or "").lower()
    return any(k in title for k in ("chief executive", "ceo", "chief financial", "cfo", "president", "chair"))


def find_clusters(purchases: pd.DataFrame, window_days: int = 30, min_insiders: int = 3,
                  min_total_value: float = 100_000) -> pd.DataFrame:
    """One row per company: its strongest qualifying buying window.

    A window qualifies when at least `min_insiders` distinct insiders bought
    within `window_days` and together spent at least `min_total_value`.
    """
    cols = ["issuer_cik", "ticker", "issuer_name", "insiders", "top_execs", "total_value",
            "trades", "first_buy", "last_buy", "score", "names"]
    out = []
    for issuer_cik, grp in purchases.groupby("issuer_cik"):
        grp = grp.sort_values("tx_date")
        best = None
        for end in grp["tx_date"].unique():
            win = grp[(grp["tx_date"] <= end) & (grp["tx_date"] > end - pd.Timedelta(days=window_days))]
            n, value = win["insider_cik"].nunique(), win["value"].sum()
            if n < min_insiders or value < min_total_value:
                continue
            # prefer more insiders, then more recent, then more money
            key = (n, end, value)
            if best is None or key > best[0]:
                best = (key, win)
        if best is None:
            continue
        win = best[1]
        people = win.groupby("insider_cik").agg(
            name=("insider_name", "first"), title=("officer_title", "first"), value=("value", "sum"),
        ).sort_values("value", ascending=False)
        execs = int(people["title"].map(_is_top_exec).sum())
        days_ago = (pd.Timestamp(date.today()) - win["tx_date"].max()).days
        # Heuristic 0-100: breadth matters most, then conviction ($), seniority, freshness.
        score = (min(len(people), 6) / 6 * 45
                 + min(math.log10(max(win["value"].sum(), 1)) / 7, 1) * 25
                 + min(execs, 2) / 2 * 15
                 + max(0, 1 - days_ago / 60) * 15)
        out.append({
            "issuer_cik": issuer_cik, "ticker": win["ticker"].iloc[-1], "issuer_name": win["issuer_name"].iloc[-1],
            "insiders": len(people), "top_execs": execs, "total_value": float(win["value"].sum()),
            "trades": len(win), "first_buy": win["tx_date"].min().date(), "last_buy": win["tx_date"].max().date(),
            "score": round(score, 1),
            "names": ", ".join(f"{name} ({title})" if title else name
                               for name, title in zip(people["name"], people["title"])),
        })
    result = pd.DataFrame(out, columns=cols)
    return result.sort_values("score", ascending=False).reset_index(drop=True)


def cluster_events(purchases: pd.DataFrame, window_days: int = 30, min_insiders: int = 3,
                   min_total_value: float = 100_000) -> pd.DataFrame:
    """Each moment a cluster became visible to the public, for grading without hindsight.

    On every filing date, look back `window_days` of trades that had been filed by then; the
    first date the thresholds are met is the event. The issuer can't fire again until the window
    has passed, so a cluster that keeps growing is one event.
    """
    cols = ["issuer_cik", "ticker", "public_date", "first_buy", "insiders", "total_value", "score"]
    if purchases.empty or "filed" not in purchases:
        return pd.DataFrame(columns=cols)
    df = purchases.dropna(subset=["filed"]).assign(tx_date=lambda d: pd.to_datetime(d["tx_date"]),
                                                   filed=lambda d: pd.to_datetime(d["filed"]))
    window, out = pd.Timedelta(days=window_days), []
    for issuer_cik, g in df.groupby("issuer_cik"):
        last = None
        for day in sorted(g["filed"].unique()):
            if last is not None and day - last < window:
                continue
            seen = g[(g["filed"] <= day) & (g["tx_date"] > day - window) & (g["tx_date"] <= day)]
            if seen["insider_cik"].nunique() < min_insiders or seen["value"].sum() < min_total_value:
                continue
            scored = find_clusters(seen, window_days, min_insiders, min_total_value)
            out.append({"issuer_cik": issuer_cik, "ticker": seen["ticker"].iloc[-1], "public_date": day.date(),
                        "first_buy": seen["tx_date"].min().date(), "insiders": seen["insider_cik"].nunique(),
                        "total_value": float(seen["value"].sum()),
                        "score": float(scored["score"].iloc[0]) if not scored.empty else float("nan")})
            last = day
    return pd.DataFrame(out, columns=cols)


def record_live_calls(engine=None) -> int:
    """Record clusters found with the radar's default thresholds as live calls (fixed, so hindsight can't tune them)."""
    from . import track
    events = cluster_events(load_purchases(engine, lookback_days=180))
    return sum(track.record("insider_cluster", e.ticker, 1, e.score, e.public_date, f"{e.issuer_cik}:{e.first_buy}",
                            issuer_cik=e.issuer_cik, engine=engine) for e in events.itertuples())


# ----------------------------------------------------------------- alerts

def send_new_alerts(clusters: pd.DataFrame, engine=None) -> int:
    """POST each not-yet-alerted cluster to ALERT_WEBHOOK_URL. Returns how many were sent."""
    url = config.alert_webhook()
    if not url or clusters.empty:
        return 0
    engine = engine or db.engine()
    sent = 0
    for row in clusters.itertuples():
        key = f"{row.issuer_cik}:{row.first_buy}:{row.insiders}"
        with engine.begin() as conn:
            if conn.execute(select(db.cluster_alerts.c.key).where(db.cluster_alerts.c.key == key)).first():
                continue
            text = (f"Insider cluster buy: {row.ticker} ({row.issuer_name}) - {row.insiders} insiders bought "
                    f"${row.total_value:,.0f} between {row.first_buy} and {row.last_buy}. "
                    f"Score {row.score}. {row.names}")
            requests.post(url, json={"text": text}, timeout=15).raise_for_status()
            conn.execute(insert(db.cluster_alerts).values(key=key))
            sent += 1
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description="Insider cluster-buy radar")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scan = sub.add_parser("scan", help="ingest recent Form 4s, detect clusters, send alerts")
    scan.add_argument("--days", type=int, default=2)
    scan.add_argument("--window", type=int, default=30)
    scan.add_argument("--min-insiders", type=int, default=3)
    scan.add_argument("--min-value", type=float, default=100_000)
    args = parser.parse_args()

    stored = scan_market(args.days, progress=lambda i, n: print(f"\r{i}/{n} filings", end="", flush=True))
    clusters = find_clusters(load_purchases(), args.window, args.min_insiders, args.min_value)
    print(f"\n{stored} new transactions stored, {len(clusters)} clusters, {send_new_alerts(clusters)} alerts sent")
    if not clusters.empty:
        print(clusters[["ticker", "insiders", "total_value", "last_buy", "score"]].to_string(index=False))


if __name__ == "__main__":
    main()
