"""Top up a thin week with established, highly-cited work.

A week that yields fewer than the configured minimum is padded from OpenAlex
by citation count rather than by loosening the relevance bar — the digest
would rather show a known classic than a weak new match.  Backfilled papers
are tagged so the report can list them separately (spec 8.5).

Retrieval runs in up to ``_MAX_ROUNDS`` rounds.  The first uses each theme's
own distilled query; if that comes back short, *requery* is asked for a
differently-worded query per theme and the search runs again.  Repeating the
identical query would return the identical papers, so a round that finds
nothing new has to change the wording — the same reason a systematic review
re-angles a search that under-covers a topic instead of re-running it.
"""

from loguru import logger

from .dedup import dedup_papers, normalize_doi, title_key
from .protocol import Paper
from .search.profile import QueryProfile

_OVERSAMPLE = 3
_MAX_ROUNDS = 3


def _fetch_round(profiles, retriever, queries: dict[str, str], per_cluster: int) -> list[Paper]:
    """Search every theme that still has a query, tagging what comes back."""
    pool: list[Paper] = []
    for profile in profiles:
        query = queries.get(profile.cluster)
        if not query:
            continue
        found = retriever.search_highly_cited(query, per_cluster)
        for paper in found:
            paper.cluster = profile.cluster
            paper.is_backfill = True
        pool.extend(found)
    return pool


def backfill_papers(
    profiles: list[QueryProfile],
    retriever,
    needed: int,
    exclude_dois: set[str],
    gate=None,
    requery=None,
    max_rounds: int = _MAX_ROUNDS,
) -> list[Paper]:
    """Return up to *needed* highly-cited papers across *profiles*.

    *gate* filters the oversampled pool before the citation sort.  Sorting by
    citations without it is how a 2005 virology paper reached a CMC digest:
    highly cited is not the same as relevant.

    *requery* — ``(profiles, tried) -> {cluster: query}`` — supplies the next
    round's queries when a round comes back short.  ``tried`` maps each theme
    to the queries already spent on it, so the callback can ask for wording
    that has not been used yet.  Omitted (or returning nothing), the search
    is a single round, exactly as it was before rounds existed.
    """
    if needed <= 0 or not profiles:
        return []

    per_cluster = max(1, needed * _OVERSAMPLE // len(profiles))
    queries = {p.cluster: p.plain_query for p in profiles}
    tried: dict[str, list[str]] = {p.cluster: [] for p in profiles}

    chosen: list[Paper] = []
    # Carries across rounds, and deliberately records rejects too: re-gating a
    # paper an earlier round already judged is a wasted LLM call, and a second
    # verdict that disagrees with the first would read as a flake.
    seen_dois = set(exclude_dois)
    seen_titles: set[str] = set()
    fetched = after_dedup = gated = 0

    for round_no in range(1, max(1, int(max_rounds)) + 1):
        for cluster, query in queries.items():
            if query:
                tried.setdefault(cluster, []).append(query)

        pool = _fetch_round(profiles, retriever, queries, per_cluster)
        fetched += len(pool)
        pool = [
            p for p in pool
            if (normalize_doi(p.doi) or "") not in seen_dois and title_key(p.title) not in seen_titles
        ]
        pool = dedup_papers(pool)
        after_dedup += len(pool)
        for paper in pool:
            doi = normalize_doi(paper.doi)
            if doi:
                seen_dois.add(doi)
            seen_titles.add(title_key(paper.title))

        # An empty pool is not worth a gate call: every gate implementation
        # short-circuits on it anyway, and calling it would make a round that
        # found nothing look, in a spy's records, like a round that judged
        # nothing worth keeping.
        if pool and gate is not None:
            pool = gate(pool)
        gated += len(pool)
        pool.sort(key=lambda p: p.cited_by_count or 0, reverse=True)
        chosen.extend(pool[: needed - len(chosen)])
        logger.info(
            f"Backfill round {round_no}/{max_rounds}: {len(chosen)}/{needed} collected"
        )

        if len(chosen) >= needed or requery is None or round_no >= max_rounds:
            break
        try:
            # A copy, not the live dict: the callback may hold on to what it
            # was handed, and later rounds appending to these lists would
            # rewrite the history it thought it had recorded.
            snapshot = {cluster: list(used) for cluster, used in tried.items()}
            queries = {k: str(v) for k, v in (requery(profiles, snapshot) or {}).items() if v}
        except Exception as exc:  # noqa: BLE001 - a retry must never cost the digest
            logger.warning(f"Backfill requery failed ({exc}); stopping after round {round_no}")
            break
        if not queries:
            logger.info(f"No fresh backfill queries after round {round_no}; stopping")
            break

    if len(chosen) < needed:
        # Silent under-fill is exactly how a thin week ships with no classic
        # top-up and no clue why: this breaks down where the shortfall came
        # from — OpenAlex came back short, exclude/dedup ate the pool, or the
        # relevance gate rejected it — so it is diagnosable from the log
        # instead of just showing up as a suspiciously small digest.
        logger.warning(
            f"Backfill fell short: needed {needed}, delivered {len(chosen)} "
            f"({fetched} fetched from OpenAlex -> {after_dedup} after exclude/dedup -> "
            f"{gated} passed the relevance gate)"
        )
    else:
        logger.info(f"Backfilled {len(chosen)} highly-cited papers (needed {needed})")
    return chosen
