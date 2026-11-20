"""Shared look: the cockpit stage, glass panels, page header and the debate's verdict bar.

Colours match .streamlit/config.toml and the design doc's "Look" section.
"""
from html import escape

import streamlit as st

BULL, BEAR, LINE, INK, MUTED = "#5CC9A7", "#E06A78", "#F4F8FA", "#EEF3F6", "#9AABB6"
AMBER = "#E2B867"
GLASS = "rgba(24, 34, 42, .46)"
EDGE = "rgba(255, 255, 255, .11)"

_GLASS = f"background: {GLASS}; border: 1px solid {EDGE}; border-radius: 18px; " \
         "backdrop-filter: blur(18px) saturate(120%); -webkit-backdrop-filter: blur(18px) saturate(120%);"

CSS = f"""
<style>
html, body, .stApp {{ font-family: Manrope, system-ui, sans-serif; font-variant-numeric: tabular-nums; }}
/* The stage: slate-blue, lit from above, with faint light shafts and a floor glow. */
.stApp {{
  background:
    radial-gradient(ellipse 60% 45% at 50% -8%, rgba(255,255,255,.16), transparent 70%),
    radial-gradient(ellipse 70% 20% at 50% 100%, rgba(150,180,200,.10), transparent 70%),
    linear-gradient(180deg, #4A6474 0%, #2D404C 45%, #1B262D 100%) fixed;
}}
.stApp::before {{
  content: ""; position: fixed; inset: -10% 10% 40% 10%; pointer-events: none; z-index: 0;
  background: repeating-linear-gradient(100deg, transparent 0 90px, rgba(255,255,255,.03) 90px 150px, transparent 150px 260px);
  filter: blur(14px); mask-image: linear-gradient(180deg, #000 0%, transparent 85%);
}}
[data-testid="stHeader"], [data-testid="stAppViewContainer"], [data-testid="stMain"] {{ background: transparent; }}
[data-testid="stHeader"] {{ {_GLASS} border-radius: 0 0 18px 18px; }}
.block-container {{ max-width: 1240px; padding-top: 5rem; position: relative; z-index: 1; }}
h1, h2, h3 {{ font-weight: 300 !important; letter-spacing: -0.03em; color: {INK}; }}

/* Glass surfaces */
[class*="st-key-panel-"], [data-testid="stExpander"] details, [data-testid="stForm"] {{ {_GLASS} padding: 1rem 1.2rem; }}
[data-testid="stExpander"] details {{ padding: 0; }}
[data-testid="stDataFrame"] {{ border-radius: 12px; overflow: hidden; }}
[data-testid="stSidebar"] {{ background: rgba(20, 29, 36, .72); backdrop-filter: blur(18px); }}

/* Page header */
.fs-head {{ text-align: center; margin: .5rem 0 1.6rem; }}
.fs-head h1 {{ margin: 0; padding: 0; font-size: clamp(2.2rem, 4.5vw, 3.2rem); line-height: 1.05; letter-spacing: -0.035em; }}
.fs-head p {{ color: {MUTED}; max-width: 64ch; margin: .6rem auto 0; font-size: 1rem; }}
.fs-hero h1 {{ font-size: clamp(3rem, 8vw, 5.2rem); font-weight: 200 !important; letter-spacing: -0.05em; }}
.fs-muted {{ color: {MUTED}; }}
.fs-tool-name {{ font-size: 1.4rem; font-weight: 300; letter-spacing: -0.02em; margin: 0; color: {INK}; }}
.fs-tool-reads {{ color: {MUTED}; font-size: .88rem; margin-top: .35rem; }}
.fs-status {{ display: grid; grid-template-columns: 1rem 11rem 1fr; gap: .55rem .75rem; align-items: baseline; }}
.fs-dot {{ width: .6rem; height: .6rem; border-radius: 50%; display: inline-block; }}
.fs-status b {{ font-weight: 600; color: {INK}; }}
.fs-status span.why {{ color: {MUTED}; font-size: .92rem; overflow-wrap: anywhere; }}

/* primaryColor is near-white, so primary buttons carry dark text. */
[data-testid^="stBaseButton-primary"], [data-testid^="stBaseButton-primary"] p {{ color: #10181D !important; font-weight: 600; }}

/* Links that act as buttons: small glass pills. */
[data-testid="stPageLink"] a {{ {_GLASS} border-radius: 999px; padding: .45rem 1rem; justify-content: center; }}
[data-testid="stPageLink"] a span, [data-testid="stPageLink"] a p {{ color: {INK}; font-weight: 500; }}
[data-testid="stPageLink"] a:hover {{ background: rgba(255,255,255,.14); }}
[data-testid="stPageLink"] a:focus-visible, button:focus-visible {{ outline: 2px solid {LINE}; outline-offset: 2px; }}

/* Debate turns: bull hugs the left, bear is indented, each with its own edge. */
[class*="st-key-turn-bull"], [class*="st-key-turn-bear"] {{ {_GLASS} padding: .7rem 1.1rem .5rem; max-width: 80ch; border-radius: 14px; }}
[class*="st-key-turn-bull"] {{ border-left: 3px solid {BULL}; }}
[class*="st-key-turn-bear"] {{ border-left: 3px solid {BEAR}; margin-left: min(12%, 6rem); }}
.fs-speaker {{ font-weight: 600; font-size: .9rem; margin-bottom: -.3rem; }}

/* Verdict: a tug-of-war bar split by score. */
.fs-bar {{ display: flex; height: 2.4rem; border-radius: 999px; overflow: hidden; margin: .6rem 0 .3rem; gap: 3px; }}
.fs-bar div {{ display: flex; align-items: center; padding: 0 1rem; color: #10181D; font-weight: 700; font-size: 1.05rem; }}
.fs-bar .bear {{ justify-content: flex-end; }}
.fs-bar-legend {{ display: flex; justify-content: space-between; color: {MUTED}; font-size: .88rem; }}
.fs-winner {{ font-size: 2rem; font-weight: 200; letter-spacing: -0.03em; margin: 1rem 0 .3rem; color: {INK}; }}
.fs-point {{ border-left: 3px solid; padding: .1rem 0 .1rem .9rem; margin: .4rem 0 1rem; }}
.fs-point h4 {{ margin: 0 0 .25rem; padding: 0; font-size: .95rem; font-weight: 600 !important; color: {INK}; }}
.fs-point p, .fs-point li {{ margin: 0; font-size: .97rem; color: {INK}; }}

@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
"""


