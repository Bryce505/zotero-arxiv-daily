"""Week anchoring for the weekly digest.

A digest week is named after the Friday it closes on: the month that Friday
falls in, plus which Friday of that month it is.  ``2026-08-21`` is the third
Friday of August 2026, so its label is ``2026-08-W3`` and it covers
``2026-08-15`` through ``2026-08-21`` inclusive.
"""

from datetime import date, timedelta

FRIDAY = 4  # date.weekday(): Monday is 0


def anchor_friday(d: date) -> date:
    """Return the most recent Friday on or before *d*."""
    return d - timedelta(days=(d.weekday() - FRIDAY) % 7)


def week_label(d: date) -> str:
    """Return the ``YYYY-MM-WN`` label for the week *d* falls in."""
    friday = anchor_friday(d)
    ordinal = (friday.day - 1) // 7 + 1
    return f"{friday.year}-{friday.month:02d}-W{ordinal}"


def week_window(d: date) -> tuple[date, date]:
    """Return the inclusive ``(start, end)`` dates covered by *d*'s week.

    The window reaches back to the *previous* Friday, so consecutive windows
    overlap by a day rather than merely abutting: the digest runs at midday
    UTC, and anything indexed later that same Friday would otherwise fall
    into no window at all.  Cross-week de-duplication makes the overlap free.
    """
    friday = anchor_friday(d)
    return friday - timedelta(days=7), friday


def report_paths(d: date) -> tuple[str, str]:
    """Return the ``(markdown, html)`` repository paths for *d*'s digest."""
    friday = anchor_friday(d)
    label = week_label(d)
    return (
        f"reports/{friday.year}/{label}.md",
        f"reports/{friday.year}/{label}.html",
    )


def library_dir(d: date) -> str:
    """Return the repository directory holding *d*'s downloaded PDFs."""
    friday = anchor_friday(d)
    return f"library/{friday.year}/{week_label(d)}"
