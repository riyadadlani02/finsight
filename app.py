"""FinSight - research tools for public companies and payment fraud.  Run:  streamlit run app.py"""
from datetime import date

import streamlit as st

from core import cockpit, config, db, markets, ui
from core.mule import neo4j_store

DEBATE, RADAR, MULE = "views/1_Bull_vs_Bear.py", "views/2_Insider_Radar.py", "views/3_Mule_Ring_Detector.py"
CHANGES, RECORD = "views/4_Filing_Changes.py", "views/5_Track_Record.py"
SAID = "views/7_Said_vs_Did.py"
DEALS = "views/6_Deals_Radar.py"
OPENS = {"said": SAID, "debate": DEBATE, "radar": RADAR, "deals": DEALS, "filings": CHANGES, "record": RECORD}  # card -> page
FLAGSHIP = {"US": "AAPL", "IN": "RELIANCE.NS", "JP": "7203.T"}  # where switching market lands


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
    switched, page, market = cockpit.render(data)
    if market in FLAGSHIP and market != data["market"]:
        st.query_params["ticker"] = FLAGSHIP[market]
        st.rerun()
    if page in OPENS:
        st.switch_page(OPENS[page], query_params={"ticker": ticker})
    if switched:
        here = markets.get(data["market"])
        company = here.resolve(switched) if cockpit.clean_ticker(switched) else None
        if company and company.ticker != ticker:
            st.query_params["ticker"] = company.ticker
            st.rerun()
        elif not company:
            st.toast(f"Couldn't find {switched[:12]} in {here.name}. Switch market first if it's listed elsewhere.")
    st.caption("Research tooling only. Nothing here is investment advice.")
    with st.expander("Setup"):
        setup()


st.set_page_config(page_title="FinSight", page_icon="📈", layout="wide")
ui.style()
st.navigation([
    st.Page(home, title="Cockpit", default=True),
    st.Page(SAID, title="Said vs did"),
    st.Page(RADAR, title="Insider radar"),
    st.Page(CHANGES, title="Filing changes"),
    st.Page(RECORD, title="Track record"),
    st.Page(DEALS, title="Deals radar (India)"),
    st.Page(MULE, title="Mule ring detector"),
    st.Page(DEBATE, title="Past debates"),
], position="top").run()
