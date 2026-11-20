"""Offline tests for 10-K change scoring: no network, no API key."""
from core import changes

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
