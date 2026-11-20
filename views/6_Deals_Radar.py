from datetime import date, timedelta

import streamlit as st

from core import ui
from core.markets import india

ui.header("Deals radar", "India: bulk and block deals from NSE's daily files. A signal needs at least ₹5 crore of net "
          "buying or selling by two or more clients, after dropping anyone who bought and sold the same stock that "
          "day. A lead to research, not investment advice.")

left, right = st.columns([2, 1], vertical_alignment="bottom")
days = left.slider("Look back", 5, 180, 30, format="%d days")
if right.button("Fetch today's deals", type="primary", width="stretch"):
    with st.spinner("Reading NSE's bulk and block deal files..."):
        try:
            n, calls = india.ingest(), india.record_live_calls()
            st.success(f"{n} new deal lines stored, {calls} new signals recorded for the track record.")
        except Exception as exc:  # noqa: BLE001 - NSE unreachable or file changed: say so plainly
            st.error(f"Couldn't read NSE's files: {str(exc).splitlines()[0]}")

deals = india.load_deals(since=date.today() - timedelta(days=days))
if deals.empty:
    st.info("No deals stored yet. Fetch today's deals; history builds up from the first day you fetch, so run it "
            "each trading evening (or schedule `python -m core.markets.india ingest`).")
    st.stop()

summary = india.day_summary(deals).sort_values(["date", "net_value"], ascending=[False, False])
signals = india.deal_signals(deals)
crore = lambda v: f"₹{v / 1e7:+,.1f} cr"

with ui.panel("signals"):
    st.subheader(f"{len(signals)} signal{'s' if len(signals) != 1 else ''} in {days} days")
    if signals.empty:
        st.caption("Nothing met the bar. The table below shows every big deal day and why it fell short.")
    for s in signals.sort_values("date", ascending=False).itertuples():
        verb = "bought" if s.direction > 0 else "sold"
        st.markdown(f"**{s.name}** ({s.symbol}), {s.date:%d %b}: {crore(s.net_value)} net {verb} by {', '.join(s.clients)}"
                    + (f". Left out as same-day traders: {', '.join(s.churners)}" if s.churners else ""))

with ui.panel("days"):
    st.subheader("Every deal day")
    view = summary.assign(net=summary["net_value"] / 1e7,
                          why=[("Signal: buying" if r.signal > 0 else "Signal: selling") if r.signal else
                               ("Under ₹5 cr net" if abs(r.net_value) < india.MIN_NET_VALUE else "Only one client on that side")
                               for r in summary.itertuples()])
    st.dataframe(view, hide_index=True, width="stretch",
                 column_order=["date", "symbol", "name", "net", "buyers", "sellers", "churners", "why"],
                 column_config={"net": st.column_config.NumberColumn("net ₹ cr", format="%+.1f"),
                                "churners": st.column_config.NumberColumn("same-day traders")})
    st.caption("Net counts only clients that didn't both buy and sell the stock that day. Bulk deals are trades of "
               "at least 0.5% of a company's shares in a day; block deals are single trades of at least ₹10 crore.")
