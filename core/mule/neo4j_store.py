"""Push the scored transaction graph into Neo4j so rings can be explored with Cypher."""
from __future__ import annotations

import networkx as nx
import pandas as pd
from neo4j import GraphDatabase

from .. import config

BATCH = 5000

PRESETS = {
    "Rings and their size": "MATCH (a:Account) WHERE a.ring IS NOT NULL\nRETURN a.ring AS ring, count(*) AS accounts, round(avg(a.risk), 3) AS avg_risk\nORDER BY accounts DESC",
    "Money loops (cycles) through flagged accounts": "MATCH p = (a:Account)-[:SENT*2..4]->(a)\nWHERE a.ring IS NOT NULL\nRETURN [n IN nodes(p) | n.id] AS loop, reduce(t = 0.0, r IN relationships(p) | t + r.amount) AS total\nORDER BY total DESC LIMIT 25",
    "Cash-out accounts and who feeds them": "MATCH (m:Account)-[s:SENT]->(c:Account {role: 'cash-out'})\nRETURN c.id AS cash_out, c.ring AS ring, count(m) AS feeders, round(sum(s.amount), 2) AS received\nORDER BY received DESC",
    "Victims paying more than one ring member": "MATCH (v:Account)-[s:SENT]->(m:Account)\nWHERE m.ring IS NOT NULL AND v.ring IS NULL\nWITH v, count(DISTINCT m) AS mules, sum(s.amount) AS lost WHERE mules > 1\nRETURN v.id AS payer, mules, round(lost, 2) AS lost ORDER BY lost DESC LIMIT 50",
    "High-risk accounts not in any ring": "MATCH (a:Account) WHERE a.ring IS NULL\nRETURN a.id AS account, a.risk AS risk ORDER BY risk DESC LIMIT 25",
}


def _driver():
    uri, user, password = config.neo4j_settings()
    return GraphDatabase.driver(uri, auth=(user, password), connection_timeout=5)


def check() -> tuple[bool, str]:
    try:
        with _driver() as driver:
            driver.verify_connectivity()
        return True, "connected"
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        return False, str(exc).splitlines()[0]


def push(graph: nx.DiGraph, risk: pd.Series, rings: pd.DataFrame) -> tuple[int, int]:
    """Replace the graph in Neo4j with this one. Returns (nodes, relationships) written."""
    ring_of = dict(zip(rings["account"], rings["ring"]))
    role_of = dict(zip(rings["account"], rings["role"]))
    nodes = [{"id": str(n), "risk": float(risk.get(n, 0.0)),
              "ring": int(ring_of[n]) if n in ring_of else None, "role": role_of.get(n)} for n in graph.nodes]
    edges = [{"s": str(u), "t": str(v), "amount": float(d["amount"]), "count": int(d["count"])}
             for u, v, d in graph.edges(data=True)]
    with _driver() as driver, driver.session() as session:
        session.run("CREATE CONSTRAINT account_id IF NOT EXISTS FOR (a:Account) REQUIRE a.id IS UNIQUE").consume()
        session.run("MATCH (a:Account) CALL { WITH a DETACH DELETE a } IN TRANSACTIONS OF 10000 ROWS").consume()
        for i in range(0, len(nodes), BATCH):
            session.run("UNWIND $rows AS r CREATE (a:Account {id: r.id, risk: r.risk, ring: r.ring, role: r.role})",
                        rows=nodes[i:i + BATCH]).consume()
        for i in range(0, len(edges), BATCH):
            session.run("UNWIND $rows AS r MATCH (s:Account {id: r.s}), (t:Account {id: r.t}) "
                        "CREATE (s)-[:SENT {amount: r.amount, count: r.count}]->(t)", rows=edges[i:i + BATCH]).consume()
    return len(nodes), len(edges)


def query(cypher: str, limit: int = 1000) -> pd.DataFrame:
    """Run a read-only Cypher query and return the rows."""
    with _driver() as driver, driver.session(default_access_mode="READ") as session:
        return pd.DataFrame([r.data() for r in session.run(cypher).fetch(limit)])
