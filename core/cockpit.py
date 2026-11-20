"""The home cockpit: one company on stage, its price as the lit centrepiece, a glass card per tool.

The component is trusted code (it runs in the page, not an iframe). Everything it shows comes in
as data and is written with textContent, so company names and filing text can never become HTML.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pandas as pd
import requests
from sqlalchemy import select

from . import db, markets, prices

_TICKER = re.compile(r"^[A-Z0-9.\-]{1,20}$")  # NSE symbols plus .NS run past 10


def clean_ticker(s: str | None) -> str | None:
    s = (s or "").strip().upper()
    return s if _TICKER.match(s) else None


def _num(x) -> float | None:
    return None if x is None or pd.isna(x) else round(float(x), 2)


def card_data(ticker: str, engine=None) -> dict:
    """Everything the cockpit shows for one ticker. A card is None when there's no data for it yet."""
    engine = engine or db.engine()
    market = markets.get(markets.of_ticker(ticker))
    try:
        company = market.resolve(ticker)
    except requests.RequestException:
        company = None
    name = company.name if company else ticker
    closes = prices.closes([ticker], date.today() - timedelta(days=5 * 365 + 10), engine=engine)
    s = closes[ticker].dropna() if ticker in closes else pd.Series(dtype=float)
    data = {"name": name, "ticker": ticker, "meta": "", "last": None, "chg_1d": None, "chg_1y": None,
            "daily_1y": [], "weekly_5y": [], "market": market.code, "symbol": market.symbol,
            "tools": sorted(market.tools), "benchmark_name": market.benchmark_name}
    if len(s) >= 2:
        data.update(last=_num(s.iloc[-1]), chg_1d=_num((s.iloc[-1] / s.iloc[-2] - 1) * 100),
                    chg_1y=_num((s.iloc[-1] / s.iloc[-min(len(s), 252)] - 1) * 100),
                    daily_1y=[_num(v) for v in s.iloc[-252:]],
                    weekly_5y=[_num(v) for v in s.resample("W").last().dropna()])

    with engine.connect() as conn:
        t = db.promises
        latest = conn.execute(select(t.c.latest).where(t.c.ticker == ticker).order_by(t.c.checked_at.desc()).limit(1)).scalar()
        ledger = [dict(r._mapping) for r in conn.execute(select(t.c.verdict).where(t.c.ticker == ticker, t.c.latest == latest))]
        t = db.insider_tx
        buys = pd.read_sql(select(t.c.insider_cik, t.c.value, t.c.tx_date).where(
            t.c.ticker == ticker, t.c.code == "P", t.c.acquired.is_(True), t.c.value > 0,
            t.c.tx_date >= date.today() - timedelta(days=90)), conn)
        t = db.filing_changes
        fc = pd.read_sql(select(t.c.accession, t.c.filed, t.c.change_score).where(t.c.ticker == ticker), conn)
        t = db.calls
        calls = pd.read_sql(select(t.c.kind, t.c.direction, t.c.public_date, t.c.mode).where(t.c.ticker == ticker), conn)

    cards: dict = {"said": None, "insiders": None, "deals": None, "filing": None, "record": None}
    if "deals" in market.tools:
        from .markets import india
        recent = india.load_deals(engine, since=date.today() - timedelta(days=90))
        recent = recent[recent["symbol"] == ticker.removesuffix(".NS").removesuffix(".BO")]
        if not recent.empty:
            days = india.day_summary(recent)
            cards["deals"] = {"net_crore": round(float(days["net_value"].sum()) / 1e7, 1), "days": len(days),
                              "signals": int((days["signal"] != 0).sum()), "last": str(max(days["date"]))}
    if ledger:
        from . import promises
        s = promises.score(ledger)
        cards["said"] = {k: s[k] for k in ("kept", "partly", "missed", "dropped", "open", "resolved", "too_few")}
        cards["said"]["credibility"] = None if s["too_few"] else round(s["credibility"] * 100)
    if not buys.empty:
        cards["insiders"] = {"insiders": int(buys["insider_cik"].nunique()), "total": float(buys["value"].sum()),
                             "last": str(pd.to_datetime(buys["tx_date"]).max().date())}
    if not fc.empty:
        years = fc.groupby(["accession", "filed"], as_index=False)["change_score"].mean().sort_values("filed")
        cards["filing"] = {"combined": int(round(years["change_score"].iloc[-1])), "filed": str(years["filed"].iloc[-1]),
                           "history": [int(round(v)) for v in years["change_score"].iloc[-6:]]}
        data["meta"] = f"Latest 10-K filed {years['filed'].iloc[-1]}"
    if not calls.empty:
        latest = calls.sort_values("public_date").iloc[-1]
        cards["record"] = {"calls": len(calls), "live": int((calls["mode"] == "live").sum()),
                           "latest_kind": latest["kind"], "latest_direction": int(latest["direction"]),
                           "latest_date": str(latest["public_date"])}
    data["cards"] = cards
    return data