def style() -> None:
    st.html(CSS)


def panel(key: str):
    """A glass panel; use as `with ui.panel("name"):`."""
    return st.container(key=f"panel-{key}")


def header(title: str, blurb: str, hero: bool = False) -> None:
    st.html(f'<div class="fs-head{" fs-hero" if hero else ""}"><h1>{escape(title)}</h1><p>{escape(blurb)}</p></div>')


def status(rows: list[tuple[str, bool | None, str]]) -> None:
    """rows: (name, ok, explanation). ok=None means optional and absent: grey, not red."""
    cells = "".join(
        f'<span class="fs-dot" style="background:{BULL if ok else MUTED if ok is None else BEAR}"></span>'
        f'<b>{escape(name)}</b><span class="why">{escape(why)}</span>'
        for name, ok, why in rows)
    st.html(f'<div class="fs-status">{cells}</div>')


def verdict_bar(winner: str, bull: int, bear: int) -> None:
    total = max(bull + bear, 1)
    title = {"bull": "The bull wins", "bear": "The bear wins", "draw": "A draw"}[winner]
    st.html(
        f'<div class="fs-winner">{title}</div>'
        f'<div class="fs-bar" role="img" aria-label="Bull {bull}, bear {bear}">'
        f'<div style="background:{BULL};width:{bull / total:.1%}">{bull}</div>'
        f'<div class="bear" style="background:{BEAR};width:{bear / total:.1%}">{bear}</div></div>'
        '<div class="fs-bar-legend"><span>Bull score</span><span>Bear score</span></div>')


def point(title: str, body: str | list[str], color: str = INK) -> None:
    text = ("<ul>" + "".join(f"<li>{escape(b)}</li>" for b in body) + "</ul>") if isinstance(body, list) \
        else f"<p>{escape(body)}</p>"
    st.html(f'<div class="fs-point" style="border-color:{color}"><h4>{escape(title)}</h4>{text}</div>')
