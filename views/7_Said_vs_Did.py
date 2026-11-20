import streamlit as st

from core import cockpit, config, edgar, markets, promises, ui

ui.header("Said vs did", "What management promised in past annual reports, and what later reports show happened. "
          "Claude reads the reports; every verdict quotes the later report, and the code checks each quote is "
          "really there. Research aid only, not investment advice.")

LABELS = {"kept": ("Kept", ui.BULL), "partly": ("Partly kept", ui.LINE), "missed": ("Missed", ui.BEAR),
          "dropped": ("Not mentioned again", ui.MUTED), "open": ("Still open", ui.MUTED),
          "unchecked": ("Couldn't verify", ui.AMBER)}

ticker = cockpit.clean_ticker(st.text_input("Ticker", value=st.query_params.get("ticker", "AAPL"), max_chars=20)) or ""
if not ticker:
    st.stop()
st.query_params["ticker"] = ticker
market = markets.get(markets.of_ticker(ticker))
if "filings" not in market.tools:
    st.info(f"Said vs did reads annual reports, which aren't available for {market.name} yet. They come with the "
            "India data feed (Phase 2). The Deals radar covers India today.")
    st.stop()

ledger = promises.stored(ticker)
label = "Refresh with the latest report" if ledger else "Check promises"
if not config.anthropic_key():
    st.caption("Add ANTHROPIC_API_KEY to check promises.")
elif st.button(label, type="primary"):
    with st.spinner(f"Reading {ticker}'s last {promises.YEARS} annual reports and checking each promise..."):
        try:
            ledger = promises.run(ticker)
        except (edgar.EdgarError, ValueError) as exc:
            st.info(str(exc))
        except Exception as exc:  # noqa: BLE001 - API or network errors shown to the user
            st.error(f"Couldn't finish: {str(exc).splitlines()[0]}")

if not ledger:
    st.caption(f"Nothing checked for {ticker} yet. Checking reads four annual reports and makes a few Claude calls.")
    st.stop()

s = ledger["score"]
with ui.panel("score"):
    cols = st.columns(6)
    cols[0].metric("Credibility", "Too few to judge" if s["too_few"] else f"{s['credibility']:.0%}",
                   help=f"Kept plus half of partly kept, over resolved promises. Needs at least {promises.MIN_RESOLVED} resolved.")
    for col, key in zip(cols[1:], ("kept", "partly", "missed", "dropped", "open")):
        col.metric(LABELS[key][0], s[key])
    st.caption("\"Not mentioned again\" means later reports never brought the promise up, not that it failed.")

for key in ("missed", "partly", "dropped", "kept", "unchecked", "open"):
    rows = [p for p in ledger["promises"] if p["verdict"] == key]
    if not rows:
        continue
    name, color = LABELS[key]
    with st.expander(f"{name} ({len(rows)})", expanded=key in ("missed", "partly")):
        for p in rows:
            ui.point(f"{p['promise']} (by {p['deadline']}, said in the report filed {p['made_filed']})",
                     f"Said: \"{p['quote']}\"" + (f"\nLater report ({p['evidence_filed']}): \"{p['evidence_quote']}\""
                                                  if p.get("evidence_quote") else ""), color)
