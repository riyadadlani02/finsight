"""What a company changed in its 10-K since last year.

Scores are plain text similarity, so anyone can reproduce them; Claude only
explains paragraphs this module has already classified.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from html import escape
from typing import Literal

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

UNCHANGED, REWORDED = 0.9, 0.5  # paragraph match thresholds (design doc)
Kind = Literal["added", "removed", "reworded", "unchanged"]


@dataclass
class Para:
    id: str
    kind: Kind
    new: str | None
    old: str | None
    match: float


@dataclass
class SectionDiff:
    similarity: float
    change_score: int
    paras: list[Para] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {k: sum(p.kind == k for p in self.paras) for k in ("added", "removed", "reworded", "unchanged")}


def split_paragraphs(text: str, min_chars: int = 80) -> list[str]:
    """Blank-line separated blocks, whitespace collapsed; short ones (headings, page furniture) dropped."""
    parts = (re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text))
    return [p for p in parts if len(p) >= min_chars]


def _cosine(docs: list[str]):
    return cosine_similarity(TfidfVectorizer(stop_words="english").fit_transform(docs))


def diff_sections(old: str, new: str) -> SectionDiff:
    try:
        similarity = float(_cosine([old, new])[0, 1])
    except ValueError:  # one side has no words
        similarity = 0.0
    similarity = min(max(similarity, 0.0), 1.0)
    old_p, new_p = split_paragraphs(old), split_paragraphs(new)

    # Greedy best-first matching: the most similar pairs claim each other first, each paragraph once.
    matched: dict[int, tuple[int, float]] = {}
    if old_p and new_p:
        sims = _cosine(old_p + new_p)[len(old_p):, :len(old_p)]
        used_old: set[int] = set()
        pairs = sorted(((sims[n, o], n, o) for n in range(len(new_p)) for o in range(len(old_p))), reverse=True)
        for score, n, o in pairs:
            if score < REWORDED:
                break
            if n not in matched and o not in used_old:
                matched[n] = (o, float(score))
                used_old.add(o)

    paras = []
    for n, text in enumerate(new_p):
        if n in matched:
            o, score = matched[n]
            paras.append(Para(f"p{n + 1}", "unchanged" if score >= UNCHANGED else "reworded", text, old_p[o], score))
        else:
            paras.append(Para(f"p{n + 1}", "added", text, None, 0.0))
    used = {o for o, _ in matched.values()}
    removed = [t for o, t in enumerate(old_p) if o not in used]
    paras += [Para(f"r{i + 1}", "removed", None, t, 0.0) for i, t in enumerate(removed)]
    return SectionDiff(similarity, round((1 - similarity) * 100), paras)


def redline_html(old: str, new: str) -> str:
    """Word-level diff with <del>/<ins>; every word is escaped."""
    a, b = old.split(), new.split()
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if op == "equal":
            out.append(escape(" ".join(a[i1:i2])))
        if op in ("delete", "replace"):
            out.append(f"<del>{escape(' '.join(a[i1:i2]))}</del>")
        if op in ("insert", "replace"):
            out.append(f"<ins>{escape(' '.join(b[j1:j2]))}</ins>")
    return " ".join(out)
