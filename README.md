# FinSight

Three research tools for public companies and payment fraud, in one Streamlit app.

![Home page](docs/screenshots/home.jpg)

| Tool | What it does | Built on |
|---|---|---|
| **Bull vs Bear** | Two Claude agents argue for and against a stock using only its latest 10-K and 10-Q. A third judges who argued from the evidence and lists every claim the filings don't support. | Claude API, SEC EDGAR, XBRL, yfinance |
| **Insider radar** | Reads SEC Form 4 filings and flags companies where several insiders bought their own stock on the open market in the same window. | SEC EDGAR, pandas |
| **Mule ring detector** | Turns transfers into a graph, scores every account with a GraphSAGE network, and groups flagged accounts into the rings that move money between each other. | NetworkX, PyTorch Geometric, Neo4j |

The tools are linked: pick a cluster in the radar and send that ticker straight into a debate.

## Bull vs Bear

Each side gets the same dossier: Risk Factors and MD&A from the latest annual and quarterly report, four years of XBRL financials, and Yahoo price and valuation data. Every claim must carry a source tag such as `[10-K MD&A]`, and the judge scores support from the filings, not which conclusion it prefers.

![Debate turns](docs/screenshots/debate.jpg)

![Verdict](docs/screenshots/verdict.jpg)

One debate is five API calls; the dossier is prompt-cached across them. Debates are saved, so you can reopen past ones.

## Mule ring detector

Synthetic mode plants rings (victims to mules to cash-out, with splits and cycles) among normal traffic that includes look-alikes such as small businesses paying suppliers the same day. On the default synthetic set (3,000 accounts, 96k transfers, 8 planted rings) it recovers all 8 rings, with 100% recall and 73% precision on held-out accounts.

![Mule ring graph](docs/screenshots/mule-ring.jpg)

CSV mode needs `sender, receiver, amount`, plus an optional timestamp and fraud label; PaySim and IBM AML column names are recognised. For big files, put them in `data/` and use the path box.

- With at least 20 labelled mule accounts, the GNN trains on your data and reports held-out metrics.
- Without labels, the GNN trains on synthetic rings and is applied to your data. Treat those scores as leads and compare them with the `heuristic` column.

The Neo4j tab loads the scored graph and has example Cypher queries (money loops, cash-out feeders).

## Insider radar

Only transaction code `P` (open-market purchase) counts. Grants, option exercises and tax withholding are ignored, and 10b5-1 pre-scheduled trades are excluded by default. "Scan watchlist" takes seconds. "Scan whole market" fetches every Form 4 for the chosen days while staying under the SEC's 10 requests per second, so allow a few minutes per trading day; filings already fetched are skipped. The score (0 to 100) weighs how many insiders bought, how much, how senior they are, and how recently.

For alerts without opening the app, set `ALERT_WEBHOOK_URL` (a Slack incoming webhook works) and schedule:

```bash
python -m core.insider scan --days 2
```

## Run it

Needs Python 3.11 or newer.

```bash
git clone https://github.com/riyadadlani02/finsight.git && cd finsight
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

In `.env`, set `ANTHROPIC_API_KEY` and `SEC_USER_AGENT` (`"Your Name you@example.com"`; the SEC blocks requests without one, and no SEC key is needed). Then:

```bash
streamlit run app.py
```

Data is stored in a local SQLite file (`data/finsight.db`) unless you set `DATABASE_URL`. The home page shows which pieces are ready.

**With Docker** (Postgres and Neo4j included):

```bash
cp .env.example .env    # fill in the two values above
docker compose up --build
```

The app is on http://localhost:8501, and Neo4j Browser is on http://localhost:7474 (user `neo4j`, password `finsight-neo4j`).

## Tests

```bash
pytest                      # offline: no network, no API key, in-memory SQLite
TEST_DATABASE_URL=postgresql+psycopg://finsight:finsight@localhost:5432/finsight pytest
```

## Layout

```
app.py                  home page and navigation
views/                  the three tool pages
core/edgar.py           SEC client (filings, XBRL facts, Form 4), shared by the debate and the radar
core/debate.py          dossier and Claude agents
core/insider.py         ingestion, cluster detection, alerts, CLI
core/mule/              synthetic and CSV data, graph features and rings, GNN, Neo4j
core/db.py              database schema (SQLite or PostgreSQL)
core/ui.py              shared styling
```

Research tooling only. Nothing here is investment advice.
