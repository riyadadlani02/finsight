"""FinSight - research tools for public companies and payment fraud.  Run:  streamlit run app.py"""
from datetime import date

import streamlit as st

from core import cockpit, config, db, ui
from core.mule import neo4j_store

DEBATE, RADAR, MULE = "views/1_Bull_vs_Bear.py", "views/2_Insider_Radar.py", "views/3_Mule_Ring_Detector.py"
CHANGES, RECORD = "views/4_Filing_Changes.py", "views/5_Track_Record.py"
OPENS = {"debate": DEBATE, "radar": RADAR, "filings": CHANGES, "record": RECORD}  # cockpit card -> page


@st.cache_data(ttl=300, show_spinner=False)
def cockpit_data(ticker: str, day: date) -> dict:
    return cockpit.card_data(ticker)


def setup() -> None:
    pg_ok, pg_msg = db.check()
    neo_ok, _ = neo4j_store.check()
    ui.status([
        ("Claude API key", bool(config.anthropic_key()),
         "Ready for debates and explanations." if config.anthropic_key() else "Set ANTHROPIC_API_KEY in .env, then restart."),
        ("SEC User-Agent", bool(config.sec_user_agent()),
         "Ready for SEC data." if config.sec_user_agent() else 'Set SEC_USER_AGENT="Name email" in .env, then restart.'),
        ("Database", pg_ok, "Stores filings, trades, prices, debates and calls." if pg_ok else pg_msg),
        ("Neo4j", neo_ok or None, "Ready for the graph explorer." if neo_ok
         else "Optional. Only the mule detector's Neo4j tab needs it: docker compose up -d neo4j"),
    ])


def home() -> None:
    ticker = cockpit.clean_ticker(st.query_params.get("ticker")) or "AAPL"
    with st.spinner(f"Loading {ticker}..."):
        data = cockpit_data(ticker, date.today())
    switched, page = cockpit.render(data)
    if page in OPENS:
        st.switch_page(OPENS[page], query_params={"ticker": ticker})
    if switched:
        clean = cockpit.clean_ticker(switched)
        if clean and clean != ticker:
            st.query_params["ticker"] = clean
            st.rerun()
        elif not clean:
            st.toast("That isn't a ticker. Use letters, numbers, dots or dashes.")
    st.caption("Research tooling only. Nothing here is investment advice.")
    with st.expander("Setup"):
        setup()


st.set_page_config(page_title="FinSight", page_icon="📈", layout="wide")
ui.style()
st.navigation([
    st.Page(home, title="Cockpit", default=True),
    st.Page(DEBATE, title="Bull vs Bear"),
    st.Page(RADAR, title="Insider radar"),
    st.Page(CHANGES, title="Filing changes"),
    st.Page(RECORD, title="Track record"),
    st.Page(MULE, title="Mule ring detector"),
], position="top").run()
