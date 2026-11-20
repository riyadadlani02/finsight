"""PostgreSQL schema and engine. Any SQLAlchemy URL works (tests use SQLite)."""
from functools import lru_cache

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer, MetaData, String, Table, Text,
    UniqueConstraint, create_engine, func, inspect, text,
)

from . import config

metadata = MetaData()

# One row per open-market line item on a Form 4.
insider_tx = Table(
    "insider_tx", metadata,
    Column("id", Integer, primary_key=True),
    Column("accession", String(25), nullable=False),
    Column("line_no", Integer, nullable=False),
    Column("issuer_cik", String(10), index=True),
    Column("issuer_name", String(200)),
    Column("ticker", String(20), index=True),
    Column("insider_cik", String(10)),
    Column("insider_name", String(200)),
    Column("is_director", Boolean),
    Column("is_officer", Boolean),
    Column("is_ten_pct", Boolean),
    Column("officer_title", String(200)),
    Column("tx_date", Date, index=True),
    Column("code", String(2)),            # P = open-market purchase, S = sale, ...
    Column("acquired", Boolean),          # True = acquired, False = disposed
    Column("shares", Float),
    Column("price", Float),
    Column("value", Float),
    Column("shares_after", Float),
    Column("ownership", String(1)),       # D direct / I indirect
    Column("planned_10b5_1", Boolean),
    Column("filed", Date),                # Form 4 filing date: when the trade became public
    UniqueConstraint("accession", "line_no", name="uq_insider_tx_line"),
)

# Every Form 4 we've already fetched, so re-scans skip it.
filings_seen = Table(
    "filings_seen", metadata,
    Column("accession", String(25), primary_key=True),
    Column("fetched_at", DateTime, server_default=func.now()),
)

# Clusters that have already been pushed to the webhook.
cluster_alerts = Table(
    "cluster_alerts", metadata,
    Column("key", String(80), primary_key=True),
    Column("created_at", DateTime, server_default=func.now()),
)

# One row per 10-K section compared with the previous 10-K (core/changes.py).
filing_changes = Table(
    "filing_changes", metadata,
    Column("id", Integer, primary_key=True),
    Column("issuer_cik", String(10), index=True),
    Column("ticker", String(20), index=True),
    Column("accession", String(25), nullable=False),
    Column("prev_accession", String(25)),
    Column("filed", Date, index=True),
    Column("section", String(40), nullable=False),
    Column("similarity", Float),
    Column("change_score", Integer),
    Column("added", Integer),
    Column("removed", Integer),
    Column("reworded", Integer),
    Column("market", String(4), server_default="US"),
    UniqueConstraint("accession", "section", name="uq_filing_changes_section"),
)

# Daily adjusted closes (core/prices.py). A row with close NULL marks a ticker Yahoo had no data for.
prices = Table(
    "prices", metadata,
    Column("id", Integer, primary_key=True),
    Column("ticker", String(20), nullable=False, index=True),
    Column("date", Date, nullable=False),
    Column("close", Float),
    Column("fetched", Date, nullable=False),
    UniqueConstraint("ticker", "date", name="uq_prices_day"),
)

# Every signal FinSight produces, graded later against SPY (core/track.py).
calls = Table(
    "calls", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", String(20), nullable=False),         # debate | insider_cluster | filing_change
    Column("ticker", String(20), nullable=False, index=True),
    Column("issuer_cik", String(10)),
    Column("direction", Integer, nullable=False),       # +1 bullish, -1 bearish, 0 neutral (not graded)
    Column("strength", Float),
    Column("public_date", Date, nullable=False),        # when the information became public
    Column("recorded_at", DateTime, server_default=func.now()),
    Column("source_ref", String(80), nullable=False),
    Column("mode", String(10), nullable=False),         # live | backtest
    Column("market", String(4), server_default="US"),   # US | IN | JP
    UniqueConstraint("kind", "source_ref", "mode", name="uq_calls_source"),
)

# When each ticker was last fetched and from what start date (core/prices.py).
price_fetches = Table(
    "price_fetches", metadata,
    Column("ticker", String(20), primary_key=True),
    Column("fetched", Date, nullable=False),
    Column("start", Date, nullable=False),
)

# NSE bulk and block deals, one row per line of the daily files (core/markets/india.py).
in_deals = Table(
    "in_deals", metadata,
    Column("id", Integer, primary_key=True),
    Column("date", Date, nullable=False, index=True),
    Column("symbol", String(20), nullable=False, index=True),
    Column("name", String(200)),
    Column("client", String(200), nullable=False),
    Column("side", String(4), nullable=False),     # BUY | SELL
    Column("quantity", Float, nullable=False),
    Column("price", Float, nullable=False),
    Column("kind", String(5), nullable=False),     # bulk | block
    UniqueConstraint("date", "symbol", "client", "side", "quantity", "price", "kind", name="uq_in_deals_line"),
)

debates = Table(
    "debates", metadata,
    Column("id", Integer, primary_key=True),
    Column("ticker", String(20), index=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("model", String(80)),
    Column("winner", String(10)),
    Column("bull_score", Integer),
    Column("bear_score", Integer),
    Column("verdict_json", Text),
    Column("transcript_json", Text),
)


_ADDED_COLUMNS = [  # (table, column, DDL) for databases created before the column existed
    ("insider_tx", "filed", "DATE"),
    ("calls", "market", "VARCHAR(4) DEFAULT 'US'"),
    ("filing_changes", "market", "VARCHAR(4) DEFAULT 'US'"),
]


@lru_cache(maxsize=4)
def engine(url: str | None = None):
    url = url or config.database_url()
    args = {"connect_timeout": 5} if url.startswith("postgresql") else {}
    eng = create_engine(url, pool_pre_ping=True, connect_args=args)
    metadata.create_all(eng)
    # create_all never alters an existing table; add columns introduced after a database was created.
    for table, column, ddl in _ADDED_COLUMNS:
        if column not in {c["name"] for c in inspect(eng).get_columns(table)}:
            with eng.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return eng


def check() -> tuple[bool, str]:
    """(ok, message) — used by pages to show a clear error instead of a traceback."""
    try:
        with engine().connect():
            return True, "connected"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        engine.cache_clear()
        return False, str(exc).splitlines()[0]
