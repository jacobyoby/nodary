"""Calibration edge cases: corpora with no malicious examples and with no
examples at all must render without crashing."""

from __future__ import annotations

from nodary.calibration import render_markdown, run_calibration
from nodary.calibration.harness import SyntheticMailbox


class _AllBenignSource:
    name = "all-benign fixture"

    def replay(self, conn):
        box = SyntheticMailbox(conn)
        box.establish_contact("dana@acme-corp.com", display="Dana Ito", n=9)
        conn.commit()
        return box.labels


class _EmptySource:
    name = "empty fixture"

    def replay(self, conn):
        return []


def test_all_benign_corpus_reports_zero_false_alarms():
    result = run_calibration(_AllBenignSource())
    assert result.examples
    assert result.headline.false_positives == 0
    assert result.headline.malicious_total == 0
    assert result.mean_score("malicious") == 0.0
    report = render_markdown(result)
    assert "## Threshold Sweep" in report


def test_empty_corpus_renders_without_crash():
    result = run_calibration(_EmptySource())
    assert result.examples == ()
    report = render_markdown(result)
    assert "# Nodary Weight Calibration" in report
    assert "Labeled incoming messages: 0" in report
