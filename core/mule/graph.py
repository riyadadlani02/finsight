"""Graph view of transactions: per-account behaviour features, an unsupervised
risk score, and ring extraction (groups of suspicious accounts that move money
between each other)."""
from __future__ import annotations

import networkx as nx
import numpy as np
import pandas as pd

RAPID_HOURS = 24  # an outgoing payment this soon after an incoming one counts as "rapid forwarding"

FEATURES = ["in_cnt", "out_cnt", "in_deg", "out_deg", "in_amt", "out_amt", "avg_in", "avg_out",
            "passthrough", "rapid_frac", "median_dwell_h", "pagerank"]


def account_features(tx: pd.DataFrame) -> pd.DataFrame:
    """One row per account, indexed by account id."""
    accounts = pd.Index(pd.unique(tx[["sender", "receiver"]].values.ravel()), name="account")
    f = pd.DataFrame(index=accounts)
    out_g, in_g = tx.groupby("sender"), tx.groupby("receiver")
    f["in_cnt"], f["out_cnt"] = in_g.size(), out_g.size()
    f["in_deg"], f["out_deg"] = in_g["sender"].nunique(), out_g["receiver"].nunique()
    f["in_amt"], f["out_amt"] = in_g["amount"].sum(), out_g["amount"].sum()
    f = f.fillna(0.0)
    f["avg_in"] = f["in_amt"] / f["in_cnt"].clip(lower=1)
    f["avg_out"] = f["out_amt"] / f["out_cnt"].clip(lower=1)
    # 1.0 when everything that comes in goes back out; 0 for pure sources/sinks
    f["passthrough"] = np.minimum(f["in_amt"], f["out_amt"]) / np.maximum(f["in_amt"], f["out_amt"]).clip(lower=1e-9)

    # Dwell time: for each outgoing payment, how long since the latest incoming one?
    outgoing = tx[["sender", "timestamp", "amount"]].rename(columns={"sender": "account"}).sort_values("timestamp")
    incoming = (tx[["receiver", "timestamp"]].rename(columns={"receiver": "account", "timestamp": "in_time"})
                .sort_values("in_time"))
    merged = pd.merge_asof(outgoing, incoming, left_on="timestamp", right_on="in_time", by="account",
                           allow_exact_matches=False)
    merged["dwell_h"] = (merged["timestamp"] - merged["in_time"]).dt.total_seconds() / 3600
    merged["rapid_amt"] = merged["amount"].where(merged["dwell_h"] <= RAPID_HOURS, 0.0)
    by_acct = merged.groupby("account")
    # share of money received that is forwarded within RAPID_HOURS
    f["rapid_frac"] = (by_acct["rapid_amt"].sum() / f["in_amt"].clip(lower=1e-9)).reindex(accounts).fillna(0).clip(upper=1)
    f["median_dwell_h"] = by_acct["dwell_h"].median().reindex(accounts).fillna(24 * 365)

    f["pagerank"] = pd.Series(nx.pagerank(build_graph(tx), weight="amount", max_iter=200)).reindex(accounts).fillna(0)
    return f[FEATURES]


def build_graph(tx: pd.DataFrame) -> nx.DiGraph:
    """Directed account graph; parallel transfers are merged into one weighted edge."""
    edges = tx.groupby(["sender", "receiver"], as_index=False).agg(amount=("amount", "sum"), count=("amount", "size"))
    return nx.from_pandas_edgelist(edges, "sender", "receiver", ["amount", "count"], create_using=nx.DiGraph)


def heuristic_score(features: pd.DataFrame) -> pd.Series:
    """Label-free 0-1 risk score: money passes straight through, fast, in size.

    Used when there are no labels to train on, and as a sanity check on the GNN.
    """
    volume_rank = features["in_amt"].rank(pct=True)
    multi_source = np.clip(features["in_deg"] / 3, 0, 1)  # funded by several different payers
    score = features["rapid_frac"] * features["passthrough"] * volume_rank * (0.5 + 0.5 * multi_source)
    return score.rename("heuristic")


def find_rings(graph: nx.DiGraph, risk: pd.Series, threshold: float, min_size: int = 3) -> pd.DataFrame:
    """Group flagged accounts that transact with each other into rings.

    Returns one row per flagged account in a ring: account, ring, role.
    Connected groups larger than 25 are split with Louvain community detection.
    """
    flagged = [a for a in risk.index[risk >= threshold] if a in graph]
    sub = graph.subgraph(flagged)
    groups: list[set] = []
    for comp in nx.weakly_connected_components(sub):
        if len(comp) > 25:
            groups += nx.community.louvain_communities(sub.subgraph(comp).to_undirected(), weight="amount", seed=0)
        else:
            groups.append(comp)
    rows = []
    groups = sorted((g for g in groups if len(g) >= min_size), key=len, reverse=True)
    for ring_id, members in enumerate(groups):
        ring = sub.subgraph(members)
        for a in members:
            inside_in = ring.in_degree(a, weight="amount")
            inside_out = ring.out_degree(a, weight="amount")
            # the account where ring money piles up and stops is the likely cash-out point
            role = "cash-out" if inside_in > 0 and inside_out < 0.2 * inside_in else "mule"
            rows.append({"account": a, "ring": ring_id, "role": role})
    return pd.DataFrame(rows, columns=["account", "ring", "role"])


def ring_summary(graph: nx.DiGraph, rings: pd.DataFrame, risk: pd.Series) -> pd.DataFrame:
    """One row per ring: size, money in from outside, internal flow, cycles."""
    out = []
    for ring_id, grp in rings.groupby("ring"):
        members = set(grp["account"])
        ring = graph.subgraph(members)
        inflow = sum(d["amount"] for u, v, d in graph.in_edges(members, data=True) if u not in members)
        feeders = {u for u, v in graph.in_edges(members) if u not in members}
        out.append({
            "ring": ring_id, "accounts": len(members), "cash_out": int((grp["role"] == "cash-out").sum()),
            "outside_payers": len(feeders), "money_in": round(inflow, 2),
            "internal_flow": round(ring.size(weight="amount"), 2),
            "has_cycle": not nx.is_directed_acyclic_graph(ring),
            "avg_risk": round(float(risk.reindex(list(members)).mean()), 3),
        })
    return pd.DataFrame(out)


def ring_subgraph(graph: nx.DiGraph, rings: pd.DataFrame, ring_id: int, max_payers: int = 60) -> nx.DiGraph:
    """The ring plus the outside accounts paying into it, annotated with a `role` per node for drawing."""
    grp = rings[rings["ring"] == ring_id]
    roles = dict(zip(grp["account"], grp["role"]))
    payers = sorted(((d["amount"], u) for u, v, d in graph.in_edges(roles, data=True) if u not in roles), reverse=True)
    keep = set(roles) | {u for _, u in payers[:max_payers]}
    view = graph.subgraph(keep).copy()
    view.remove_edges_from([(u, v) for u, v in view.edges if u not in roles and v not in roles])
    nx.set_node_attributes(view, {n: roles.get(n, "payer") for n in view}, "role")
    return view
