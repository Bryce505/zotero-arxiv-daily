"""Tests for week anchoring and labelling."""

from datetime import date

from zotero_arxiv_daily.weeknum import (
    anchor_friday,
    library_dir,
    report_paths,
    week_label,
    week_window,
)


def test_anchor_on_a_friday_returns_that_friday():
    assert anchor_friday(date(2026, 8, 21)) == date(2026, 8, 21)


def test_anchor_on_a_saturday_returns_previous_day():
    assert anchor_friday(date(2026, 8, 22)) == date(2026, 8, 21)


def test_anchor_on_a_thursday_walks_back_six_days():
    assert anchor_friday(date(2026, 8, 20)) == date(2026, 8, 14)


def test_label_counts_fridays_within_the_month():
    assert week_label(date(2026, 8, 7)) == "2026-08-W1"
    assert week_label(date(2026, 8, 14)) == "2026-08-W2"
    assert week_label(date(2026, 8, 21)) == "2026-08-W3"
    assert week_label(date(2026, 8, 28)) == "2026-08-W4"


def test_label_uses_the_month_the_friday_falls_in():
    # 2026-10-02 is a Friday: the week spans September but the label is October W1.
    assert week_label(date(2026, 10, 2)) == "2026-10-W1"


def test_window_is_the_seven_days_ending_on_the_friday():
    assert week_window(date(2026, 8, 21)) == (date(2026, 8, 15), date(2026, 8, 21))


def test_window_may_cross_a_month_boundary():
    assert week_window(date(2026, 10, 2)) == (date(2026, 9, 26), date(2026, 10, 2))


def test_report_paths_are_year_foldered():
    md, html = report_paths(date(2026, 8, 21))
    assert md == "reports/2026/2026-08-W3.md"
    assert html == "reports/2026/2026-08-W3.html"


def test_library_dir_is_year_foldered():
    assert library_dir(date(2026, 8, 21)) == "library/2026/2026-08-W3"
