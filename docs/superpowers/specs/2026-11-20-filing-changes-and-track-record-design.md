# Filing changes and track record

## Why

FinSight is for people who will use it, and it should give them depth they can check, not AI opinion. Two features do that:

1. **Filing changes**: what a company changed in its 10-K since last year, measured by code, with Claude only explaining the measured changes.
2. **Track record**: every signal FinSight produces (debate verdicts, insider clusters, filing changes) is graded against what the stock then did versus the market, live and on historical data.

Part 2 grades part 1's signal, so they are built in that order.

## Goals

- Diff any ticker's latest 10-K against the prior year's, for Risk Factors and MD&A: added, removed and reworded paragraphs, with a word-level redline for reworded ones.
- A change score per section that anyone can reproduce, and its history across the last six 10-Ks.
- A screen of companies that filed a 10-K in the last N days, ranked by change score.
- Claude explains the material changes, citing only paragraphs the diff produced.
- A shared record of calls from all three signals, graded at 30, 90 and 180 days against SPY.
- A historical insider backtest from the SEC's quarterly insider data sets, and a historical filing-change backtest over a fixed universe.
- A scoreboard that shows sample size and uncertainty next to every number.

## Non-goals

- Trading, order placement, portfolios or position sizing.
- 10-Q diffs, 8-Ks, earnings calls.
- Intraday data.

## Anti-goals (a failure even if it technically works)

- **Look-ahead bias.** Every call is dated by when the information became public: the 10-K filing date, not the period end; the Form 4 filing date of the purchase that completed a cluster, not the transaction date. Entry is the first close after that date.
- **Hidden uncertainty.** No hit rate or average return is shown without its count and a 95% confidence interval. Groups under 20 calls are labelled "too few to judge".
- **Unexplained survivorship bias.** yfinance lacks most delisted tickers. The scoreboard states how many calls had no price data and were dropped.
- **Claude inventing changes.** The explanation step receives only the diff output and must reference paragraph ids from it. Claims with unknown ids are dropped before display.
- **A number nobody can trace.** Every score and graded call links back to its filing, debate or cluster.

## Constraints

- Free data only: SEC EDGAR (10 requests/second, User-Agent required), SEC insider data sets, yfinance.
- Works on the default SQLite database; Postgres via Docker still works.
- Tests run offline with no API key, as today.
- Reuse what exists: `edgar.recent_filings`, `edgar.filing_sections`, `insider.find_clusters` logic, `debate.save`, `core/ui.py` styling.

## Approaches

Considered:

1. **Filing changes only.** Immediately useful, but its signal stays ungraded.
2. **Grading only.** Debates take months to accumulate; insider clusters can be backtested.
3. **Insider backtest only.** Smallest, narrowest.

**Picked: 1 and 2 together, built 1 then 2**, because the track record then grades all three signals, including the new one.

For the insider backtest, fetching Form 4s one at a time (as the radar does) would take hundreds of thousands of requests for a few years. The SEC publishes quarterly bulk files instead (`https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/<yyyy>q<n>_form345.zip`, 2006 onward, 8 to 16 MB each; 2026 Q2 and later live under `/files/datastandardsinnovation/data/insider-transactions-data-sets/`). The readme confirms the fields needed: `SUBMISSION` (accession, filing date, issuer CIK and ticker, document type, 10b5-1 flag from 2023), `REPORTINGOWNER` (owner CIK, relationship, title), `NONDERIV_TRANS` (transaction code, date, shares, price, acquired/disposed).

## Design

### Part 1: Filing changes (`core/changes.py`, page `views/4_Filing_Changes.py`)

- **Input:** a ticker. Fetch its last six 10-K filings (`10-K` only; amendments skipped) and extract Risk Factors and MD&A with the existing `filing_sections`, without the 30k-character truncation.
- **Change score:** for each section and each consecutive pair of years, TF-IDF cosine similarity (scikit-learn, already installed). Change score = 1 minus similarity, shown as 0 to 100.
- **Paragraph diff:** split sections into paragraphs; match each new paragraph to its most similar old one. At or above 0.9 similarity it's unchanged, 0.5 to 0.9 reworded, below 0.5 added. Old paragraphs left unmatched are removed. Reworded pairs get a word-level redline (difflib).
- **Claude explanation:** structured output (`output_config.format`, as the judge now uses), given the added, removed and reworded paragraphs with ids. It returns a list of material changes: id, kind (new risk, dropped risk, softened, strengthened, other), one-line summary. Unknown ids are dropped. Without an API key, the page still shows scores and the diff.
- **Screen:** companies that filed a 10-K in the last N days, from the SEC daily form index (same approach as `daily_form4_index`, form type `10-K`), each scored against its prior 10-K and ranked.
- **Storage:** a `filing_changes` table keyed by (issuer CIK, accession, section) with prior accession, filing date, similarity, and paragraph counts by kind, so the screen and backtest don't recompute.
- **Links:** "Debate this company" sends the ticker to Bull vs Bear, as the radar does.

### Part 2: Track record (`core/track.py`, `core/prices.py`, `core/bulk_insider.py`, page `views/5_Track_Record.py`)

- **Calls table:** kind (`debate`, `insider_cluster`, `filing_change`), ticker, issuer CIK, direction (+1, -1 or 0), strength, public date, source reference, and mode (`live` or `backtest`).
  - Debate: written by `debate.save`. Bull win is +1, bear win -1, draw 0; strength is the score margin.
  - Insider cluster: written when the radar detects one. Always +1; strength is the radar score.
  - Filing change: one call per 10-K (keyed by accession), on the company's combined change score (the mean across the sections found). Following Cohen, Malloy and Nguyen ("Lazy Prices", 2020), a score in the top quintile is -1 and the bottom quintile +1; others are 0 and not graded. Quintile cut-offs come from the filing-change backtest's distribution, never from whatever set the user happened to screen. Until a backtest exists, live filing-change calls are not recorded.
