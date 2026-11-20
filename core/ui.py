"""Shared look: page header, CSS, and the debate's verdict bar. Colours match .streamlit/config.toml."""
from html import escape

import streamlit as st

BULL, BEAR, INK, MUTED, RULE = "#17705A", "#B0303F", "#17212B", "#56656D", "#D3DAD6"

CSS = f"""
<style>
html, body, [class*="css"] {{ font-variant-numeric: tabular-nums; }}
.block-container {{ max-width: 1180px; padding-top: 4.5rem; }}
h1, h2, h3 {{ letter-spacing: -0.02em; }}
.fs-head {{ border-bottom: 2px solid {INK}; padding-bottom: .9rem; margin-bottom: 1.4rem; }}
.fs-head h1 {{ margin: 0; padding: 0; line-height: 1.05; }}
.fs-head p {{ color: {MUTED}; max-width: 68ch; margin: .5rem 0 0; font-size: 1.02rem; }}
.fs-hero h1 {{ font-size: clamp(3rem, 8vw, 5.5rem); font-weight: 900; letter-spacing: -0.045em; padding-top: .15em; line-height: 1; }}
[data-testid="stPageLink"] a {{ border: 1.5px solid #2340C4; border-radius: 4px; padding: .45rem .9rem; justify-content: center; }}
[data-testid="stPageLink"] a span, [data-testid="stPageLink"] a p {{ color: #2340C4; font-weight: 650; }}
[data-testid="stPageLink"] a:hover {{ background: #2340C4; }}
[data-testid="stPageLink"] a:hover span, [data-testid="stPageLink"] a:hover p {{ color: #fff; }}
[data-testid="stPageLink"] a:focus-visible {{ outline: 3px solid #2340C4; outline-offset: 2px; }}
.fs-muted {{ color: {MUTED}; }}
.fs-tool-name {{ font-size: 1.45rem; font-weight: 800; letter-spacing: -0.02em; margin: 0; }}
.fs-tool-reads {{ color: {MUTED}; font-size: .9rem; margin-top: .35rem; }}
.fs-status {{ display: grid; grid-template-columns: 1rem 11rem 1fr; gap: .55rem .75rem; align-items: baseline; }}
.fs-dot {{ width: .6rem; height: .6rem; border-radius: 50%; display: inline-block; }}
.fs-status b {{ font-weight: 650; }}
.fs-status span.why {{ color: {MUTED}; font-size: .92rem; overflow-wrap: anywhere; }}

/* Debate turns: bull hugs the left, bear is indented, each with its own edge. */
[class*="st-key-turn-bull"], [class*="st-key-turn-bear"] {{ padding: .2rem 0 .4rem 1.1rem; max-width: 78ch; }}
[class*="st-key-turn-bull"] {{ border-left: 4px solid {BULL}; }}
[class*="st-key-turn-bear"] {{ border-left: 4px solid {BEAR}; margin-left: min(12%, 6rem); }}
.fs-speaker {{ font-weight: 800; font-size: .95rem; margin-bottom: -.3rem; }}

/* Verdict: a tug-of-war bar split by score. */
.fs-bar {{ display: flex; height: 2.6rem; border-radius: 4px; overflow: hidden; margin: .6rem 0 .3rem; }}
.fs-bar div {{ display: flex; align-items: center; padding: 0 .9rem; color: #fff; font-weight: 800; font-size: 1.1rem; }}
.fs-bar .bear {{ justify-content: flex-end; }}
.fs-bar-legend {{ display: flex; justify-content: space-between; color: {MUTED}; font-size: .9rem; }}
.fs-winner {{ font-size: 1.6rem; font-weight: 900; letter-spacing: -0.02em; margin: 1rem 0 .3rem; }}
.fs-point {{ border-left: 3px solid; padding: .1rem 0 .1rem .9rem; margin: .4rem 0 1rem; }}
.fs-point h4 {{ margin: 0 0 .25rem; padding: 0; font-size: .95rem; }}
.fs-point p, .fs-point li {{ margin: 0; font-size: .97rem; }}

@media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
"""


def style() -> None:
    st.html(CSS)


def header(title: str, blurb: str, hero: bool = False) -> None:
    st.html(f'<div class="fs-head{" fs-hero" if hero else ""}"><h1>{escape(title)}</h1><p>{escape(blurb)}</p></div>')


def status(rows: list[tuple[str, bool, str]]) -> None:
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
