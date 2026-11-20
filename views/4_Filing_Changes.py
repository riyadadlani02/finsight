from html import escape

import pandas as pd
import streamlit as st

from core import changes, config, edgar, ui

ui.header("Filing changes", "What a company changed in its annual report since last year. The scores come from "
          "comparing the text itself; Claude only explains what the comparison found.")

KIND_COLOR = {"new risk": ui.BEAR, "strengthened": ui.BEAR, "dropped risk": ui.BULL, "softened": ui.BULL,
              "other": ui.MUTED}


def paragraphs(paras: list[changes.Para], redline: bool = False) -> None:
    rows = []
    for p in paras:
        body = changes.redline_html(p.old, p.new) if redline else escape(p.new or p.old or "")
        rows.append(f'<p class="fs-para"><span class="fs-pid">{p.id}</span>{body}</p>')
    st.html("".join(rows))


def company_tab() -> None:
    current = st.query_params.get("ticker", "AAPL")
    ticker = st.text_input("Ticker", value=current, max_chars=10).strip().upper()
    if not ticker:
        return
    if ticker != current:
        st.query_params["ticker"] = ticker
    try:
        with st.spinner(f"Reading {ticker}'s 10-K filings from the SEC..."):
            years = changes.score_company(ticker)
            latest = changes.latest(ticker)
    except edgar.EdgarError as exc:
        st.info(str(exc))
        return
    if not years or not latest["diffs"]:
        st.info(f"Couldn't find Risk Factors or MD&A in both of {ticker}'s last two 10-Ks, so there's nothing to compare.")
        return

    new, old, now = latest["new"], latest["old"], years[-1]
    prev = years[-2] if len(years) > 1 else None
    with ui.panel("score"):
        st.markdown(f"**{latest['name']}**: [10-K filed {new.filed}]({new.url}) compared with "
                    f"[10-K filed {old.filed}]({old.url})")
        cols = st.columns(1 + len(now.scores))
        cols[0].metric("Change score", f"{now.combined} / 100",
                       delta=None if prev is None else now.combined - prev.combined, delta_color="off",
                       help="100 minus the text similarity of the two filings, averaged over the sections found.")
        for col, (name, score) in zip(cols[1:], now.scores.items()):
            col.metric(name, score, delta=None if prev is None or name not in prev.scores else score - prev.scores[name],
                       delta_color="off")
        history = pd.DataFrame([{"10-K filed": y.filed[:4], "Change score": y.combined} for y in years])
        st.bar_chart(history, x="10-K filed", y="Change score", height=160, color=ui.LINE, x_label="", y_label="")
        st.caption("Change score of each 10-K against the one before, by year filed.")

    key = f"explain-{new.accession}"
    if config.anthropic_key():
        if st.button("Explain the changes", type="primary"):
            with st.spinner("Claude is reading the changed paragraphs..."):
                try:
                    st.session_state[key] = changes.explain(latest["name"], ticker, latest["diffs"])
                except Exception as exc:  # noqa: BLE001 - API errors shown to the user
                    st.error(f"Claude API error: {exc}")
    else:
        st.caption("Add ANTHROPIC_API_KEY to .env to have Claude explain the changes.")
    if st.session_state.get(key) is not None:
        found = st.session_state[key]
        st.subheader("What changed, explained")
        if not found:
            st.caption("Claude found no material changes among the edited paragraphs.")
        for item in found:
            ui.point(f"{item['section']} {item['id']}: {item['kind']}", item["summary"], KIND_COLOR[item["kind"]])

    for tab, (name, diff) in zip(st.tabs(list(latest["diffs"])), latest["diffs"].items()):
        with tab:
            groups = {k: [p for p in diff.paras if p.kind == k] for k in ("added", "removed", "reworded", "unchanged")}
            for kind, label in (("added", "Added"), ("removed", "Removed"), ("reworded", "Reworded"), ("unchanged", "Unchanged")):
                if groups[kind]:
                    with st.expander(f"{label} ({len(groups[kind])})", expanded=kind == "added"):
                        paragraphs(groups[kind], redline=kind == "reworded")
    st.page_link("views/7_Said_vs_Did.py", label=f"Check what {ticker}'s management promised", query_params={"ticker": ticker})


def screen_tab() -> None:
    days = st.slider("Filed in the last", 1, 30, 7, format="%d days")
    if st.button("Scan", type="primary"):
        bar = st.progress(0.0, "Reading the SEC daily index...")
        st.session_state["screen"] = changes.screen(days, progress=lambda i, n: bar.progress(i / n, f"{i}/{n} companies"))
        bar.empty()
    result = st.session_state.get("screen")
    if result is None:
        st.caption("Scan ranks every company that filed a 10-K in that window by how much it changed. "
                   "The first scan reads two 10-Ks per company, so allow a few seconds each.")
    elif result.empty:
        st.info("No 10-K filings with comparable sections in that window. Try a longer one.")
    else:
        st.dataframe(result.assign(open=[f"/Filing_Changes?ticker={t}" for t in result["ticker"]]), hide_index=True,
                     width="stretch", column_order=["open", "ticker", "name", "filed", "combined", "risk_factors", "mdna"],
                     column_config={"open": st.column_config.LinkColumn("", display_text="Open"),
                                    "combined": st.column_config.ProgressColumn("change score", min_value=0, max_value=100, format="%d"),
                                    "risk_factors": "Risk Factors", "mdna": "MD&A"})


company, recent = st.tabs(["Company", "Recently filed"])
with company:
    company_tab()
with recent:
    screen_tab()
