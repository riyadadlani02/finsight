# India and Japan markets

## Why

FinSight's users are Indian retail investors. Today every signal comes from the US SEC. This adds India and Japan as markets, so the cockpit, the signals and the track record work for an Indian or Japanese company with the same depth: scores from the regulator's or exchange's own data, Claude only arguing or explaining that data, and every signal graded against the right local market.

Indian retail investors watch different signals from US ones: promoter buying, selling and pledging, bulk and block deals, and the quarterly shareholding pattern. The India side is built around those, not a copy of the US tools.

## Decisions already made (by the user)

- **Who:** Indian retail investors. **First markets:** India and Japan.
- **Data (option 3):** free official files where they exist; paid data only for what has no clean free source. Phase 2 needs a paid Indian market-data feed; the vendor is chosen after Phase 1 ships.
- **Architecture (option 1):** market adapters in one app, not separate apps or India-only pages.

## Goals (Phase 1, free data)

- A market switcher (US, India, Japan) in the cockpit; each card shows only tools its market supports and says plainly when a tool isn't available there yet.
- **Japan:** Filing changes and Bull vs Bear from EDINET annual securities reports (有価証券報告書): the business-risk section (事業等のリスク) and MD&A (経営者による財政状態、経営成績及びキャッシュ・フローの状況の分析). Filing-change calls graded against TOPIX.
- **India:** a Deals radar on NSE's daily bulk and block deals, deal signals recorded as calls and graded against the Nifty 50, INR prices.
- **Track record per market:** each call graded against its own market's benchmark; the scoreboard can filter by market and never mixes currencies or benchmarks in one number.
- The US keeps working exactly as today.

## Non-goals (Phase 1)

- India: promoter buys, sells and pledges; annual-report Filing changes and debates; shareholding pattern. These are Phase 2 (paid feed; NSE/BSE pages that carry them block automated access).
- Japan: a radar on large-shareholder (5%) reports. Japan publishes no officer-trade data; this is Phase 2.
- Other markets (UK, EU, Hong Kong...). The adapter design should make them possible, not do them.
- Translating the interface into Hindi or Japanese. Claude writes in English; Japanese filing text is shown as filed.

## Anti-goals (a failure even if it technically works)

- **Scraping pages that block scripts or whose terms forbid it.** Phase 1 uses only the EDINET API and NSE archive files that download as plain files (verified 2026-11-21: `https://nsearchives.nseindia.com/content/equities/bulk.csv` and `block.csv` return CSV).
- **Grading a market against the wrong benchmark** (an Indian call against SPY), or showing one number that mixes markets or currencies.
- **Treating churn as conviction.** Many bulk-deal clients are proprietary and arbitrage traders that buy and sell the same stock the same day; their trades are not a signal.
- **"No data" when the truth is "not available in this market yet."** Empty states must say which.
- Look-ahead and hidden uncertainty, as in the previous design: unchanged rules apply to every market.

## Constraints

- EDINET API v2 needs a free API key (`Subscription-Key`), from an account the user registers on EDINET (confirmed in the API spec, ESE140206.pdf, Version 2). Read from `EDINET_API_KEY` in `.env`; Japan features say how to get one when it's missing.
- EDINET has no per-company endpoint: filings are listed by date (`/api/v2/documents.json?date=…&type=2`) and downloaded by document id.
- NSE's `bulk.csv` and `block.csv` hold the latest trading day only. History accrues from the first daily ingest unless dated archive files exist (to be verified while building).
- Japanese text has no spaces between words, so word-based TF-IDF and word-level redlines don't work on it.
- Same as before: SQLite by default, Postgres still works, tests offline with no keys.

## Approaches

1. **Market adapters in one app (picked).** A small adapter per market answers the same questions; scoring, grading and the cockpit stay shared.
2. Separate India and Japan apps: faster start, but every fix happens three times and the copies drift.
3. India-only pages bolted on: cheapest, but the cockpit and track record stay US-only.

## Design

### Identity: one ticker string per security

Every security is keyed by its Yahoo Finance symbol, which is unique across markets: `AAPL`, `RELIANCE.NS`, `7203.T`. Existing `ticker` columns keep working. A `market` column (`US`, `IN`, `JP`) is added to `calls` and `filing_changes` with the same startup migration pattern as `insider_tx.filed`; existing rows are `US`. Issuer ids stay market-specific strings (CIK for US, EDINET code for JP).

### Market adapters (`core/markets/`)

`core/markets/__init__.py` defines a `Market` record and a registry:

- `code`, `name`, `currency` (`USD`, `INR`, `JPY`), `currency_symbol`, `benchmark` (Yahoo symbol), `benchmark_name`
- `tools`: which of `filings`, `debate`, `insider`, `deals` it supports
- `resolve(user_text) -> Company | None`: what the user typed into the Yahoo symbol, name and issuer id (`RELIANCE` → `RELIANCE.NS` in India; in Japan the 4-digit securities code, `7203` → `7203.T`)
- for markets with `filings`: `annual_reports(company, limit) -> list[Report]` (newest first: id, filed date, period, url) and `sections(report) -> {"Risk Factors": text, "MD&A": text}` with the same canonical section names as today