def render(data: dict, key: str = "cockpit") -> tuple[str | None, str | None, str | None]:
    """Mount the cockpit. Returns (ticker switched to, page a card asked to open, market switched to); one-shot."""
    import streamlit as st
    global _mount
    if _mount is None:
        _mount = st.components.v2.component("finsight_cockpit", html="<div class='cockpit'></div>", css=CSS, js=JS)
    result = _mount(data=data, key=key, on_ticker_change=lambda: None, on_open_change=lambda: None,
                    on_market_change=lambda: None)
    return getattr(result, "ticker", None), getattr(result, "open", None), getattr(result, "market", None)


_mount = None

CSS = """
.cockpit { --ink:#EEF3F6; --muted:#9AABB6; --faint:#6E808C; --glass:rgba(24,34,42,.46); --edge:rgba(255,255,255,.11);
  --bull:#5CC9A7; --bear:#E06A78; --line:#F4F8FA;
  font-family: Manrope, system-ui, sans-serif; color: var(--ink); font-variant-numeric: tabular-nums; }
.cockpit button { all: unset; cursor: pointer; }
.cockpit button:focus-visible, .cockpit input:focus-visible { outline: 2px solid var(--line); outline-offset: 2px; }
.cockpit .glass { background: var(--glass); border: 1px solid var(--edge); backdrop-filter: blur(18px) saturate(120%);
  -webkit-backdrop-filter: blur(18px) saturate(120%); }
.markets { display: flex; gap: 4px; justify-content: center; margin: 0 auto 8px; padding: 4px; width: max-content;
  border-radius: 999px; background: var(--glass); border: 1px solid var(--edge); }
.markets button { padding: 5px 14px; border-radius: 999px; color: var(--muted); font-size: .82rem; }
.markets button.on { background: rgba(255,255,255,.14); color: var(--ink); cursor: default; }
.hero { text-align: center; }
.hero .meta { color: var(--muted); font-size: .8rem; min-height: 1em; }
.hero h1 { margin: 6px 0 4px; font-weight: 300; font-size: clamp(2.2rem, 5vw, 3.6rem); letter-spacing: -.035em; line-height: 1.05; }
.hero h1 .tk { color: var(--muted); font-weight: 200; }
.hero h1 .chev { font-size: .45em; vertical-align: .55em; color: var(--muted); margin-left: .25em; padding: 0 .2em; border-radius: 6px; }
.hero form { margin: 8px auto 0; }
.hero input { background: var(--glass); border: 1px solid var(--edge); color: var(--ink); border-radius: 999px; padding: 8px 14px;
  font: inherit; width: 200px; text-align: center; text-transform: uppercase; }
.price { display: inline-flex; gap: 12px; align-items: baseline; margin-top: 10px; flex-wrap: wrap; justify-content: center; }
.price .now { font-size: 1.35rem; }
.chip { font-size: .78rem; padding: 3px 9px; border-radius: 999px; background: rgba(255,255,255,.08); color: var(--muted); }
.chip.down { color: var(--bear); } .chip.up { color: var(--bull); }
.middle { display: grid; grid-template-columns: 140px 1fr 48px; align-items: center; min-height: 300px; margin-top: 6px; }
.rail { display: grid; gap: 13px; }
.rail button { display: grid; grid-template-columns: 22px auto; gap: 10px; align-items: center; color: var(--faint); font-size: .76rem; }
.rail button i { width: 22px; height: 22px; border-radius: 6px; display: grid; place-items: center; background: rgba(255,255,255,.06); font-style: normal; font-size: .62rem; }
.rail button.on { color: var(--ink); font-size: 1.1rem; font-weight: 300; cursor: default; }
.rail button.on i { background: rgba(255,255,255,.16); }
.rail button:hover:not(.on) { color: var(--ink); }
.chart { position: relative; height: 300px; }
.chart svg { width: 100%; height: 100%; overflow: visible; display: block; }
.chart .none { position: absolute; inset: 0; display: grid; place-items: center; color: var(--muted); }
.ctl { justify-self: end; display: grid; gap: 8px; }
.ctl button { width: 32px; height: 32px; border-radius: 9px; display: grid; place-items: center; color: var(--muted); }
.readout { position: absolute; pointer-events: none; transform: translate(-50%, -135%); font-size: .75rem; padding: 6px 9px; border-radius: 8px; white-space: nowrap; opacity: 0; transition: opacity .12s; }
.dial { position: relative; height: 64px; }
.dial svg { position: absolute; inset: 0; width: 100%; height: 100%; }
.dial .center { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); display: flex; gap: 18px; align-items: center; }
.dial button { width: 30px; height: 30px; border-radius: 50%; border: 1px solid var(--edge); display: grid; place-items: center; color: var(--muted); }
.dial .range { font-size: 1.05rem; font-weight: 300; min-width: 56px; text-align: center; }
.cards { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-top: 8px; }
.card { border-radius: 18px; padding: 14px 16px 16px; min-height: 150px; display: grid; grid-template-rows: auto 1fr; gap: 12px; }
.card header { display: grid; grid-template-columns: 30px 1fr 28px; gap: 10px; align-items: center; }
.card header i { width: 30px; height: 30px; border-radius: 8px; background: rgba(255,255,255,.08); display: grid; place-items: center; font-style: normal; font-size: .8rem; }
.card header b { font-weight: 500; font-size: .86rem; display: block; }
.card header span { color: var(--muted); font-size: .68rem; }
.card .go { width: 26px; height: 26px; border-radius: 50%; border: 1px solid var(--edge); display: grid; place-items: center; color: var(--muted); }
.big { font-size: 2.2rem; font-weight: 300; letter-spacing: -.04em; line-height: 1; }
.big small { font-size: .38em; color: var(--muted); letter-spacing: 0; margin-left: .35em; }
.sub { color: var(--muted); font-size: .72rem; margin-top: 6px; }
.split { display: flex; height: 6px; border-radius: 3px; overflow: hidden; margin-top: 12px; gap: 3px; }
.bars { display: flex; gap: 6px; align-items: flex-end; height: 40px; margin-top: 8px; }
.bars div { flex: 1; background: rgba(255,255,255,.22); border-radius: 2px; min-height: 2px; }
.bars div.hot { background: linear-gradient(180deg, rgba(244,248,250,.85), rgba(244,248,250,.25)); }
.ringrow { display: flex; gap: 14px; align-items: center; }
.empty { color: var(--muted); font-size: .85rem; }
.cockpit .cta { margin-top: 10px; display: inline-block; padding: 6px 12px; border-radius: 999px; border: 1px solid var(--edge); font-size: .78rem; color: var(--ink); }
.cockpit .cta:hover, .card .go:hover, .ctl button:hover, .dial button:hover { background: rgba(255,255,255,.12); }
@media (max-width: 1000px) { .cards { grid-template-columns: repeat(2, minmax(0, 1fr)); } .middle { grid-template-columns: 1fr; } .rail, .ctl { display: none; } }
@media (max-width: 560px) { .cards { grid-template-columns: 1fr; } }
@media (prefers-reduced-motion: reduce) { .cockpit * { transition: none !important; } }
"""

