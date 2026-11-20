import json

import pandas as pd
import streamlit as st

from core import config, db, debate, ui

ui.header("Bull vs Bear", "Two Claude agents argue a stock from its SEC filings, then a judge scores who "
          "argued from the evidence. " + debate.DISCLAIMER)

if "debate_ticker" in st.session_state:  # handed over from the insider radar
    st.session_state["ticker_input"] = st.session_state.pop("debate_ticker")
st.session_state.setdefault("ticker_input", "AAPL")

with st.form("run", border=False):
    left, mid, right = st.columns([2, 2, 1], vertical_alignment="bottom")
    ticker = left.text_input("Ticker (US-listed, files with the SEC)", key="ticker_input").strip().upper()
    depth = mid.select_slider("How much filing text the agents read", ["Short", "Standard", "Deep"], "Standard",
                              help="Risk Factors and MD&A per filing. Deep costs more tokens.")
    go = right.form_submit_button("Start debate", type="primary", width="stretch")


def show_dossier(d: dict) -> None:
    with st.expander(f"What the agents were given ({len(d['text']):,} characters)"):
        for f in d["sources"]:
            st.markdown(f"- [{f.form} filed {f.filed}]({f.url}), period ending {f.report_date}")
        if d["market"]:
            cols = st.columns(5)
            for i, (k, v) in enumerate(d["market"].items()):
                cols[i % 5].metric(k, v)
        if d["history"] is not None:
            st.line_chart(d["history"], height=180, color="#2340C4")
        if d["facts"]:
            table = pd.DataFrame(d["facts"]).T
            table.columns = [f"FY {c[:4]}" for c in table.columns]
            st.dataframe(table.style.format("{:,.0f}", na_rep="-"), width="stretch")


def show_turn(turn: dict, key: str, stream=None) -> str:
    color = ui.BULL if turn["speaker"] == "bull" else ui.BEAR
    with st.container(key=f"turn-{turn['speaker']}-{key}"):
        st.html(f'<div class="fs-speaker" style="color:{color}">{turn["speaker"].title()}, {turn["round"]}</div>')
        return st.write_stream(stream) if stream else st.markdown(turn["text"])


def show_verdict(v: dict) -> None:
    ui.verdict_bar(v["winner"], v["bull_score"], v["bear_score"])
    st.write(v["summary"])
    a, b = st.columns(2, gap="large")
    with a:
        ui.point("Strongest bull point", v["strongest_bull_point"], ui.BULL)
    with b:
        ui.point("Strongest bear point", v["strongest_bear_point"], ui.BEAR)
    if v.get("unsupported_claims"):
        ui.point("Claims the filings don't support", v["unsupported_claims"], "#8A6A1F")
    ui.point("What would change the verdict", v["what_would_change_the_verdict"])


if go and ticker:
    if not config.anthropic_key():
        st.error("ANTHROPIC_API_KEY is not set. Add it to .env and restart.")
        st.stop()
    try:
        with st.spinner(f"Reading {ticker}'s SEC filings..."):
            chars = {"Short": 12000, "Standard": 30000, "Deep": 60000}[depth]
            dossier = debate.build_dossier(ticker, chars)
    except Exception as exc:  # noqa: BLE001 - EDGAR/network problems shown to the user
        st.error(f"Couldn't build the dossier: {exc}")
        st.stop()

    st.header(f"{dossier['name']} ({dossier['ticker']})")
    show_dossier(dossier)
    transcript: list[dict] = []
    try:
        for i, (speaker, round_name, instruction) in enumerate(debate.ROUNDS):
            turn = {"speaker": speaker, "round": round_name}
            turn["text"] = show_turn(turn, str(i), debate.stream_turn(dossier, transcript, speaker, instruction))
            transcript.append(turn)
        with st.spinner("The judge is scoring both sides..."):
            verdict = debate.judge(dossier, transcript)
    except Exception as exc:  # noqa: BLE001 - API errors shown to the user
        st.error(f"Claude API error: {exc}")
        st.stop()
    show_verdict(verdict)
    try:
        debate.save(dossier, transcript, verdict)
    except Exception as exc:  # noqa: BLE001
        st.caption(f"Not saved to history (database unavailable: {str(exc).splitlines()[0]})")

st.divider()
st.subheader("Past debates")
if db.check()[0]:
    past = debate.history()
    if not past:
        st.caption("Debates you run are saved here.")
    for row in past:
        label = (f"**{row['ticker']}**, {row['created_at']:%d %b %Y %H:%M}: {row['winner']} "
                 f"({row['bull_score']} to {row['bear_score']})")
        with st.expander(label):
            for j, turn in enumerate(json.loads(row["transcript_json"])):
                show_turn(turn, f"h{row['id']}-{j}")
            show_verdict(json.loads(row["verdict_json"]))
else:
    st.caption("History needs the database (`docker compose up -d postgres`).")
