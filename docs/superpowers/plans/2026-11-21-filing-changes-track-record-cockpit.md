# Filing changes, track record and cockpit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a 10-K change detector, a graded track record of every FinSight signal (live and backtested), and the cockpit look across the app.

**Architecture:** Pure scoring and grading logic lives in `core/` modules with offline tests (`core/changes.py`, `core/prices.py`, `core/track.py`, `core/bulk_insider.py`); Streamlit pages in `views/` only call them. New tables join the existing SQLAlchemy metadata in `core/db.py`. The cockpit home is a Streamlit v2 component in `core/cockpit.py`.

**Tech Stack:** Python 3.11+, Streamlit 1.67 (`st.components.v2`), SQLAlchemy (SQLite default), pandas, scikit-learn (TF-IDF), difflib, yfinance, Anthropic SDK (structured outputs), SEC EDGAR.

**Spec:** `docs/superpowers/specs/2026-11-20-filing-changes-and-track-record-design.md` (read it first; this plan argues from it). Mockup: `docs/2026-11-21-cockpit-mockup.html`.

## Global Constraints

- Free data only: SEC EDGAR through `edgar._get` (throttled, User-Agent), SEC insider data sets, yfinance.
- Default DB is SQLite; Postgres must keep working. Tests run offline, no API key, in-memory SQLite (`tests/test_core.py` fixture `engine`).
- Look-ahead: a call's `public_date` is when the information was public (10-K filing date; Form 4 filing date of the purchase that completed a cluster; debate run date). Entry = first close strictly after `public_date`.
- Horizons: 30, 90, 180 calendar days. Benchmark: `SPY`.
- "Too few to judge": n < 20 calls or < 6 distinct months.
- Change score = round((1 − cosine similarity) × 100). Paragraph match: ≥ 0.9 unchanged, 0.5 to < 0.9 reworded, < 0.5 added; unmatched old paragraphs removed.
- Filing-change direction: combined score ≥ 80th percentile of backtest scores → −1; ≤ 20th percentile → +1; else 0 (not graded). No backtest yet → no live filing-change calls.
- Live insider calls use fixed thresholds: window 30 days, 3 insiders, $100,000, lookback 180 days.
- Claude: `config.claude_model()`, `debate.FALLBACK` kwargs on `client.beta.messages`, `output_config.format` JSON schema, no forced `tool_choice`.
- Colours and type exactly as the spec's "Look" section. Never render SEC, user or Claude text as HTML without `html.escape`; cockpit JS sets text with `textContent` only.
- Copy rules: sentence case, plain verbs, empty states name the action that fills them.

## Review Focus

1. A ticker with fewer than two 10-Ks (new IPO, or a 20-F filer) should show a plain explanation, not an error. Test: `test_score_company_needs_two_annual_reports` (Task 3).
2. A year where Risk Factors or MD&A can't be extracted (smaller reporting companies may omit Item 1A) should skip that section for that pair, and the combined score should use what was found. Test: `test_score_company_skips_missing_section` (Task 3).
3. A delisted ticker with no yfinance prices should be counted as dropped, not crash grading or silently vanish. Test: `test_grade_marks_missing_prices` (Task 7).
4. Filing text, company names or Claude output containing `<script>` must render as text. Test: `test_redline_html_escapes` (Task 2); cockpit check in Task 12.
5. An SEC bulk quarter that isn't published yet (a future quarter, or a 404) should be skipped with a message, not abort the backtest. Test: `test_download_quarter_skips_missing` (Task 9).

---

### Task 1: Cockpit theme across the app

**Files:**
- Modify: `.streamlit/config.toml` (theme section), `core/ui.py` (CSS and colour constants), `app.py` (navigation position)
- Test: `tests/test_core.py`

**Interfaces:**
- Produces: `ui.BULL = "#5CC9A7"`, `ui.BEAR = "#E06A78"`, `ui.LINE = "#F4F8FA"`, `ui.INK = "#EEF3F6"`, `ui.MUTED = "#9AABB6"`; `ui.style()`, `ui.header(title, blurb, hero=False)`, `ui.status(rows)`, `ui.verdict_bar(...)`, `ui.point(...)` keep their signatures; new `ui.panel(key: str) -> st.container` returning `st.container(key=f"panel-{key}")`, styled as glass.

- [ ] **Step 1: Write the failing test**

