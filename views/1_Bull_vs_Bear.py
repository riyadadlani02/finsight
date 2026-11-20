import json

import pandas as pd
import streamlit as st

from core import cockpit, config, db, debate, ui

ui.header("Past debates", "Bull vs Bear debates run before Said vs did replaced them. Kept for reference; "
          "their verdicts stay in the track record.")

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
        ui.point("Claims the filings don't support", v["unsupported_claims"], ui.AMBER)
    ui.point("What would change the verdict", v["what_would_change_the_verdict"])



if db.check()[0]:
    past = debate.history()
    if not past:
        st.caption("No debates were run before Said vs did replaced them.")
    for row in past:
        label = (f"**{row['ticker']}**, {row['created_at']:%d %b %Y %H:%M}: {row['winner']} "
                 f"({row['bull_score']} to {row['bear_score']})")
        with st.expander(label, expanded=str(row["id"]) == st.query_params.get("debate")):
            for j, turn in enumerate(json.loads(row["transcript_json"])):
                show_turn(turn, f"h{row['id']}-{j}")
            show_verdict(json.loads(row["verdict_json"]))
else:
    st.caption("History needs the database (`docker compose up -d postgres`).")
