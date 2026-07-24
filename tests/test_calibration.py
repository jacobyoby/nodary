from __future__ import annotations

from nodary.calibration import render_markdown, run_calibration


def test_synthetic_calibration_report_is_deterministic(monkeypatch, tmp_path):
    monkeypatch.setenv("TLDEXTRACT_CACHE", str(tmp_path / "tldextract"))

    first = run_calibration()
    report = render_markdown(first)
    second_report = render_markdown(run_calibration())

    assert report == second_report
    assert "# Nodary Weight Calibration" in report
    assert "## Score Distribution" in report
    assert "## Feature Firing" in report
    assert "## Threshold Sweep" in report
    assert first.mean_score("malicious") > first.mean_score("benign")
    assert first.headline.threshold == 10.0
    assert first.headline.true_positives == first.headline.malicious_total