```python
def test_theme_tokens():
    from core import ui
    assert (ui.BULL, ui.BEAR, ui.LINE) == ("#5CC9A7", "#E06A78", "#F4F8FA")
    assert "backdrop-filter" in ui.CSS and "Manrope" in ui.CSS and "#1B262D" in ui.CSS
```

- [ ] **Step 2: Run it:** `pytest tests/test_core.py::test_theme_tokens -v`, expect FAIL.
- [ ] **Step 3: Implement.** `config.toml`: `base = "dark"`, `backgroundColor = "#1B262D"`, `secondaryBackgroundColor = "#2D404C"`, `textColor = "#EEF3F6"`, `primaryColor = "#F4F8FA"`, Manrope via Google Fonts URL, `headingFontWeights = [300, 300, 400]`, `baseRadius = "14px"`, chart colours starting `#F4F8FA, #5CC9A7, #E06A78`. `ui.CSS`: the stage gradient and light shafts on `.stApp` (values from the spec), glass for `[class*="st-key-panel-"]`, `[data-testid="stExpander"]`, `[data-testid="stDataFrame"]` wrappers and the top nav; keep the debate-turn and verdict classes with the new colours. `app.py`: `st.navigation(..., position="top")`.
- [ ] **Step 4: Run the test** (PASS), then restart the app and screenshot every page at 1440×900 in the browser pane; text must stay readable on the stage.
- [ ] **Step 5: Commit** `feat: cockpit theme across the app`.

### Task 2: Change scoring and paragraph diff

**Files:**
- Create: `core/changes.py`
- Test: `tests/test_changes.py`

**Interfaces:**
- Produces:
  - `split_paragraphs(text: str, min_chars: int = 80) -> list[str]`
  - `@dataclass Para: id: str; kind: Literal["added","removed","reworded","unchanged"]; new: str | None; old: str | None; match: float`
  - `@dataclass SectionDiff: similarity: float; change_score: int; paras: list[Para]` with property `counts -> dict[str, int]`
  - `diff_sections(old: str, new: str) -> SectionDiff`; ids are `p1, p2, …` in new-text order, then `r1, r2, …` for removed
  - `redline_html(old: str, new: str) -> str`: word-level diff, `<del>`/`<ins>` around escaped words

- [ ] **Step 1: Write the failing tests**

```python
BASE = ["Supply chain disruption in Asia could reduce our ability to ship products on time to customers worldwide.",
        "We depend on a small number of carriers and contract manufacturers for most of our hardware output.",
        "Changes in foreign exchange rates may reduce reported revenue when the US dollar strengthens."]

def test_identical_sections_score_zero():
    d = changes.diff_sections("\n\n".join(BASE), "\n\n".join(BASE))
    assert d.change_score == 0 and d.counts == {"added": 0, "removed": 0, "reworded": 0, "unchanged": 3}

def test_added_removed_and_reworded():
    new = [BASE[0],
           "We depend on a small number of carriers and contract manufacturers, and a single supplier for our chips.",
           "Proposed tariffs on imported semiconductors could sharply raise component costs and compress margins."]
    d = changes.diff_sections("\n\n".join(BASE), "\n\n".join(new))
    assert d.counts == {"added": 1, "removed": 1, "reworded": 1, "unchanged": 1}
    assert 0 < d.change_score < 100 and [p.id for p in d.paras if p.kind == "removed"] == ["r1"]

def test_redline_html_escapes():
    out = changes.redline_html("risk <b>old</b>", "risk <script>x</script>")
    assert "<script>" not in out and "&lt;script&gt;" in out and "<ins>" in out and "<del>" in out
```

- [ ] **Step 2: Run:** `pytest tests/test_changes.py -v`, expect FAIL (module missing).
- [ ] **Step 3: Implement.** Section similarity: `TfidfVectorizer(stop_words="english")` fitted on the two sections, cosine of the two rows. Paragraph matching: one vectorizer fitted on all paragraphs of both sections; each new paragraph takes its best old match (greedy, an old paragraph used once), thresholds from Global Constraints. If the fixture wording lands on the wrong side of a threshold, adjust the fixture text, not the thresholds.
- [ ] **Step 4: Run** `pytest tests/test_changes.py -v`, expect PASS.
- [ ] **Step 5: Commit** `feat: 10-K change scoring and paragraph diff`.

