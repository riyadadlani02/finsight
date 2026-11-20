from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from core import prices, track, ui

ui.header("Track record", "Every debate verdict, insider cluster and filing change becomes a dated call, graded "
          "against the S&P 500 once its horizon has passed. Past results don't promise future ones.")

LABELS = {("debate", 1): "Bull wins a debate", ("debate", -1): "Bear wins a debate",
          ("insider_cluster", 1): "Insider cluster buy",
          ("filing_change", 1): "10-K barely changed (expect to beat the market)",
          ("filing_change", -1): "10-K changed a lot (expect to lag the market)",
          ("deal", 1): "Big deal buying (India)", ("deal", -1): "Big deal selling (India)"}


def pct(x: float) -> str:
    return "n/a" if pd.isna(x) else f"{x:+.1%}"


def interval(lo: float, hi: float) -> str:
    return "needs 2+ months" if pd.isna(lo) else f"{lo:+.1%} to {hi:+.1%}"


@st.cache_data(ttl=3600, show_spinner=False)
def graded_calls(mode: str, today: date) -> pd.DataFrame:
    calls = track.load_calls(mode=mode)
    if calls.empty:
        return calls
    closes = prices.closes(sorted(set(calls["ticker"])) + track.benchmarks_for(calls),
                           min(calls["public_date"]) - timedelta(days=7))
    return track.grade(calls, closes, today)


@st.cache_data(ttl=3600, show_spinner=False)
def curve(mode: str, kind: str, today: date) -> pd.DataFrame:
    calls = track.load_calls(mode=mode)
    calls = calls[calls["kind"] == kind]
    closes = prices.closes(sorted(set(calls["ticker"])) + track.benchmarks_for(calls), min(calls["public_date"]) - timedelta(days=7))
    return track.event_curve(calls, closes)


left, right = st.columns([1, 1])
mode = (left.segmented_control("Calls", ["Backtest", "Live"], default="Backtest") or "Backtest").lower()
horizon = right.segmented_control("Horizon", [30, 90, 180], default=90, format_func=lambda h: f"{h} days") or 90

with st.spinner("Grading calls against S&P 500 prices (first run downloads prices)..."):
    graded = graded_calls(mode, date.today())

if graded.empty:
    st.info("No live calls yet. They're recorded when you run a debate, scan the insider radar, or score a 10-K."
            if mode == "live" else "No backtest yet. Run one below to grade the signals on history.")
