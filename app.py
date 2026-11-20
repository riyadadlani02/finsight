"""FinSight - three fintech tools in one Streamlit app.  Run:  streamlit run app.py"""
import streamlit as st

from core import config, db, ui
from core.mule import neo4j_store

DEBATE, RADAR, MULE = "views/1_Bull_vs_Bear.py", "views/2_Insider_Radar.py", "views/3_Mule_Ring_Detector.py"

TOOLS = [
    (DEBATE, "Bull vs Bear",
     "Two Claude agents argue for and against a stock using only its latest SEC filings. A third judges "
     "who argued better and names every claim the filings don't back up.",
     "Reads 10-K and 10-Q text, XBRL financials, Yahoo prices", "Start a debate"),
    (RADAR, "Insider radar",
     "Finds companies where several insiders bought their own stock on the open market in the same few "
     "weeks. Grants, option exercises and pre-scheduled trades are left out.",
     "Reads SEC Form 4 filings", "Scan for cluster buys"),
    (MULE, "Mule ring detector",
     "Turns transfers into a graph, scores every account with a graph neural network, and groups the "
     "flagged accounts into the rings that move money between each other.",
     "Reads your transfer CSV, or a synthetic set with planted rings", "Find mule rings"),
]


def home() -> None:
    ui.header("FinSight", "Research tools for public companies and payment fraud. "
              "Nothing here is investment advice.", hero=True)
    for path, name, what, reads, cta in TOOLS:
        left, mid, right = st.columns([1.1, 2.4, 1], vertical_alignment="center", gap="large")
        left.html(f'<p class="fs-tool-name">{name}</p>')
        mid.html(f'<div>{what}</div><div class="fs-tool-reads">{reads}</div>')
        right.page_link(path, label=cta)
        st.divider()

    st.subheader("Setup")
    pg_ok, pg_msg = db.check()
    neo_ok, neo_msg = neo4j_store.check()
    ui.status([
        ("Claude API key", bool(config.anthropic_key()),
         "Ready for debates." if config.anthropic_key() else "Set ANTHROPIC_API_KEY in .env, then restart."),
        ("SEC User-Agent", bool(config.sec_user_agent()),
         "Ready for debates and the radar." if config.sec_user_agent()
         else 'Set SEC_USER_AGENT="Name email" in .env, then restart.'),
        ("Database", pg_ok, "Stores insider trades and past debates." if pg_ok else pg_msg),
        ("Neo4j", neo_ok or None, "Ready for the graph explorer." if neo_ok
         else "Optional. Only the mule detector's Neo4j tab needs it: docker compose up -d neo4j"),
    ])


st.set_page_config(page_title="FinSight", page_icon="📈", layout="wide")
ui.style()
st.navigation([
    st.Page(home, title="Home", default=True),
    st.Page(DEBATE, title="Bull vs Bear"),
    st.Page(RADAR, title="Insider radar"),
    st.Page(MULE, title="Mule ring detector"),
]).run()