JS = r"""
export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const NS = "http://www.w3.org/2000/svg";
  const h = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text); return e; };
  const s = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  const money = v => v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`;
  const sign = v => (v > 0 ? "+" : "") + v.toFixed(1) + "%";
  const label = (b, text) => b.setAttribute("aria-label", text);
  const d = data || {}, c = d.cards || {};
  const open = page => setTriggerValue("open", page);

  let root = parentElement.querySelector(".cockpit");
  if (!root) { root = h("div", "cockpit"); parentElement.appendChild(root); }
  if (root._observer) root._observer.disconnect();
  root.replaceChildren();

  // Market switcher, then the hero: name, ticker switcher, price.
  const marketsRow = h("nav", "markets");
  label(marketsRow, "Market");
  [["US", "US"], ["IN", "India"], ["JP", "Japan"]].forEach(([code, text]) => {
    const b = h("button", code === d.market ? "on" : "", text);
    if (code !== d.market) b.onclick = () => setTriggerValue("market", code); else b.setAttribute("aria-current", "true");
    marketsRow.append(b);
  });
  const hero = h("section", "hero");
  hero.append(h("div", "meta", d.meta || " "));
  const title = h("h1"), sw = h("button", "chev", "⌄");
  label(sw, "Switch company");
  title.append(h("span", "nm", d.name), h("span", "tk", " " + d.ticker), sw);
  const form = h("form"), input = h("input");
  input.placeholder = "Ticker, e.g. MSFT"; input.maxLength = 10; label(input, "Ticker");
  form.hidden = true; form.append(input);
  sw.onclick = () => { form.hidden = !form.hidden; if (!form.hidden) input.focus(); };
  form.onsubmit = e => { e.preventDefault(); const v = input.value.trim().toUpperCase(); if (v) setTriggerValue("ticker", v); };
  const price = h("div", "price");
  if (d.last != null) {
    price.append(h("span", "now", (d.symbol || "$") + d.last.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })));
    if (d.chg_1d != null) price.append(h("span", "chip " + (d.chg_1d < 0 ? "down" : "up"), sign(d.chg_1d) + " today"));
    if (d.chg_1y != null) price.append(h("span", "chip " + (d.chg_1y < 0 ? "down" : "up"), sign(d.chg_1y) + " this year"));
  } else price.append(h("span", "chip", "No price data for this ticker"));
  hero.append(title, form, price);

  // Stage: view rail, the price line, range controls.
  const mid = h("section", "middle"), rail = h("nav", "rail");
  label(rail, "Views");
  [["⚖", "Said vs did", "said"], ["◎", "Insiders", "radar"], ["●", "Price", null], ["▤", "Filings", "filings"], ["↗", "Record", "record"]]
    .forEach(([icon, text, page]) => {
      const b = h("button", page ? "" : "on"); b.append(h("i", null, icon), document.createTextNode(text));
      if (page) b.onclick = () => open(page); else b.setAttribute("aria-current", "true");
      rail.append(b);
    });
  const chart = h("div", "chart"), svg = s("svg", { role: "img", "aria-label": `${d.ticker} price` }), readout = h("div", "readout glass");
  chart.append(svg, readout);
  const ctl = h("div", "ctl");
  const ctlButton = (text, aria) => { const b = h("button", "glass", text); label(b, aria); ctl.append(b); return b; };
  const zin = ctlButton("+", "Shorter range"), zout = ctlButton("−", "Longer range"), zreset = ctlButton("⟲", "Back to one year");
  mid.append(rail, chart, ctl);

  const dial = h("section", "dial"), dsvg = s("svg", { "aria-hidden": "true" }), center = h("div", "center");
  const prev = h("button", null, "‹"), rangeLabel = h("div", "range"), next = h("button", null, "›");
  label(prev, "Shorter range"); label(next, "Longer range");
  center.append(prev, rangeLabel, next); dial.append(dsvg, center);

  // Cards: one per tool, each with its empty state and a way in.
  const cards = h("section", "cards");
  const card = (icon, name, sub, page, fill) => {
    const a = h("article", "card glass"), head = h("header"), t = h("div"), go = h("button", "go", "›");
    t.append(h("b", null, name), h("span", null, sub)); label(go, "Open " + name); go.onclick = () => open(page);
    head.append(h("i", null, icon), t, go);
    const body = h("div"); fill(body); a.append(head, body); cards.append(a);
  };
  const empty = (body, text, action, page) => {
    body.append(h("div", "empty", text)); const b = h("button", "cta", action); b.onclick = () => open(page); body.append(b);
  };
  const tools = d.tools || [];
  const unavailable = (icon, name, text) => {
    const a = h("article", "card glass"), head = h("header"), t = h("div");
    t.append(h("b", null, name), h("span", null, "Not available in this market yet"));
    head.append(h("i", null, icon), t, h("span")); a.append(head, h("div", "empty", text)); cards.append(a);
  };
  if (!tools.includes("filings")) unavailable("⚖", "Said vs did", "Needs annual reports, which come with the India data feed (Phase 2).");
  else card("⚖", "Said vs did", "Management's promises, checked against later reports", "said", body => {
    if (!c.said) return empty(body, "Promises not checked yet.", "Check promises", "said");
    const big = h("div", "big", c.said.credibility == null ? "—" : c.said.credibility + "%");
    big.append(h("small", null, c.said.too_few ? "too few to judge" : "credibility"));
    const split = h("div", "split");
    [["kept", "var(--bull)"], ["partly", "rgba(244,248,250,.55)"], ["missed", "var(--bear)"], ["dropped", "rgba(255,255,255,.18)"]]
      .forEach(([k, color]) => { if (c.said[k]) { const b = h("div"); b.style.flex = c.said[k]; b.style.background = color; split.append(b); } });
    body.append(big, split, h("div", "sub", `Kept ${c.said.kept}, partly ${c.said.partly}, missed ${c.said.missed}, ` +
      `not mentioned again ${c.said.dropped}, still open ${c.said.open}`));
  });
  if (tools.includes("deals")) card("◎", "Big deals", "NSE bulk and block deals, last 90 days", "deals", body => {
    if (!c.deals) return empty(body, "No bulk or block deals in 90 days.", "Open the deals radar", "deals");
    const big = h("div", "big", (c.deals.net_crore > 0 ? "+" : "") + c.deals.net_crore); big.append(h("small", null, "₹ cr net"));
    body.append(big, h("div", "sub", `${c.deals.days} deal day${c.deals.days === 1 ? "" : "s"}, ${c.deals.signals} signal${c.deals.signals === 1 ? "" : "s"}, last on ${c.deals.last}`));
  });
  else if (!tools.includes("insider")) unavailable("◎", "Insider buying", "Japan doesn't publish officers' share trades as data. A large-shareholder radar is planned.");
  else card("◎", "Insider buying", "Open-market, last 90 days", "radar", body => {
    if (!c.insiders) return empty(body, "No insider buying recorded in 90 days.", "Scan the radar", "radar");
    const row = h("div", "ringrow"), ring = s("svg", { width: 58, height: 58, viewBox: "0 0 62 62", "aria-hidden": "true" });
    const frac = Math.min(c.insiders.insiders, 5) / 5;
    ring.append(s("circle", { cx: 31, cy: 31, r: 26, fill: "none", stroke: "rgba(255,255,255,.12)", "stroke-width": 5 }),
      s("circle", { cx: 31, cy: 31, r: 26, fill: "none", stroke: "var(--bull)", "stroke-width": 5, "stroke-linecap": "round",
        "stroke-dasharray": 163, "stroke-dashoffset": 163 * (1 - frac), transform: "rotate(-90 31 31)" }));
    const txt = h("div"), big = h("div", "big", c.insiders.insiders);
    big.append(h("small", null, c.insiders.insiders === 1 ? "insider" : "insiders"));
    txt.append(big, h("div", "sub", `${money(c.insiders.total)} bought, last on ${c.insiders.last}`));
    row.append(ring, txt); body.append(row);
  });
  if (!tools.includes("filings")) unavailable("▤", "Filing changes", "Needs annual reports, which come with the India data feed (Phase 2).");
  else card("▤", d.market === "JP" ? "Annual report change" : "10-K change", c.filing ? "10-K filed " + c.filing.filed + " vs the year before" : "Risk Factors and MD&A vs last year", "filings", body => {
    if (!c.filing) return empty(body, "Not scored yet.", "Score it", "filings");
    const big = h("div", "big", c.filing.combined); big.append(h("small", null, "/ 100"));
    const bars = h("div", "bars"), top = Math.max(...c.filing.history, 1);
    c.filing.history.forEach((v, i) => { const b = h("div", i === c.filing.history.length - 1 ? "hot" : ""); b.style.height = (100 * v / top) + "%"; bars.append(b); });
    body.append(big, bars);
  });
  const kinds = { debate: "debate verdict", insider_cluster: "insider cluster", filing_change: "filing change" };
  card("↗", "Track record", "Calls on " + d.ticker, "record", body => {
    if (!c.record) return empty(body, "No graded calls yet.", "See the record", "record");
    const big = h("div", "big", c.record.calls); big.append(h("small", null, c.record.calls === 1 ? "call" : "calls"));
    const dir = c.record.latest_direction > 0 ? "bullish" : c.record.latest_direction < 0 ? "bearish" : "neutral";
    body.append(big, h("div", "sub", `Latest: ${dir} ${kinds[c.record.latest_kind] || c.record.latest_kind}, ${c.record.latest_date}`),
      h("div", "sub", `${c.record.live} live, ${c.record.calls - c.record.live} from backtests`));
  });

  root.append(marketsRow, hero, mid, dial, cards);

  // The price line, drawn for the chosen range.
  const RANGES = [["1M", (d.daily_1y || []).slice(-21), "day"], ["3M", (d.daily_1y || []).slice(-63), "day"],
                  ["1Y", d.daily_1y || [], "day"], ["5Y", d.weekly_5y || [], "week"]];
  let idx = 2, animate = true;
  function draw() {
    const [name, ys, unit] = RANGES[idx];
    rangeLabel.textContent = name;
    svg.replaceChildren(); chart.querySelector(".none")?.remove();
    drawDial();
    const W = chart.clientWidth, H = chart.clientHeight, pad = 24;
    if (ys.length < 2 || !W) { if (ys.length < 2) chart.append(h("div", "none", "No price history")); return; }
    const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const x = i => pad + i * (W - 2 * pad) / (ys.length - 1), y = v => pad + (1 - (v - lo) / span) * (H * .72 - pad);
    const pts = ys.map((v, i) => [x(i), y(v)]), line = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join("");
    const defs = s("defs", {});
    const fill = s("linearGradient", { id: "fsfill", x1: 0, y1: 0, x2: 0, y2: 1 });
    fill.append(s("stop", { offset: 0, "stop-color": "#F4F8FA", "stop-opacity": .22 }), s("stop", { offset: 1, "stop-color": "#F4F8FA", "stop-opacity": 0 }));
    const glow = s("filter", { id: "fsglow", x: "-10%", y: "-50%", width: "120%", height: "200%" });
    glow.append(s("feGaussianBlur", { stdDeviation: 5, result: "b" }));
    const merge = s("feMerge", {}); merge.append(s("feMergeNode", { in: "b" }), s("feMergeNode", { in: "SourceGraphic" })); glow.append(merge);
    const floor = s("radialGradient", { id: "fsfloor", cx: .5, cy: .5, r: .5 });
    floor.append(s("stop", { offset: 0, "stop-color": "#B9D0DE", "stop-opacity": .22 }), s("stop", { offset: 1, "stop-color": "#B9D0DE", "stop-opacity": 0 }));
    defs.append(fill, glow, floor); svg.append(defs);
    svg.append(s("ellipse", { cx: W / 2, cy: H * .9, rx: W * .42, ry: 24, fill: "url(#fsfloor)" }),
               s("path", { d: line + `L${x(ys.length - 1)} ${H * .9}L${x(0)} ${H * .9}Z`, fill: "url(#fsfill)" }));
    const path = s("path", { d: line, fill: "none", stroke: "#F4F8FA", "stroke-width": 2.2, "stroke-linejoin": "round", filter: "url(#fsglow)" });
    svg.append(path);
    if (animate && !matchMedia("(prefers-reduced-motion: reduce)").matches) {
      const L = path.getTotalLength(); path.style.strokeDasharray = L; path.style.strokeDashoffset = L; path.getBoundingClientRect();
      path.style.transition = "stroke-dashoffset 1.4s cubic-bezier(.3,.7,.2,1)"; path.style.strokeDashoffset = 0;
    }
    animate = false;
    const last = pts[pts.length - 1];
    svg.append(s("circle", { cx: last[0], cy: last[1], r: 4.5, fill: "#F4F8FA" }));
    const dot = s("circle", { r: 5, fill: "none", stroke: "#F4F8FA", "stroke-width": 1.5, opacity: 0 });
    const guide = s("line", { stroke: "rgba(255,255,255,.25)", "stroke-dasharray": "2 4", opacity: 0 });
    svg.append(guide, dot);
    chart.onmousemove = e => {
      const r = chart.getBoundingClientRect();
      const i = Math.max(0, Math.min(ys.length - 1, Math.round((e.clientX - r.left - pad) / ((W - 2 * pad) / (ys.length - 1)))));
      const [px, py] = pts[i], ago = ys.length - 1 - i;
      for (const [k, v] of [["cx", px], ["cy", py], ["opacity", 1]]) dot.setAttribute(k, v);
      for (const [k, v] of [["x1", px], ["x2", px], ["y1", py], ["y2", H * .9], ["opacity", 1]]) guide.setAttribute(k, v);
      readout.style.left = px + "px"; readout.style.top = py + "px"; readout.style.opacity = 1;
      readout.textContent = `$${ys[i].toFixed(2)}, ${ago ? ago + " " + unit + (ago > 1 ? "s" : "") + " ago" : "latest"}`;
    };
    chart.onmouseleave = () => { dot.setAttribute("opacity", 0); guide.setAttribute("opacity", 0); readout.style.opacity = 0; };
  }
  function drawDial() {
    const W = dial.clientWidth, H = dial.clientHeight, n = 140, active = (idx + 1) / RANGES.length;
    dsvg.replaceChildren();
    if (!W) return;
    for (let i = 0; i <= n; i++) {
      const t = i / n;
      if (Math.abs(t - .5) < Math.max(.14, 95 / W)) continue;  // keep the range controls clear on narrow screens
      const px = W * .06 + t * W * .88, py = H * .5 - 9 + Math.pow((t - .5) * 2, 2) * 15;
      dsvg.append(s("line", { x1: px, x2: px, y1: py, y2: py + (i % 5 ? 7 : 12), "stroke-width": 1,
        stroke: Math.abs(t - .5) < .5 * active ? "rgba(244,248,250,.75)" : "rgba(255,255,255,.18)" }));
    }
  }
  const step = dlt => { const n = Math.max(0, Math.min(RANGES.length - 1, idx + dlt)); if (n !== idx) { idx = n; animate = true; draw(); } };
  prev.onclick = zin.onclick = () => step(-1);
  next.onclick = zout.onclick = () => step(1);
  zreset.onclick = () => { idx = 2; animate = true; draw(); };
  root._observer = new ResizeObserver(() => draw());
  root._observer.observe(chart);
  draw();
}
"""
