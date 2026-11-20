from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st
from pyvis.network import Network
from sklearn.metrics import precision_recall_fscore_support

from core import ui
from core.mule import data, gnn, graph, neo4j_store

ui.header("Mule ring detector", "Transfers become a graph. A GraphSAGE model scores every account, then flagged accounts that move money between each other are grouped into rings.")

# Only CSVs already in data/ can be read from disk: no free-form paths or URLs.
DATA = Path(__file__).resolve().parent.parent / "data"

COLORS = {"mule": ui.BEAR, "cash-out": "#5D4BA8", "payer": "#A9B4B0"}

# -------------------------------------------------------------------- data
with st.sidebar:
    st.header("Data")
    source = st.radio("Source", ["Synthetic (planted rings)", "CSV file"])
    if source.startswith("Synthetic"):
        n_accounts = st.slider("Accounts", 1000, 10000, 3000, 500)
        n_rings = st.slider("Planted rings", 2, 20, 8)
        seed = st.number_input("Seed", 0, 9999, 7)
    else:
        upload = st.file_uploader("Transactions CSV", type="csv")
        path = st.selectbox("...or a CSV in the data/ folder", ["", *sorted(f.name for f in DATA.glob("*.csv"))],
                            help="Put large files in data/ and pick them here.")
        max_rows = st.number_input("Max rows to read", 10_000, 5_000_000, 500_000, step=50_000)
        st.caption("Needs sender, receiver, amount. Optional timestamp and fraud label. "
                   "PaySim and IBM AML column names are recognised.")
    run = st.button("Analyse", type="primary")


def train_on_synthetic():
    tx_s, _ = data.generate()
    feats_s = graph.account_features(tx_s)
    return gnn.train(gnn.to_data(feats_s, tx_s, data.account_labels(tx_s)))[0]


if run:
    try:
        if source.startswith("Synthetic"):
            tx, _ = data.generate(n_accounts, n_rings, seed=int(seed))
        else:
            if not (upload or path):
                st.error("Upload a CSV or pick one from data/.")
                st.stop()
            tx = data.load_csv(upload or DATA / path, int(max_rows))
    except Exception as exc:  # noqa: BLE001 - bad file/columns shown to the user
        st.error(f"Could not load the data: {exc}")
        st.stop()

    with st.status("Analysing...", expanded=True) as status:
        st.write(f"{len(tx):,} transfers. Computing account features...")
        feats = graph.account_features(tx)
        labels = data.account_labels(tx)
        result = {"tx": tx, "features": feats, "labels": labels, "graph": graph.build_graph(tx),
                  "heuristic": graph.heuristic_score(feats), "metrics": None}
        if labels is not None and labels.sum() >= 20:
            st.write(f"Labels found ({int(labels.sum())} mule accounts). Training the GNN on this data...")
            pyg = gnn.to_data(feats, tx, labels)
            model, result["metrics"] = gnn.train(pyg)
            result["mode"] = "GNN trained on this dataset (metrics are on a held-out 20% of accounts)"
        else:
            st.write("No usable labels. Training the GNN on synthetic rings and applying it here...")
            pyg = gnn.to_data(feats, tx)
            model = train_on_synthetic()
            result["mode"] = ("GNN trained on synthetic rings and applied to this data. It has never seen labels "
                              "for this dataset, so treat scores as leads and compare with the heuristic column.")
        result["risk"] = pd.Series(gnn.predict(model, pyg), index=feats.index, name="risk")
        status.update(label="Analysis complete", state="complete", expanded=False)
    st.session_state["mule"] = result

if "mule" not in st.session_state:
    st.info("Pick a data source in the sidebar and press **Analyse**.")
    st.stop()

r = st.session_state["mule"]
tx, feats, risk, G, labels = r["tx"], r["features"], r["risk"], r["graph"], r["labels"]

# ----------------------------------------------------------------- results
st.info(r["mode"])
threshold = st.slider("Risk threshold for flagging an account", 0.05, 0.99, 0.5, 0.05)
rings = graph.find_rings(G, risk, threshold)
flagged = risk >= threshold

cols = st.columns(6)
cols[0].metric("Accounts", f"{len(feats):,}")
cols[1].metric("Transfers", f"{len(tx):,}")
cols[2].metric("Flagged accounts", int(flagged.sum()))
cols[3].metric("Rings found", rings["ring"].nunique())
if labels is not None:
    p, rec, _, _ = precision_recall_fscore_support(labels.reindex(feats.index), flagged, average="binary", zero_division=0)
    note = "All accounts at this threshold, including ones the model trained on."
    cols[4].metric("Precision", f"{p:.0%}", help=note)
    cols[5].metric("Recall", f"{rec:.0%}", help=note)