### Task 3: Scoring companies from EDGAR, storage and the 10-K screen

**Files:**
- Modify: `core/edgar.py` (daily index), `core/db.py` (`filing_changes` table), `core/changes.py`
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: `diff_sections` (Task 2); `edgar.recent_filings`, `edgar.filing_sections`, `edgar.lookup`.
- Produces:
  - `edgar.daily_index(day: date, form: str) -> list[tuple[str, str, str]]` as `(cik10, accession, txt_url)`; `daily_form4_index` becomes a thin wrapper returning `(accession, txt_url)` for form `"4"` (its test must still pass)
  - `db.filing_changes`: `issuer_cik String(10)`, `ticker String(20)`, `accession String(25)`, `prev_accession String(25)`, `filed Date`, `section String(40)`, `similarity Float`, `change_score Integer`, `added/removed/reworded Integer`; unique `(accession, section)`
  - `changes.sections(filing: edgar.Filing) -> dict[str, str]`: full sections (no truncation), cached in memory by accession
  - `@dataclass YearChange: accession: str; prev_accession: str; filed: str; scores: dict[str, int]; combined: int` (combined = mean of available sections, rounded)
  - `changes.score_company(ticker: str, years: int = 6, engine=None) -> list[YearChange]`, oldest first, stores rows it hasn't stored before; raises `edgar.EdgarError("<TICKER> has fewer than two 10-K filings on EDGAR.")` when there are fewer than two
  - `changes.latest(ticker: str) -> dict`: `{"name", "ticker", "new": Filing, "old": Filing, "diffs": {section: SectionDiff}}`
  - `changes.screen(days: int, engine=None, progress=None) -> pd.DataFrame` with columns `ticker, name, filed, combined, risk_factors, mdna, accession`, sorted by `combined` descending; companies that fail are skipped

- [ ] **Step 1: Write the failing tests** (monkeypatch `edgar.lookup`, `edgar.recent_filings` and `changes.sections` with fixtures, using the `engine` fixture pattern from `tests/test_core.py`):
  - `test_score_company_stores_and_is_idempotent`: three fake 10-Ks give two `YearChange`s, oldest first; calling twice leaves 2 × (sections found) rows in `filing_changes`.
  - `test_score_company_needs_two_annual_reports`: one filing raises `EdgarError` whose message contains "fewer than two 10-K".
  - `test_score_company_skips_missing_section`: a year missing "Risk Factors" yields `scores` with only `"MD&A"` and `combined == scores["MD&A"]`.
  - `test_daily_index_10k`: an index text with `10-K`, `10-K/A` and `4` lines returns only the `10-K` row, with the CIK zero-padded to 10.
- [ ] **Step 2: Run:** `pytest tests/test_changes.py -v`, expect the 4 new tests FAIL.
- [ ] **Step 3: Implement** the interfaces above. `sections` calls `edgar.filing_sections(filing, max_chars=10**7)`. `screen` uses `daily_index(day, "10-K")` for each of the last `days` days, then `score_company(ticker, years=2)` per CIK, mapping CIK → ticker through `edgar.ticker_map()` (CIKs without a ticker are skipped).
- [ ] **Step 4: Run** `pytest -q`, expect all PASS (including the existing `test_daily_index_and_facts`).
- [ ] **Step 5: Commit** `feat: score companies' 10-K changes and screen recent filers`.

### Task 4: Claude explains the material changes

**Files:**
- Modify: `core/changes.py`
- Test: `tests/test_changes.py`

**Interfaces:**
- Consumes: `SectionDiff`, `Para` (Task 2); `debate._client`, `debate.FALLBACK`.
- Produces: `changes.explain(name: str, ticker: str, diffs: dict[str, SectionDiff]) -> list[dict]`; each dict is `{"id": str, "section": str, "kind": "new risk"|"dropped risk"|"softened"|"strengthened"|"other", "summary": str}`; ids not present in `diffs` are dropped.

