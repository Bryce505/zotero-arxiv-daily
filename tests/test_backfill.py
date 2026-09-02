"""Highly-cited backfill when the week is thin (spec 8.5)."""

from zotero_arxiv_daily.backfill import backfill_papers
from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.search.profile import QueryProfile


class StubRetriever:
    """Returns a fixed pool, recording how it was asked."""

    def __init__(self, pool):
        self.pool = pool
        self.calls = []

    def search_highly_cited(self, query, limit):
        self.calls.append((query, limit))
        # Fresh copies: the caller mutates cluster and is_backfill.
        return [
            Paper(
                source=p.source,
                title=p.title,
                authors=[],
                abstract=p.abstract,
                url=p.url,
                doi=p.doi,
                cited_by_count=p.cited_by_count,
            )
            for p in self.pool
        ]


def make_paper(doi: str, cited: int) -> Paper:
    return Paper(
        source="openalex",
        title=f"Paper {doi}",
        authors=[],
        abstract="abs",
        url="u",
        doi=doi,
        cited_by_count=cited,
    )


PROFILES = [
    QueryProfile(cluster="a", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="charge variant"),
    QueryProfile(cluster="b", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="host cell protein"),
]


def test_backfill_returns_at_most_what_is_needed():
    pool = [make_paper(f"10.1000/{i}", 100 - i) for i in range(10)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=3, exclude_dois=set())
    assert len(result) == 3


def test_backfill_orders_by_citation_count():
    pool = [make_paper("10.1000/low", 5), make_paper("10.1000/high", 500), make_paper("10.1000/mid", 50)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=3, exclude_dois=set())
    assert [p.cited_by_count for p in result] == [500, 50, 5]


def test_backfill_excludes_dois_already_in_the_digest_or_library():
    pool = [make_paper("10.1000/a", 100), make_paper("10.1000/b", 90)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=5, exclude_dois={"10.1000/a"})
    assert [p.doi for p in result] == ["10.1000/b"]


def test_backfill_deduplicates_across_clusters():
    pool = [make_paper("10.1000/same", 100)]
    result = backfill_papers(PROFILES, StubRetriever(pool), needed=5, exclude_dois=set())
    assert len(result) == 1


def test_backfill_tags_each_paper_with_its_cluster():
    retriever = StubRetriever([make_paper("10.1000/a", 100)])
    result = backfill_papers(PROFILES[:1], retriever, needed=1, exclude_dois=set())
    assert result[0].cluster == "a"
    assert result[0].is_backfill is True


def test_backfill_queries_every_cluster():
    retriever = StubRetriever([make_paper("10.1000/a", 100)])
    backfill_papers(PROFILES, retriever, needed=2, exclude_dois=set())
    assert [q for q, _ in retriever.calls] == ["charge variant", "host cell protein"]


def test_backfill_does_nothing_when_nothing_is_needed():
    retriever = StubRetriever([make_paper("10.1000/a", 100)])
    assert backfill_papers(PROFILES, retriever, needed=0, exclude_dois=set()) == []
    assert retriever.calls == []


def test_backfill_does_nothing_without_profiles():
    retriever = StubRetriever([make_paper("10.1000/a", 100)])
    assert backfill_papers([], retriever, needed=5, exclude_dois=set()) == []
    assert retriever.calls == []


def test_backfill_candidates_pass_through_the_gate():
    profiles = [QueryProfile(cluster="c", mesh_terms=[], free_terms=[], pubmed_query="q", plain_query="p")]

    class Retriever:
        def search_highly_cited(self, query, limit):
            return [
                make_paper("10.1/keep", 500),
                make_paper("10.1/drop", 900),
            ]

    def gate(papers):
        return [p for p in papers if p.doi == "10.1/keep"]

    # Without the gate the 900-citation paper would win. Highly cited is not
    # the same as relevant — that is how a 2005 virology paper got in.
    chosen = backfill_papers(profiles, Retriever(), needed=2, exclude_dois=set(), gate=gate)
    assert [p.doi for p in chosen] == ["10.1/keep"]