if r["metrics"]:
    m = r["metrics"]
    st.caption(f"Held-out test ({m['test_accounts']} accounts, {m['test_mules']} mules, threshold 0.5): "
               f"precision {m['precision']:.0%}, recall {m['recall']:.0%}, average precision {m['avg_precision']:.2f}")

tab_rings, tab_accounts, tab_neo = st.tabs(["Rings", "Accounts", "Neo4j"])

with tab_rings:
    if rings.empty:
        st.warning("No rings at this threshold. Lower it to widen the net.")
    else:
        summary = graph.ring_summary(G, rings, risk)
        st.dataframe(summary, hide_index=True, width="stretch",
                     column_config={"money_in": st.column_config.NumberColumn(format="%.0f"),
                                    "internal_flow": st.column_config.NumberColumn(format="%.0f")})
        ring_id = st.selectbox("Inspect ring", summary["ring"])
        view = graph.ring_subgraph(G, rings, ring_id)
        net = Network(height="560px", width="100%", directed=True, cdn_resources="in_line",
                      bgcolor="#1F2C34", font_color=ui.INK)
        peak = max(d["amount"] for _, _, d in view.edges(data=True))
        for node, attrs in view.nodes(data=True):
            role, name = attrs["role"], escape(str(node))  # ids may come from an uploaded file
            net.add_node(name, label=name if role != "payer" else " ", color=COLORS[role],
                         size=10 if role == "payer" else 22, title=f"{name} · {role} · risk {risk.get(node, 0):.2f}")
        for u, v, d in view.edges(data=True):
            net.add_edge(escape(str(u)), escape(str(v)), width=1 + 6 * d["amount"] / peak, color="rgba(238,243,246,.35)",
                         title=f"{d['amount']:,.0f} in {d['count']} transfers")
        # pyvis wraps the graph in a white bootstrap card; match the stage instead.
        graph_html = net.generate_html().replace(
            "</head>", "<style>body{margin:0;background:#1F2C34}.card{border:0;background:transparent}</style></head>", 1)
        if hasattr(st, "iframe"):
            st.iframe(graph_html, height=580)
        else:  # older Streamlit
            st.components.v1.html(graph_html, height=580)
        st.caption("Red: mule. Violet: likely cash-out. Grey: outside payers (possible victims). "
                   "Arrow width = amount. Hover for details.")
        members = rings[rings["ring"] == ring_id].set_index("account")
        st.dataframe(members.join(risk).join(feats[["in_amt", "out_amt", "in_deg", "out_deg", "median_dwell_h"]])
                     .sort_values("risk", ascending=False), width="stretch")

with tab_accounts:
    table = feats.join(risk).join(r["heuristic"]).join(rings.set_index("account")[["ring", "role"]])
    if labels is not None:
        table = table.join(labels.rename("labelled_mule"))
    st.dataframe(table.sort_values("risk", ascending=False).head(500), width="stretch")
    st.download_button("Download all scores (CSV)", table.sort_values("risk", ascending=False).to_csv(), "mule_scores.csv")

with tab_neo:
    ok, msg = neo4j_store.check()
    if not ok:
        st.warning(f"Neo4j is not reachable: {msg}\n\nStart it with `docker compose up -d neo4j`.")
    else:
        st.write(f"Graph to load: {G.number_of_nodes():,} accounts, {G.number_of_edges():,} relationships, "
                 f"with risk, ring and role on each account. This replaces what is in Neo4j now.")
        if st.button("Load into Neo4j"):
            with st.spinner("Writing..."):
                n, e = neo4j_store.push(G, risk, rings)
            st.success(f"Wrote {n:,} accounts and {e:,} relationships. Browse at http://localhost:7474")
        preset = st.selectbox("Example queries", list(neo4j_store.PRESETS))
        cypher = st.text_area("Cypher (read-only)", neo4j_store.PRESETS[preset], height=140)
        if st.button("Run query"):
            try:
                st.dataframe(neo4j_store.query(cypher), width="stretch")
            except Exception as exc:  # noqa: BLE001 - Cypher errors shown to the user
                st.error(str(exc))