- **Live means recorded in time (builder's call, from review).** A call is `live` only if it was recorded on or before its public date's entry close; `recorded_at` is stored. Anything detected later is not recorded as live (it is the backtest's job). Live calls use fixed thresholds (the radar's defaults: 30-day window, 3 insiders, $100k), not the user's sliders, so the record isn't shaped by hindsight. One call per (kind, source reference): cluster key = issuer CIK + first buy date; filing key = accession; debate key = debate id.
- **Form 4 filing date (builder's call, from review).** `insider_tx` gains a `filed` column, added with an `ALTER TABLE` on startup when missing (works on SQLite and Postgres). New rows take it from the daily index day or the filing list. Existing rows stay empty and are never used for live calls.
- **Prices (`core/prices.py`):** daily adjusted closes from yfinance, cached in a `prices` table so each ticker is fetched once per day. SPY is the benchmark.
- **Grading:** for each horizon (30, 90, 180 calendar days), excess return = the ticker's return minus SPY's, from the entry close to the first close on or after the horizon. A call is "pending" until the horizon passes. A hit is an excess return whose sign matches the direction.
- **Scoreboard:** by kind, direction and mode: count, hit rate, mean and median excess return, each with a 95% interval from a block bootstrap that resamples by calendar month of the public date. Calls bunch in time (filing season, selloffs) and share the same SPY path, so resampling calls one by one would make intervals too narrow. The scoreboard shows the number of distinct months next to the count, and a group is "too few to judge" under 20 calls or under 6 distinct months (builder's call, from review).
- **Survivorship in the filing-change backtest (builder's call, from review).** Today's S&P 100 are survivors, so their absolute excess returns are biased upward. The headline result is the spread between the bottom and top quintile (both legs are survivors, so the bias largely cancels), with absolute numbers shown second and a stated caveat. An event-study chart plots average cumulative excess return by day since the call, with its band. A third row shows calls dropped for missing prices.
- **Insider backtest (`core/bulk_insider.py`):** download and cache the quarterly zips in `data/sec_bulk/`, and join the three tables into the same row shape as `insider_tx`. Cluster events: each time a company first meets the radar's thresholds within a window, dated by the filing date of the completing purchase, with no repeat event for that company until the window has passed. Default range: the last five years, adjustable back to 2006. Thresholds are the radar's current controls.
- **Filing-change backtest:** over a fixed universe, score every consecutive pair among the last six 10-Ks, take quintiles within each filing year, and record backtest calls. Default universe: a static list of S&P 100 tickers in `core/universe_sp100.txt` (committed and edited by hand; `data/` is git-ignored); the page also accepts a custom list.
- **Long jobs** (bulk downloads, scoring a universe) show progress and resume where they stopped, as the radar's market scan already does.

### Look: the company cockpit

Approved from a mockup (`docs/2026-11-21-cockpit-mockup.html`, inspired by Fantik Studio's CarWhisperer dashboard concept on Dribbble; the style is borrowed, not the artwork). It replaces the light "ledger" theme everywhere.

- **Stage:** slate-blue vertical gradient (`#4A6474` to `#2D404C` to `#1B262D`) with a soft light from above, faint light shafts and a floor glow. Applied to the whole app, all pages.
- **Type:** Manrope; large headings at weight 200 to 300 with tight tracking; tabular figures.
- **Home becomes the cockpit** for one company at a time: company name and ticker (with a ticker switcher), price with today and one-year change chips, the price line as the lit centrepiece with hover readout, a tick-arc range dial (1M, 3M, 1Y, 5Y), and four frosted-glass cards: Bull vs Bear, Insider buying, 10-K change, Track record. Each card shows its headline number and a mini chart and opens its full page for the same ticker.
- **Navigation:** sections in a glass pill at the top (`st.navigation(position="top")`); the left rail switches what the stage shows for the current company; the ticker carries between pages in the URL (`?ticker=`).
- **Colours:** bull `#5CC9A7`, bear `#E06A78`, line `#F4F8FA`, text `#EEF3F6`, muted `#9AABB6`; glass `rgba(24,34,42,.46)` with a 1px `rgba(255,255,255,.11)` edge and an 18px blur.
- **Other pages** keep their content and controls, restyled: glass panels instead of plain sections, light headings, charts and tables on the dark theme. The debate keeps its bull and bear edges and tug-of-war bar in the new colours.
- **Cards with no data** say so plainly and offer the action that fills them ("No debate yet: start one"), never placeholder numbers.
- **Build (builder's call):** the cockpit is a Streamlit v2 component (installed: `streamlit.components.v2`), so it renders in the page (glass blur works) and returns the chosen ticker and range to Python. If v2 can't do this, fall back to a v1 iframe component with the ticker input in Streamlit.
- One page-load moment: the price line draws in. Respect reduced motion; keyboard focus visible on every control; cards stack on narrow screens.

### Tests

Offline, with fixtures: change scores and paragraph classification on synthetic text; redline output; cluster-event detection and dating on synthetic trades; the bulk-file join on a tiny zip built in the test; grading math, pending status and confidence intervals on synthetic price series. No network, no API key.

## Left to the builder

- Exact page layout within the existing style in `core/ui.py`.
- Cache expiry and file layout under `data/`.
- Paragraph-splitting rules and the minimum paragraph length.
- Batch sizes and concurrency for yfinance and EDGAR, within the SEC's rate limit.
