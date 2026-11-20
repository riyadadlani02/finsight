"""Build the static project site (GitHub Pages): the real cockpit component on a snapshot of real data.

    python scripts/build_site.py site/        # then publish site/ to the gh-pages branch

The app itself needs a Python server; this page runs the cockpit's own JS and CSS in the browser
against data captured when it was built.
"""
import json
import shutil
import sys
from datetime import date
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import cockpit  # noqa: E402

TICKERS = ["AAPL", "MSFT", "NVDA", "JPM", "TSLA"]
REPO = "https://github.com/riyadadlani02/finsight"

TOOLS = [  # (anchor, title, text, screenshot or None)
    ("filings", "Filing changes",
     "Compares each 10-K's Risk Factors and MD&A with the year before: new, dropped and reworded paragraphs, with a "
     "word-level redline. The change score is plain text similarity, so anyone can reproduce it, and Claude only "
     "explains paragraphs the comparison found. Research (Cohen, Malloy and Nguyen, \"Lazy Prices\", 2020) found that "
     "companies whose annual reports change a lot go on to underperform.", "filing-changes.jpg"),
    ("record", "Track record",
     "Every debate verdict, insider cluster and filing change becomes a dated call, graded against the S&P 500 at 30, "
     "90 and 180 days, live and on history back to 2006. Calls are dated when the information became public, every "
     "number carries a 95% interval that resamples whole months, and small groups say \"too few to judge\".",
     "track-record.jpg"),
    ("debate", "Bull vs Bear",
     "Two Claude agents argue for and against a stock using only its latest 10-K and 10-Q, tagging every claim with "
     "its source. A judge scores which side the filings support better and lists claims they don't.", "debate.jpg"),
    ("radar", "Insider radar",
     "Reads SEC Form 4 filings and flags companies where several insiders bought their own stock on the open market "
     "in the same few weeks. Grants, option exercises and pre-scheduled 10b5-1 trades are left out.", None),
    ("mule", "Mule ring detector",
     "Turns transfers into a graph, scores every account with a GraphSAGE network and groups flagged accounts into the "
     "rings that move money between each other. On the synthetic set it recovers all 8 planted rings.", "mule-ring.jpg"),
]

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FinSight</title>
<meta name="description" content="Research cockpit for US public companies and payment fraud: 10-K change detection, a graded track record, a Claude bull-vs-bear debate, an insider radar and a mule-ring detector.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@200..700&display=swap" rel="stylesheet">
<style>
:root { --ink:#EEF3F6; --muted:#9AABB6; --edge:rgba(255,255,255,.11); --glass:rgba(24,34,42,.46); }
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
body { margin: 0; font-family: Manrope, system-ui, sans-serif; color: var(--ink); background: #1B262D;
  background: radial-gradient(ellipse 60% 45% at 50% -8%, rgba(255,255,255,.16), transparent 70%),
              linear-gradient(180deg, #4A6474 0%, #2D404C 30%, #1B262D 70%) fixed; }
a { color: var(--ink); }
a:focus-visible, button:focus-visible { outline: 2px solid #F4F8FA; outline-offset: 2px; }
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 16px; }
header.top { position: sticky; top: 0; z-index: 5; background: rgba(24,34,42,.6); backdrop-filter: blur(18px);
  -webkit-backdrop-filter: blur(18px); border-bottom: 1px solid var(--edge); }
header.top .wrap { display: flex; align-items: center; gap: 22px; height: 56px; }
.brand { font-weight: 500; font-size: 1.1rem; margin-right: auto; text-decoration: none; }
header.top nav a { color: var(--muted); text-decoration: none; font-size: .9rem; }
header.top nav a:hover { color: var(--ink); }
header.top nav { display: flex; gap: 18px; }
.demo { padding: 36px 0 10px; }
.note { color: var(--muted); font-size: .82rem; text-align: center; margin: 14px auto 0; max-width: 70ch; }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%); background: var(--glass); border: 1px solid var(--edge);
  backdrop-filter: blur(18px); padding: 10px 16px; border-radius: 12px; font-size: .9rem; opacity: 0; transition: opacity .2s; pointer-events: none; }
.toast.on { opacity: 1; }
section.tool { display: grid; grid-template-columns: minmax(0, 5fr) minmax(0, 7fr); gap: 40px; align-items: center; padding: 70px 0;
  border-top: 1px solid var(--edge); scroll-margin-top: 60px; }
section.tool h2 { font-weight: 300; font-size: clamp(1.8rem, 3.5vw, 2.6rem); letter-spacing: -.03em; margin: 0 0 14px; }
section.tool p { color: #C9D4DA; line-height: 1.65; margin: 0; max-width: 60ch; }
section.tool img { width: 100%; height: auto; border-radius: 16px; border: 1px solid var(--edge); display: block; }
.scope, .run { padding: 60px 0; border-top: 1px solid var(--edge); }
.scope h2, .run h2 { font-weight: 300; font-size: 2rem; letter-spacing: -.03em; margin: 0 0 14px; }
.scope p, .run p { color: #C9D4DA; line-height: 1.65; max-width: 75ch; }
pre { background: var(--glass); border: 1px solid var(--edge); border-radius: 12px; padding: 14px 16px; overflow-x: auto; font-size: .85rem; }
footer { color: var(--muted); font-size: .8rem; padding: 30px 0 50px; border-top: 1px solid var(--edge); }
@media (max-width: 820px) { section.tool { grid-template-columns: 1fr; gap: 20px; padding: 48px 0; } header.top nav a.opt { display: none; } }
__COCKPIT_CSS__
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a class="brand" href="#">FinSight</a>
  <nav aria-label="Sections"><a class="opt" href="#filings">Filing changes</a><a class="opt" href="#record">Track record</a>
    <a href="#run">Run it</a><a href="__REPO__">GitHub</a></nav>
</div></header>

<main>
  <div class="wrap demo"><div id="cockpit"></div>
    <p class="note">This is the app's real cockpit running in your browser on data captured __DATE__. Switch between
      __TICKER_LIST__ from the name; the full app (a Python server) covers any US-listed company.</p></div>
  <div class="wrap">__TOOLS__
    <section class="scope" id="scope"><h2>Which markets</h2>
      <p>FinSight reads the US SEC's filings, so it covers companies that file with the SEC: 10-Ks for filing changes
        and debates, Form 4s for insider buying. Indian (SEBI, NSE/BSE) and Japanese (EDINET, TDnet) disclosures use
        different systems and aren't supported yet. The mule ring detector works on any market's transfer data.</p></section>
    <section class="run" id="run"><h2>Run it</h2>
      <p>Needs Python 3.11 or newer, a Claude API key for debates and explanations, and a name and email for the SEC's
        User-Agent rule.</p>
<pre><code>git clone __REPO__.git && cd finsight
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # set ANTHROPIC_API_KEY and SEC_USER_AGENT
streamlit run app.py</code></pre>
      <p><a href="__REPO__">Source, tests and design notes on GitHub</a></p></section>
  </div>
</main>
<footer><div class="wrap">MIT licensed. Research tooling only: nothing here is investment advice, and past results
  don't promise future ones.</div></footer>
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<script>
const SNAPSHOT = __DATA__;
__COCKPIT_JS__
const host = document.getElementById("cockpit"), toast = document.getElementById("toast");
let timer;
function say(text) { toast.textContent = text; toast.classList.add("on"); clearTimeout(timer); timer = setTimeout(() => toast.classList.remove("on"), 3200); }
function show(ticker) {
  mountCockpit({ data: SNAPSHOT[ticker], parentElement: host, setTriggerValue: (name, value) => {
    if (name === "ticker") SNAPSHOT[value] ? show(value) : say(`This demo includes __TICKER_LIST__. Run FinSight for ${value}.`);
    if (name === "open") document.getElementById(value)?.scrollIntoView();
  } });
}
show("AAPL");
</script>
</body>
</html>
"""


def build(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    snapshot = {t: cockpit.card_data(t) for t in TICKERS}
    tools = "".join(
        f'\n    <section class="tool" id="{anchor}"><div><h2>{escape(title)}</h2><p>{escape(text)}</p></div>'
        + (f'<img src="{shot}" alt="{escape(title)} in FinSight" loading="lazy" width="1600" height="1000">' if shot else "<div></div>")
        + "</section>"
        for anchor, title, text, shot in TOOLS)
    js = cockpit.JS.replace("export default function (component)", "function mountCockpit(component)", 1)
    assert "function mountCockpit" in js
    # </script> inside JSON would end the script tag early; company names come from the SEC, so guard anyway.
    data = json.dumps(snapshot).replace("</", "<\\/")
    html = (PAGE.replace("__COCKPIT_CSS__", cockpit.CSS).replace("__COCKPIT_JS__", js).replace("__DATA__", data)
            .replace("__TOOLS__", tools).replace("__REPO__", REPO).replace("__DATE__", f"{date.today():%d %b %Y}")
            .replace("__TICKER_LIST__", ", ".join(TICKERS[:-1]) + " and " + TICKERS[-1]))
    (out / "index.html").write_text(html)
    for _, _, _, shot in TOOLS:
        if shot:
            shutil.copy(ROOT / "docs" / "screenshots" / shot, out / shot)
    (out / ".nojekyll").write_text("")


if __name__ == "__main__":
    build(Path(sys.argv[1] if len(sys.argv) > 1 else "site"))