def test_backfill_without_a_gate_keeps_everything():
    profiles = [QueryProfile(cluster="c", mesh_terms=[], free_terms=[], pubmed_query="q", plain_query="p")]

    class Retriever:
        def search_highly_cited(self, query, limit):
            return [make_paper("10.1/a", 5)]

    assert len(backfill_papers(profiles, Retriever(), needed=1, exclude_dois=set())) == 1


# --------------------------------------------------------------------------- multi-round retries


class RoundRetriever:
    """Answers each distinct query with its own pool, recording the order."""

    def __init__(self, pools: dict[str, list[Paper]]):
        self.pools = pools
        self.calls: list[str] = []

    def search_highly_cited(self, query, limit):
        self.calls.append(query)
        return [
            Paper(
                source=p.source,
                title=p.title,
                authors=[],
                abstract=p.abstract,
                url=p.url,
                doi=p.doi,
                cited_by_count=p.cited_by_count,
            )
            for p in self.pools.get(query, [])
        ]


ONE_PROFILE = [
    QueryProfile(cluster="a", mesh_terms=[], free_terms=[], pubmed_query="", plain_query="round one")
]


def test_a_short_round_triggers_a_requery_and_a_second_round():
    """One round of the same query is what left run 33517443909 with zero
    classics; a second angle is the cheapest thing that can still find some."""
    retriever = RoundRetriever(
        {"round one": [make_paper("10.1000/a", 100)], "round two": [make_paper("10.1000/b", 90)]}
    )
    asked: list = []

    def requery(profiles, tried):
        asked.append(dict(tried))
        return {"a": "round two"}

    result = backfill_papers(ONE_PROFILE, retriever, needed=2, exclude_dois=set(), requery=requery)
    assert [p.doi for p in result] == ["10.1000/a", "10.1000/b"]
    assert retriever.calls == ["round one", "round two"]
    assert asked == [{"a": ["round one"]}], "the requery must know what has already been tried"


def test_rounds_stop_as_soon_as_enough_papers_are_found():
    retriever = RoundRetriever({"round one": [make_paper(f"10.1000/{i}", 100 - i) for i in range(5)]})

    def requery(profiles, tried):
        raise AssertionError("no second round is needed once the quota is met")

    assert len(backfill_papers(ONE_PROFILE, retriever, needed=3, exclude_dois=set(), requery=requery)) == 3
    assert retriever.calls == ["round one"]


def test_rounds_are_capped():
    retriever = RoundRetriever({"round one": [make_paper("10.1000/a", 100)]})
    rounds = {"n": 1}

    def requery(profiles, tried):
        rounds["n"] += 1
        return {"a": f"round {rounds['n']}"}  # always a query with an empty pool

    backfill_papers(ONE_PROFILE, retriever, needed=9, exclude_dois=set(), requery=requery, max_rounds=3)
    assert len(retriever.calls) == 3


def test_without_a_requery_only_one_round_runs():
    """Unchanged behaviour for any caller that does not pass one."""
    retriever = RoundRetriever({"round one": [make_paper("10.1000/a", 100)]})
    assert len(backfill_papers(ONE_PROFILE, retriever, needed=5, exclude_dois=set())) == 1
    assert retriever.calls == ["round one"]


def test_a_requery_that_fails_to_produce_queries_stops_the_loop():
    retriever = RoundRetriever({"round one": [make_paper("10.1000/a", 100)]})
    backfill_papers(ONE_PROFILE, retriever, needed=5, exclude_dois=set(), requery=lambda p, t: {})
    assert retriever.calls == ["round one"]


def test_a_later_round_does_not_re_gate_papers_an_earlier_round_rejected():
    """Re-judging the same paper is a wasted LLM call, and the second verdict
    could differ from the first, which would look like a flake to a reader."""
    repeat = make_paper("10.1000/dup", 100)
    retriever = RoundRetriever({"round one": [repeat], "round two": [repeat, make_paper("10.1000/new", 50)]})
    gated: list[list[str]] = []

    def gate(papers):
        gated.append([p.doi for p in papers])
        return [p for p in papers if p.doi != "10.1000/dup"]  # rejects the repeat every time

    backfill_papers(
        ONE_PROFILE, retriever, needed=2, exclude_dois=set(), gate=gate,
        requery=lambda p, t: {"a": "round two"},
    )
    assert gated == [["10.1000/dup"], ["10.1000/new"]]
