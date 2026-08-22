"""Match journal titles and author affiliations against curated name lists.

Journal titles arrive in wildly different shapes: ``Molecular & cellular
proteomics : MCP`` from PubMed, ``Molecular and Cellular Proteomics`` from a
hand-written list, ``The Journal of biological chemistry`` with a leading
article.  Mapping punctuation to spaces is not enough — ``&`` leaves a gap
where the list entry writes ``and``, so the two sides land one token apart
and never match.  Dropping ``the`` and ``and`` from both sides closes that
gap without loosening anything else.

``of`` is deliberately kept: it appears on both sides of every ``Journal of
X`` title, so dropping it would only widen the false-positive surface.
"""

import re

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]+")
# Dropped from both sides, so this can only widen a match, never create a
# cross-boundary one.
_DROPPED = frozenset({"the", "and"})


def normalize(text: str) -> str:
    """Lowercase *text*, drop punctuation and the articles that vary by source."""
    tokens = _NON_ALNUM_RE.sub(" ", (text or "").lower()).split()
    return " ".join(t for t in tokens if t not in _DROPPED)


def match_name(text: str, names: list[str]) -> str | None:
    """Return the first entry of *names* occurring as a whole word sequence."""
    haystack = f" {normalize(text)} "
    if haystack == "  ":
        return None
    for name in names or []:
        needle = normalize(name)
        if needle and f" {needle} " in haystack:
            return name
    return None


def match_journal(journal: str | None, names: list[str]) -> str | None:
    """Return the curated journal name this *journal* string matches."""
    return match_name(journal or "", names)


def match_industry(
    institutions: list[str],
    company_institutions: list[str],
    names: list[str],
) -> str | None:
    """Return the company behind a paper, or None when it looks academic.

    Two independent signals: a name the operator curated, or an institution
    the retrieval source itself flagged as a company.  The curated list wins
    so the badge shows the name the operator recognises.
    """
    for institution in institutions or []:
        hit = match_name(institution, names)
        if hit:
            return hit
    return next(iter(company_institutions or []), None)
