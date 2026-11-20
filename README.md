# FinSight

Research tools for public companies and payment fraud, built so every signal can be checked: the scores come from the SEC's own data, Claude only argues or explains what that data shows, and every call FinSight makes is graded against the market afterwards.

![The cockpit](docs/screenshots/cockpit.jpg)

**Try the cockpit in your browser:** https://riyadadlani02.github.io/finsight/ (a snapshot of real data; the full app runs locally).

The **cockpit** puts one company on stage: its price, the last Bull vs Bear verdict, recent insider buying, how much its latest 10-K changed, and the calls FinSight has made on it. Switch company from the name, and every card opens its tool for the same ticker.

| Tool | What it answers | Built on |
|---|---|---|
| **Filing changes** | What did the company change in its annual report since last year? New, dropped and reworded paragraphs, a change score anyone can reproduce, and a screen of every recent 10-K ranked by change. | SEC EDGAR, TF-IDF, Claude |
| **Track record** | Did FinSight's signals actually work? Every debate verdict, insider cluster and filing change becomes a dated call, graded against the S&P 500 at 30, 90 and 180 days, live and on years of history. | SEC insider data sets, yfinance |
| **Bull vs Bear** | What's the strongest honest case for and against a stock? Two Claude agents argue from its 10-K and 10-Q; a judge scores who argued from the evidence. | Claude API, SEC EDGAR, XBRL |
| **Insider radar** | Which companies have several insiders buying their own stock at once? | SEC Form 4 |
| **Mule ring detector** | Which accounts move money as a ring? A GraphSAGE network scores every account in a transfer graph. | NetworkX, PyTorch Geometric, Neo4j |

## Filing changes

![Filing changes](docs/screenshots/filing-changes.jpg)

FinSight compares each 10-K's Risk Factors and MD&A with the year before. The change score is 100 minus the TF-IDF similarity of the two sections. Paragraphs are matched one to one: at least 0.9 similar is unchanged, 0.5 to 0.9 reworded (shown as a word-level redline), and anything else added or removed. **Explain the changes** sends only the changed paragraphs to Claude, which must cite their ids; any explanation citing a paragraph the diff didn't produce is dropped.

Why it matters: Cohen, Malloy and Nguyen ("Lazy Prices", Journal of Finance, 2020) found that companies whose annual reports change a lot go on to underperform those whose reports barely change. The track record tests whether that holds here.

## Track record

![Track record](docs/screenshots/track-record.jpg)

- **No hindsight.** A call is dated when its information became public: the 10-K filing date, or the Form 4 filing date of the purchase that completed an insider cluster. Returns run from the first close after that. A live call only counts as live if it was recorded by that first close, and live insider calls use fixed thresholds rather than whatever the sliders say.
- **Honest uncertainty.** Hit rates and returns carry 95% intervals from a bootstrap that resamples whole months, because calls in the same month share one market. Groups with fewer than 20 calls or 6 months say "Too few to judge".
- **Stated biases.** Calls on delisted companies have no Yahoo prices and are counted as dropped. The filing backtest uses today's S&P 100, which are survivors, so its headline is the gap between the least- and most-changed fifth rather than absolute returns.

The insider backtest reads the SEC's quarterly insider data sets (every Form 4 since 2006, about 10 MB a quarter). One year of history gives about 670 cluster calls in under 20 seconds.

## Bull vs Bear

![Bull vs Bear](docs/screenshots/debate.jpg)

Each side gets the same dossier: Risk Factors and MD&A from the latest annual and quarterly report, four years of XBRL financials, and Yahoo price data. Every claim must carry a source tag such as `[10-K MD&A]`, and the judge scores support from the filings, not which conclusion it prefers. One debate is five API calls with the dossier prompt-cached across them, and each verdict is recorded as a call for the track record.

## Insider radar

Only open-market purchases (transaction code `P`) count; grants, option exercises, tax withholding and 10b5-1 pre-scheduled trades are left out. "Scan watchlist" takes seconds, "Scan whole market" reads every Form 4 for the chosen days under the SEC's 10 requests a second. For alerts without opening the app, set `ALERT_WEBHOOK_URL` (a Slack incoming webhook works) and schedule:

```bash
python -m core.insider scan --days 2
```

## Mule ring detector

![Mule ring graph](docs/screenshots/mule-ring.jpg)

Synthetic mode plants rings (victims to mules to cash-out, with splits and cycles) among normal traffic that includes look-alikes such as small businesses paying suppliers the same day. On the default synthetic set (3,000 accounts, 96k transfers, 8 planted rings) it recovers all 8 rings, with 100% recall and 73% precision on held-out accounts. CSV mode needs `sender, receiver, amount`, plus an optional timestamp and fraud label (PaySim and IBM AML column names are recognised); put large files in `data/` and pick them from the dropdown. Without labels, the GNN trains on synthetic rings, so treat its scores as leads. The Neo4j tab loads the scored graph with example Cypher queries.

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

Data is stored in a local SQLite file (`data/finsight.db`) unless you set `DATABASE_URL`; the Setup panel under the cockpit shows what's ready. The default model is `claude-sonnet-5-5`; change it with `CLAUDE_MODEL`.

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
app.py                  cockpit home and navigation
views/                  the five tool pages
core/cockpit.py         cockpit data and its in-page component
core/changes.py         10-K diffing, change scores, the recent-filer screen, Claude explanations
core/track.py           calls, grading against SPY, month-block intervals, backtests
core/prices.py          cached daily prices
core/bulk_insider.py    SEC quarterly insider data sets
core/edgar.py           SEC client (filings, XBRL facts, Form 4, daily index)
core/debate.py          dossier and Claude agents
core/insider.py         Form 4 ingestion, cluster detection and events, alerts, CLI
core/mule/              synthetic and CSV data, graph features and rings, GNN, Neo4j
core/db.py              database schema (SQLite or PostgreSQL)
core/ui.py              shared styling
```

The project site is built with `python scripts/build_site.py site/` and published to the `gh-pages` branch.

Design notes: `docs/superpowers/specs/` (what and why) and `docs/superpowers/plans/` (how it was built).

Research tooling only. Nothing here is investment advice, and past results don't promise future ones.

## License

MIT. See [LICENSE](LICENSE).