Adapters: `us.py` wraps today's EDGAR code; `jp.py` uses EDINET; `india.py` provides resolve, benchmark and deals.

Benchmarks are total-return, like SPY, because stock closes are dividend-adjusted (builder's call, from review): India `NIFTYBEES.NS` (a Nifty 50 ETF, labelled "Nifty 50"), Japan `1306.T` (a TOPIX ETF, labelled "TOPIX"). Grading adjusted stocks against a price index like `^NSEI` would flatter every bullish call by the index's dividend yield.

### Japan (EDINET)

- **Company index.** The official EDINET code list (`EdinetcodeDlInfo`, a downloadable CSV of EDINET code, securities code and names) maps 4-digit securities codes to EDINET codes. Cached in `data/`, refreshed monthly.
- **Annual-report index.** A `jp_reports` table (EDINET code, document id, filed date, period end, document type) filled by scanning `documents.json` day by day and keeping annual securities reports (document type code 120). The first run backfills six years (about 1,500 requests at a polite rate, resumable, with progress); after that each scan only adds new days.
- **Sections.** Downloaded per report (cached), taking the business-risk and MD&A text blocks from the XBRL or CSV rendition; the exact element names come from the EDINET taxonomy (builder verifies on a real report and records them).
- **Text scoring.** `changes.diff_sections` takes a `language`; for Japanese it uses character 2–3-gram TF-IDF, splits paragraphs on line breaks and 。-terminated blocks, and redlines by character. US scoring is unchanged.
- **Backtest and cut-offs (builder's call, from review).** Mirrors the US: a hand-edited Nikkei 225 list (`core/universe_nikkei225.txt`, 4-digit codes) scored over its last six annual reports, about 1,350 report downloads, resumable. Japan's 20th/80th-percentile cut-offs come only from Japanese backtest scores, ranked within each filing year (character n-gram scores aren't on the US scale). Live Japan calls start once those exist. The headline is the gap between the least- and most-changed fifth, with the survivorship caveat.
- **Debate.** The dossier uses the Japanese sections plus Yahoo market data (in yen); agents are told the filing is in Japanese and answer in English, quoting the original. XBRL financials for Japan are out of Phase 1 unless the CSV rendition makes them trivial (builder's call).

### India (NSE deals)

- **Ingest.** `core/markets/india.py` downloads `bulk.csv` and `block.csv` once per trading day (CLI `python -m core.markets.india ingest`, like the insider scan), storing rows in an `in_deals` table (date, symbol, name, client, side, quantity, price, kind bulk/block). Idempotent per (date, symbol, client, side, quantity, price, kind).
- **Churn filter.** A client who both buys and sells the same symbol on the same day is excluded from that day's signal.
- **Signal (builder's call, from review).** For each symbol and day, sum the net value of the clients left after the churn filter. A combined net of at least ₹5 crore bought, with at least two of those clients net buyers, is a +1 call; the mirror image (₹5 crore sold, two net sellers) is −1. A day can't be both. One call per run: no new call for the same symbol and direction until 30 days after the last, so a streak of buying days counts once (`source_ref` = symbol:date of the first day). Thresholds are named constants. `public_date` is the deal date (NSE publishes the file that evening), so entry is the next close.
- **Deals radar page** (`views/6_Deals_Radar.py`, India): recent days' net buying and selling by stock, the clients behind each, the churners left out and why, and a link to the debate when Phase 2 adds it.

### Shared changes

- **Track record.** `grade` looks up each call's benchmark from its market. The page gets a market filter (default: the market chosen in the cockpit) and labels "vs Nifty 50", "vs TOPIX", "vs S&P 500". The scoreboard groups by market as well as kind and direction.
- **Prices.** Unchanged (`prices.closes` already takes any Yahoo symbol); currency shown from the market.
- **Cockpit.** A market switcher next to the ticker switcher; `?market=IN&ticker=RELIANCE.NS` in the URL. Cards: US as today; India shows Deals in place of Insider buying, and Filing changes and Bull vs Bear as "Coming with Phase 2"; Japan shows Filing changes, Bull vs Bear and Track record, with Insider buying as "Not published in Japan". Prices in ₹ or ¥.
- **Filing changes page.** Market selector; accepts Japanese companies by 4-digit code; the "Recently filed" screen for Japan uses the `jp_reports` index.
- **Static site.** The snapshot adds `RELIANCE.NS` and `7203.T`; the "Which markets" section is updated.

### Tests

Offline fixtures: an EDINET `documents.json` page and a small report rendition; the EDINET code list CSV; NSE `bulk.csv`/`block.csv` samples including a same-day churner; Japanese text through `diff_sections` (identical → 0, one added paragraph detected, character redline escapes HTML); per-market benchmark grading; the market migration on an old database.

## Phase 2 (after Phase 1 ships; needs the vendor decision)

India promoter buys, sells and pledges (a Promoter radar), shareholding pattern, and annual-report Filing changes and debates; Japan large-shareholder radar.

## Left to the builder

- EDINET XBRL/CSV element names, the TOPIX symbol, and whether NSE serves dated deal archives (verify, record in the code, test with real fixtures).
- Request pacing for EDINET and NSE (polite, resumable).
- Page layouts within the existing look.
