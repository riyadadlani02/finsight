import pandas as pd
import streamlit as st
from sqlalchemy import func, select

from core import config, db, insider, ui

ui.header("Insider radar", "Several insiders buying on the open market in the same window is a far stronger signal than one. Source: SEC Form 4. A lead to research, not investment advice.")

ok, msg = db.check()
if not ok:
    st.error(f"The database isn't reachable: {msg}\n\nStart it with `docker compose up -d postgres`.")
    st.stop()
engine = db.engine()

# ------------------------------------------------------------------ ingest
with st.sidebar:
    st.header("Fetch Form 4 filings")
    watch = st.text_input("Watchlist tickers", placeholder="AAPL, MSFT, NVDA")
    if st.button("Scan watchlist") and watch.strip():
        bar = st.progress(0.0)
        try:
            n = insider.scan_tickers([t for t in watch.replace(",", " ").split()],
                                     progress=lambda i, total: bar.progress(i / total))
            st.success(f"{n} new transactions stored")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    st.divider()
    days = st.number_input("Whole market: days back", 1, 30, 3,
                           help="Every Form 4 filed. Roughly 3-5 minutes per trading day the first time; "
                                "filings already fetched are skipped.")
    if st.button("Scan whole market"):
        bar = st.progress(0.0, "Reading the daily index...")
        try:
            n = insider.scan_market(int(days), progress=lambda i, total: bar.progress(i / total, f"{i}/{total} filings"))
            st.success(f"{n} new transactions stored")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    with engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(db.insider_tx)).scalar()
        latest = conn.execute(select(func.max(db.insider_tx.c.tx_date))).scalar()
    st.caption(f"Database: {total:,} transactions, latest {latest or 'n/a'}")

# ------------------------------------------------------------------ detect
a, b, c, d = st.columns(4)
window = a.slider("Window (days)", 7, 90, 30)
min_insiders = b.slider("Min. distinct insiders", 2, 8, 3)
min_value = c.number_input("Min. total bought ($)", 0, 50_000_000, 100_000, step=50_000)
lookback = d.slider("Look back (days)", 30, 365, 180)
skip_plans = st.checkbox("Ignore pre-scheduled (10b5-1) trades", True)

purchases = insider.load_purchases(engine, lookback, skip_plans)
clusters = insider.find_clusters(purchases, window, min_insiders, float(min_value))

if clusters.empty:
    st.info("No cluster buys match. Fetch filings from the sidebar, or loosen the thresholds."
            if total else "No data yet. Use the sidebar to fetch Form 4 filings.")
    st.stop()

st.subheader(f"{len(clusters)} cluster buy{'s' if len(clusters) != 1 else ''}")
picked = st.dataframe(
    clusters.drop(columns=["issuer_cik", "names"]), hide_index=True, width="stretch",
    on_select="rerun", selection_mode="single-row",
    column_config={
        "total_value": st.column_config.NumberColumn("total bought", format="$%.0f"),
        "score": st.column_config.ProgressColumn("score", min_value=0, max_value=100, format="%.0f"),
        "top_execs": "CEO/CFO/Chair",
    },
)
row = clusters.iloc[picked.selection.rows[0] if picked.selection.rows else 0]

st.subheader(f"{row.ticker} · {row.issuer_name}")
st.write(f"**{row.insiders} insiders** bought **${row.total_value:,.0f}** between {row.first_buy} and {row.last_buy}.")
detail = purchases[(purchases["issuer_cik"] == row.issuer_cik)
                   & (purchases["tx_date"] >= pd.Timestamp(row.first_buy))
                   & (purchases["tx_date"] <= pd.Timestamp(row.last_buy))].sort_values("tx_date")
detail = detail.assign(
    tx_date=detail["tx_date"].dt.date,
    role=[t or ("Director" if dr else "10% owner" if tp else "") for t, dr, tp in
          zip(detail["officer_title"], detail["is_director"], detail["is_ten_pct"])],
    filing=["https://www.sec.gov/Archives/edgar/data/%d/%s/%s-index.htm" % (int(cik), acc.replace("-", ""), acc)
            for cik, acc in zip(detail["issuer_cik"], detail["accession"])],
)
st.dataframe(
    detail[["tx_date", "insider_name", "role", "shares", "price", "value", "shares_after", "filing"]],
    hide_index=True, width="stretch",
    column_config={"price": st.column_config.NumberColumn(format="$%.2f"),
                   "value": st.column_config.NumberColumn(format="$%.0f"),
                   "filing": st.column_config.LinkColumn(display_text="Form 4")},
)

left, right = st.columns(2)
if row.ticker and left.button(f"Run a Bull vs Bear debate on {row.ticker}", type="primary"):
    st.session_state["debate_ticker"] = row.ticker
    st.switch_page("views/1_Bull_vs_Bear.py")
if config.alert_webhook():
    if right.button("Send new clusters to webhook"):
        st.success(f"{insider.send_new_alerts(clusters, engine)} alerts sent")
else:
    right.caption("Set ALERT_WEBHOOK_URL in .env to push alerts (e.g. to Slack).")