- [ ] **Step 1: Write the failing test** `test_explain_drops_unknown_ids`: monkeypatch `debate._client` with a fake whose `beta.messages.create` returns a response with `stop_reason="end_turn"` and one text block of JSON containing ids `p2` (exists) and `p99` (doesn't); assert one item comes back, with `id == "p2"`, and that the request carried `output_config["format"]["type"] == "json_schema"`.
- [ ] **Step 2: Run**, expect FAIL.
- [ ] **Step 3: Implement.** Send only non-unchanged paragraphs, labelled `[section id kind]` with old and new text, capped at 60,000 characters total (the most changed first). System prompt: explain only the listed paragraphs, cite their ids, skip trivial edits, one sentence each. `max_tokens=16000`.
- [ ] **Step 4: Run**, expect PASS. **Step 5: Commit** `feat: Claude explains material 10-K changes`.

### Task 5: Filing changes page

**Files:**
- Create: `views/4_Filing_Changes.py`
- Modify: `app.py` (add `st.Page("views/4_Filing_Changes.py", title="Filing changes")`)

**Interfaces:**
- Consumes: `changes.score_company`, `changes.latest`, `changes.explain`, `changes.redline_html`, `changes.screen` (Tasks 2 to 4); `ui.header`, `ui.panel`.
- Produces: the page reads and writes `st.query_params["ticker"]`.

- [ ] **Step 1: Build the page.** It has two tabs:
  - **Company:** ticker input, a combined change score with the per-section scores, and a six-year history chart of the combined score. The paragraph list is grouped by kind (added, removed, reworded with its redline, unchanged collapsed). An "Explain the changes" button calls Claude and shows each explanation above its paragraph. The tab ends with a "Debate this company" link that passes the ticker.
  - **Recently filed:** a days selector (1 to 30, default 7), a "Scan" button with progress, and a ranked table with a link per row that opens the company tab.

  The `EdgarError` message is shown as `st.info`. Paragraph text goes through `html.escape` before any `st.html`.
- [ ] **Step 2: Verify in the browser pane:** AAPL shows scores for every 10-K pair and a redline; a ticker with one 10-K shows the "fewer than two" message; the Explain button returns items whose ids exist on the page.
- [ ] **Step 3: Commit** `feat: filing changes page`.

### Task 6: Cached daily prices

**Files:**
- Create: `core/prices.py`
- Modify: `core/db.py` (`prices` table: `ticker String(20)`, `date Date`, `close Float`, `fetched Date`; unique `(ticker, date)`)
- Test: `tests/test_track.py`

**Interfaces:**
- Produces: `prices.closes(tickers: list[str], start: date, engine=None, fetch=None) -> pd.DataFrame` (DatetimeIndex, one column per ticker, adjusted closes). `fetch(tickers, start) -> pd.DataFrame` defaults to yfinance download and is injectable for tests. A ticker is refetched only if its newest `fetched` is before today. Tickers with no data become an all-NaN column.

- [ ] **Step 1: Write the failing test** `test_closes_caches_and_fills_missing`: a fake fetch returns data for `AAA` and nothing for `GONE`; the first call queries both, a second call the same day calls fetch zero times; the result has an all-NaN `GONE` column.
- [ ] **Step 2: Run**, expect FAIL. **Step 3: Implement** (batch download with `auto_adjust=True`). **Step 4: Run**, expect PASS. **Step 5: Commit** `feat: cached daily prices`.

### Task 7: Calls, grading and the scoreboard

**Files:**
- Create: `core/track.py`
- Modify: `core/db.py` (`calls` table)
- Test: `tests/test_track.py`

**Interfaces:**
- Consumes: `prices.closes` (Task 6).
- Produces:
  - `db.calls`: `id`, `kind String(20)`, `ticker String(20)`, `issuer_cik String(10)` (nullable), `direction Integer`, `strength Float`, `public_date Date`, `recorded_at DateTime`, `source_ref String(80)`, `mode String(10)`; unique `(kind, source_ref, mode)`
  - `track.entry_date(public_date: date) -> date`: next business day (`pandas.offsets.BDay`)
  - `track.record(kind, ticker, direction, strength, public_date, source_ref, mode="live", issuer_cik=None, engine=None, today=None) -> bool`: False if it already exists, or if `mode == "live"` and `today > entry_date(public_date)`
  - `track.grade(calls: pd.DataFrame, closes: pd.DataFrame, today: date, horizons=(30, 90, 180)) -> pd.DataFrame` adding `entry`, and per horizon `excess_{h}` (float or NaN), `hit_{h}` (bool or NA), `status_{h}` in `{"graded", "pending", "no_price"}`
  - `track.block_ci(values: pd.Series, months: pd.Series, stat=np.mean, n=2000, seed=0) -> tuple[float, float]`: 95% percentile interval from resampling whole months
  - `track.scoreboard(graded: pd.DataFrame, horizon: int) -> pd.DataFrame` with one row per `(kind, direction, mode)`: `n, months, hit_rate, hit_lo, hit_hi, mean, mean_lo, mean_hi, median, too_few, dropped`
  - `track.event_curve(calls, closes, days: int = 180) -> pd.DataFrame` indexed by trading day 0..days: `mean, lo, hi` of cumulative excess (band from `block_ci` at every 5th day, interpolated)

- [ ] **Step 1: Write the failing tests** with synthetic business-day prices where `XYZ` rises 1% a day and `SPY` is flat:
  - `test_grade_uses_first_close_after_public_date`: public date Friday 2026-01-02 → entry Monday 2026-01-05; `excess_30 > 0`; `hit_30` is True for direction +1 and False for −1.
  - `test_grade_pending_before_horizon`: `today` 10 days after entry gives `status_30 == "pending"`.
  - `test_grade_marks_missing_prices`: an all-NaN ticker gives `status_* == "no_price"`; `scoreboard(...)["dropped"]` counts it.
  - `test_record_live_window`: record with `today` equal to the entry date succeeds; a day later returns False; a duplicate returns False.
  - `test_scoreboard_too_few`: 25 calls all in one month give `too_few` True (months < 6), and `block_ci` gives a wider interval than a call-level bootstrap on the same values.
- [ ] **Step 2: Run** `pytest tests/test_track.py -v`, expect FAIL. **Step 3: Implement.** **Step 4: Run**, expect PASS. **Step 5: Commit** `feat: grade calls against SPY with month-block intervals`.

### Task 8: Record live calls from debates, the radar and filing changes

**Files:**
- Modify: `core/db.py` (add `insider_tx.filed` + startup migration), `core/insider.py`, `core/debate.py` (`save`), `core/changes.py` (`score_company`), `core/track.py`
- Test: `tests/test_track.py`, `tests/test_core.py`

**Interfaces:**
- Consumes: `track.record` (Task 7), `changes.score_company` (Task 3).
- Produces:
  - `db.engine()` runs `ALTER TABLE insider_tx ADD COLUMN filed DATE` when the column is missing (checked with `sqlalchemy.inspect`)
  - `insider._store` writes `filed`: the daily index day in `scan_market`, `Filing.filed` in `scan_tickers`
  - `insider.cluster_events(purchases: pd.DataFrame, window_days=30, min_insiders=3, min_total_value=100_000) -> pd.DataFrame`: one row per event, columns `issuer_cik, ticker, public_date, first_buy, insiders, total_value, score`. An event is the first purchase (in filing-date order) at which the window ending at it meets the thresholds; `public_date` is that purchase's `filed`. No new event for that issuer until `window_days` after it. Rows without `filed` are ignored.
  - `insider.record_live_calls(engine=None) -> int`: `cluster_events` on `load_purchases(lookback_days=180)` with the fixed thresholds, then `track.record("insider_cluster", ..., direction=1, strength=score, source_ref=f"{issuer_cik}:{first_buy}")`; called at the end of both scans
  - `debate.save` records `("debate", ticker, +1/−1/0 by winner, abs(bull − bear), today, str(debate_id))`
  - `track.filing_cutoffs(engine=None) -> tuple[float, float] | None`: 20th and 80th percentiles of `strength` over backtest `filing_change` calls (None if there are none); `score_company` records a live call for each new `YearChange` when cut-offs exist (`source_ref = accession`)

- [ ] **Step 1: Write the failing tests:**
  - `test_cluster_events_dated_by_filing`: three insiders buy on Nov 2, 3 and 4, filed Nov 4, 5 and 9; one event with `public_date == 2026-11-09`; a fourth buy on Nov 10 adds no event.
  - `test_filed_column_migration`: a SQLite file created with the old `insider_tx` schema gains `filed` after `db.engine(url)`.
  - `test_debate_save_records_call`: after `debate.save` (existing fixture in `test_debate_dossier_and_history`), there's one `debate` call with direction −1 for a bear win.
- [ ] **Step 2: Run**, expect FAIL. **Step 3: Implement.** **Step 4: Run** `pytest -q`, expect all PASS. **Step 5: Commit** `feat: record live calls from debates, radar and filing changes`.

### Task 9: SEC bulk insider data

**Files:**
- Create: `core/bulk_insider.py`
- Test: `tests/test_track.py`

**Interfaces:**
- Produces:
  - `quarter_url(year: int, q: int) -> str`: path segment `structureddata` before 2026 Q2, `datastandardsinnovation` from 2026 Q2; filename `<yyyy>q<n>_form345.zip`
  - `download(year: int, q: int, dest: Path = DATA / "sec_bulk") -> Path | None`: cached; uses `edgar._get`; returns None on `EdgarError` (not published)
  - `load_quarter(zip_path: Path) -> pd.DataFrame`: open-market purchases only (`TRANS_CODE == "P"`, acquired `A`, `DOCUMENT_TYPE == "4"`), with the column names `insider.load_purchases` returns plus `filed` and `accession`; one reporting owner per accession (the first)

- [ ] **Step 1: Write the failing tests:**
  - `test_quarter_url_paths`: 2025 Q4 → `.../structureddata/data/insider-transactions-data-sets/2025q4_form345.zip`; 2026 Q2 → `.../datastandardsinnovation/...`.
  - `test_download_quarter_skips_missing`: `edgar._get` raises `EdgarError` → None, nothing written.
  - `test_load_quarter_joins_tables`: build a zip in `tmp_path` with `SUBMISSION.tsv`, `REPORTINGOWNER.tsv` and `NONDERIV_TRANS.tsv` (one P buy, one S sale); one row with ticker, filed date, price × shares as `value`, `is_director` parsed from `RPTOWNER_RELATIONSHIP`.
- [ ] **Step 2: Run**, expect FAIL.
- [ ] **Step 3: Implement.** Read column names and the date format from the SEC readme (`https://www.sec.gov/files/insider_transactions_readme.pdf`). Fields: `ACCESSION_NUMBER, FILING_DATE, ISSUERCIK, ISSUERTRADINGSYMBOL, DOCUMENT_TYPE, AFF10B5ONE` (SUBMISSION); `RPTOWNERCIK, RPTOWNERNAME, RPTOWNER_RELATIONSHIP, RPTOWNER_TITLE` (REPORTINGOWNER); `TRANS_CODE, TRANS_DATE, TRANS_SHARES, TRANS_PRICEPERSHARE, TRANS_ACQUIRED_DISP_CD` (NONDERIV_TRANS). Download one real quarter by hand to confirm the format before trusting the fixture.
- [ ] **Step 4: Run**, expect PASS. **Step 5: Commit** `feat: load SEC quarterly insider data sets`.

### Task 10: Backtests

**Files:**
- Modify: `core/track.py`
- Create: `core/universe_sp100.txt` (one ticker per line, current S&P 100 members)
- Test: `tests/test_track.py`

**Interfaces:**
- Consumes: `bulk_insider.*` (Task 9), `insider.cluster_events` (Task 8), `changes.score_company` (Task 3), `track.record` (Task 7).
- Produces:
  - `track.backtest_insider(years: int = 5, engine=None, progress=None) -> int` (calls recorded): every quarter in range; `cluster_events` with the radar defaults; `mode="backtest"`, `source_ref=f"{issuer_cik}:{first_buy}"`
  - `track.backtest_filings(tickers: list[str], engine=None, progress=None) -> int`: `score_company(t, years=6)` per ticker; quintiles of `combined` within each filing year across all tickers; direction −1 for the top, +1 for the bottom, 0 otherwise; `strength = combined`; `source_ref = accession`
  - `track.universe() -> list[str]` reads `core/universe_sp100.txt`

- [ ] **Step 1: Write the failing test** `test_backtest_filings_quintiles`: monkeypatch `score_company` to return 10 tickers × 1 year with combined scores 0 to 90; 2 calls at −1 (80 and 90), 2 at +1 (0 and 10), 6 at 0; then `filing_cutoffs()` returns (18.0, 72.0) for those strengths (numpy linear percentiles).
- [ ] **Step 2: Run**, expect FAIL. **Step 3: Implement.** **Step 4: Run**, expect PASS. **Step 5: Commit** `feat: insider and filing-change backtests`.

### Task 11: Track record page

**Files:**
- Create: `views/5_Track_Record.py`
- Modify: `app.py` (add the page)

**Interfaces:**
- Consumes: `track.grade`, `track.scoreboard`, `track.event_curve`, `track.backtest_insider`, `track.backtest_filings`, `track.universe`, `prices.closes`.

- [ ] **Step 1: Build the page:**
  - A horizon selector (30, 90 or 180 days) and a mode selector (Live, Backtest).
  - A scoreboard table: n, months, hit rate with its interval, mean excess with its interval, median, and a "too few to judge" label. Dropped calls are shown in their own line.
  - An event-study chart with its band.
  - For the filing backtest, the headline is the bottom-minus-top quintile spread, followed by the survivorship caveat in plain words.
  - A "Run backtests" panel with an insider years selector (1 to 20, default 5), a universe text area defaulting to `track.universe()`, buttons with progress, and a note that the first run downloads about 10 MB per quarter.
  - A calls table with links to sources: the SEC filing index for filings and clusters, and Bull vs Bear with `?ticker=` for debates.
- [ ] **Step 2: Verify in the browser:** run the insider backtest for 1 year, and the filing backtest for 10 tickers from the universe. The scoreboard shows counts, intervals and dropped rows, and the chart renders.
- [ ] **Step 3: Commit** `feat: track record page`.

### Task 12: Cockpit home

**Files:**
- Create: `core/cockpit.py` (component definition: HTML, CSS and JS adapted from `docs/2026-11-21-cockpit-mockup.html`)
- Modify: `app.py` (`home()` renders the cockpit)
- Test: `tests/test_track.py` (data assembly)

**Interfaces:**
- Consumes: `prices.closes`, `debate.history`, `insider.load_purchases`, `db.filing_changes`, `track.scoreboard`.
- Produces:
  - `cockpit.card_data(ticker: str, engine=None) -> dict`: `{"name", "ticker", "exchange", "last", "chg_1d", "chg_1y", "daily_1y": [...], "weekly_5y": [...], "cards": {"debate": {...} | None, "insiders": {...} | None, "filing": {...} | None, "record": {...} | None}}`, where None means no data yet
  - `cockpit.render(data: dict, key: str = "cockpit") -> str | None`: returns a new ticker when the user switches it (trigger value `ticker`)
  - `clean_ticker(s: str) -> str | None`: uppercase, `^[A-Z0-9.\-]{1,10}$`, else None

- [ ] **Step 1: Write the failing tests:** `test_card_data_empty_states` (no rows in the DB → all four cards None, with price data from a fake `prices.closes`) and `test_clean_ticker` (`"aapl"` → `"AAPL"`, `"<script>"` → None, `"BRK.B"` → `"BRK.B"`).
- [ ] **Step 2: Run**, expect FAIL.
- [ ] **Step 3: Implement.** The JS builds every text node with `textContent`. A card with None shows its empty state ("No debate yet", "No insider buying in 90 days", "Not scored yet", "No graded calls yet"), and every card links to its page with `?ticker=` (`target="_top"`). The ticker switcher is an inline input behind the chevron. `home()` reads `st.query_params.get("ticker", "AAPL")`, passes the result through `clean_ticker`, and writes the switched ticker back to the query params.
- [ ] **Step 4: Run the tests** (PASS), then check in the browser at 1440×900 and 390×844:
  - the range dial and hover work;
  - the cards link to the right pages and ticker;
  - a stock with no data shows the empty states;
  - a company name containing `<b>` renders as text;
  - keyboard focus is visible;
  - the cards stack on mobile.
- [ ] **Step 5: Commit** `feat: cockpit home`.

### Task 13: Ticker continuity, README and publish

**Files:**
- Modify: `views/1_Bull_vs_Bear.py`, `views/2_Insider_Radar.py`, `views/3_Mule_Ring_Detector.py` (glass panels), `README.md`, `docs/screenshots/*`

- [ ] **Step 1:** Bull vs Bear and the radar read `?ticker=`, and the radar's "debate" link sets it. Wrap each page's sections in `ui.panel`.
- [ ] **Step 2:** `pytest -q` passes. Screenshot the cockpit, filing changes, track record, debate and mule pages into `docs/screenshots/`, and update the README sections for the new features and the backtest caveats.
- [ ] **Step 3:** Run the secret scan from the first publish (`grep -lE "sk-ant-|gho_|@gmail\.com"` over staged files must be empty), commit `docs: cockpit, filing changes and track record`, then `git push`.