else:
    if mode == "backtest" and (graded["kind"] == "filing_change").any():
        s = track.spread(graded[graded["kind"] == "filing_change"], horizon)
        with ui.panel("spread"):
            st.subheader("Do companies that rewrite their 10-K lag?")
            if s is None:
                st.caption(f"Needs graded calls on both sides at {horizon} days.")
            else:
                st.metric(f"Barely changed minus changed a lot, {horizon} days", pct(s["spread"]),
                          help="Mean excess return of the bottom fifth by change score minus the top fifth.")
                if s["too_few"]:
                    st.warning("Too few to judge: each side needs at least 20 calls over 6 months. Run the filing "
                               "backtest on more companies.", icon=":material/info:")
                st.caption(f"95% interval {interval(s['lo'], s['hi'])}, {s['n']:,} calls over {s['months']} months. "
                           "The universe is today's S&P 100, so every company in it survived: absolute returns are "
                           "biased upward, and this gap between the two groups is the fairer number.")

    board = track.scoreboard(graded, horizon)
    rows = [{"Signal": LABELS.get((r.kind, r.direction), f"{r.kind} {r.direction:+d}"), "Calls": r.n, "Months": r.months,
             "Hit rate": "n/a" if pd.isna(r.hit_rate) else f"{r.hit_rate:.0%}",
             "Hit rate 95%": "needs 2+ months" if pd.isna(r.hit_lo) else f"{r.hit_lo:.0%} to {r.hit_hi:.0%}",
             "Mean vs market": pct(r.mean), "Mean 95%": interval(r.mean_lo, r.mean_hi), "Median": pct(r.median),
             "Median 95%": interval(r.median_lo, r.median_hi),
             "Verdict": "Too few to judge" if r.too_few else ""} for r in board.itertuples()]
    with ui.panel("board"):
        st.subheader(f"{horizon}-day results, {mode} calls")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        pending = int((graded[f"status_{horizon}"] == "pending").sum())
        st.caption(f"Each call is graded against its own market: S&P 500 (US), Nifty 50 (India), TOPIX (Japan). A hit means the stock beat or lagged it the way the signal said. Intervals resample whole months, "
                   f"because calls in the same month share one market. {pending:,} calls are still inside their "
                   f"{horizon}-day window. {int(board['dropped'].sum()):,} calls had no price data (often delisted "
                   "companies) and are left out, which flatters results a little.")

    kinds = sorted(graded["kind"].unique())
    kind = st.selectbox("Path after the call", kinds, format_func=lambda k: {"debate": "Debates", "insider_cluster":
                        "Insider clusters", "filing_change": "Filing changes"}.get(k, k))
    c = curve(mode, kind, date.today())
    if c.empty:
        st.caption("No calls with prices to plot yet.")
    else:
        data = c.reset_index().assign(mean=lambda d: d["mean"] * 100, lo=lambda d: d["lo"] * 100, hi=lambda d: d["hi"] * 100)
        band = alt.Chart(data).mark_area(opacity=.18, color=ui.LINE).encode(
            x=alt.X("day:Q", title="Trading days after the call"), y=alt.Y("lo:Q", title="Excess return vs its market (%)"), y2="hi:Q")
        line = alt.Chart(data).mark_line(color=ui.LINE, strokeWidth=2).encode(x="day:Q", y="mean:Q")
        zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=ui.MUTED, strokeDash=[3, 3]).encode(y="y:Q")
        st.altair_chart((band + line + zero).properties(height=260), width="stretch")
        st.caption("Average path, signed so that up means the signal was right; the band is the 95% interval.")

    with st.expander(f"All {len(graded):,} {mode} calls"):
        view = graded.assign(source=[track.source_url(row) for row in graded.to_dict("records")])
        st.dataframe(view.sort_values("public_date", ascending=False), hide_index=True, width="stretch",
                     column_order=["public_date", "kind", "ticker", "direction", "strength", f"excess_{horizon}",
                                   f"status_{horizon}", "source"],
                     column_config={f"excess_{horizon}": st.column_config.NumberColumn("excess vs market", format="percent"),
                                    "source": st.column_config.LinkColumn(display_text="Open")})

with st.expander("Run backtests"):
    st.caption("The insider backtest downloads the SEC's quarterly insider data (about 10 MB a quarter, cached). "
               "The filing backtest reads six years of 10-Ks per company, so allow 5 to 15 minutes for 100 companies "
               "the first time; scores are cached after that.")
    years = st.slider("Insider history", 1, 20, 5, format="%d years")
    if st.button("Run insider backtest", type="primary"):
        bar = st.progress(0.0)
        result = track.backtest_insider(years, progress=lambda i, total: bar.progress(i / total, f"Quarter {i} of {total}"))
        st.cache_data.clear()
        st.success(f"Recorded {result['recorded']:,} new insider cluster calls.")
        if result["skipped"]:
            st.warning(f"The SEC didn't serve {', '.join(result['skipped'])}: not published yet, or the request was refused "
                       "or timed out. Run again later to fill them in; quarters already downloaded are kept.")
    tickers = st.text_area("Filing backtest universe (today's S&P 100 by default)", "\n".join(track.universe()), height=120)
    if st.button("Run filing backtest", type="primary"):
        names = [t.strip().upper() for t in tickers.replace(",", "\n").split() if t.strip()]
        bar = st.progress(0.0)
        n = track.backtest_filings(names, progress=lambda i, total: bar.progress(i / total, f"{names[i - 1]}: {i} of {total}"))
        st.cache_data.clear()
        st.success(f"Recorded {n:,} new filing-change calls.")
