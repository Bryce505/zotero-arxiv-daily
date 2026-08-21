"""Per-cluster quota allocation (spec finding 12)."""

from zotero_arxiv_daily.protocol import Paper
from zotero_arxiv_daily.quota import allocate_quota, take_by_quota


def make_paper(title: str, cluster: str, score: float) -> Paper:
    return Paper(
        source="pubmed",
        title=title,
        authors=[],
        abstract="abs",
        url="https://example.org/" + title,
        score=score,
        cluster=cluster,
    )


def test_quota_sums_to_the_requested_total():
    quota = allocate_quota({"a": 100, "b": 25, "c": 4}, total=18)
    assert sum(quota.values()) == 18


def test_quota_compresses_imbalance_by_square_root():
    # 100:25:4 in raw size is 10:5:2 under sqrt, so the smallest cluster
    # keeps a meaningful share instead of being crowded out.
    quota = allocate_quota({"a": 100, "b": 25, "c": 4}, total=17)
    assert quota == {"a": 10, "b": 5, "c": 2}


def test_the_floor_does_not_distort_an_allocation_that_already_clears_it():
    # Every cluster is already above the floor, so the sqrt proportion stands.
    quota = allocate_quota({"a": 100, "b": 25, "c": 4}, total=17, min_per_cluster=2)
    assert quota == {"a": 10, "b": 5, "c": 2}


def test_every_cluster_gets_at_least_the_floor():
    quota = allocate_quota({"big": 400, "tiny": 1}, total=20, min_per_cluster=2)
    assert quota["tiny"] >= 2
    assert sum(quota.values()) == 20


def test_raising_a_cluster_to_the_floor_takes_from_the_largest():
    quota = allocate_quota({"big": 400, "tiny": 1}, total=20, min_per_cluster=2)
    assert quota == {"big": 18, "tiny": 2}


def test_total_below_the_combined_floor_spreads_one_each():
    quota = allocate_quota({"a": 9, "b": 4, "c": 1}, total=2, min_per_cluster=1)
    assert sum(quota.values()) == 2
    assert set(quota) == {"a", "b", "c"}
    assert quota["a"] == 1


def test_no_clusters_yields_no_quota():
    assert allocate_quota({}, total=15) == {}


def test_take_by_quota_picks_the_best_of_each_cluster():
    ranked = [
        make_paper("a1", "a", 9.0),
        make_paper("a2", "a", 8.0),
        make_paper("b1", "b", 7.0),
        make_paper("a3", "a", 6.0),
        make_paper("b2", "b", 5.0),
    ]
    taken = take_by_quota(ranked, {"a": 2, "b": 1})
    assert [p.title for p in taken] == ["a1", "a2", "b1"]


def test_take_by_quota_redistributes_an_underfilled_cluster():
    ranked = [
        make_paper("a1", "a", 9.0),
        make_paper("a2", "a", 8.0),
        make_paper("a3", "a", 7.0),
        make_paper("b1", "b", 6.0),
    ]
    # b is owed 3 but only has 1; the surplus goes to a by score order.
    taken = take_by_quota(ranked, {"a": 1, "b": 3})
    assert [p.title for p in taken] == ["a1", "a2", "a3", "b1"]


def test_take_by_quota_returns_papers_in_descending_score():
    ranked = [
        make_paper("a1", "a", 9.0),
        make_paper("b1", "b", 8.5),
        make_paper("a2", "a", 8.0),
    ]
    taken = take_by_quota(ranked, {"a": 2, "b": 1})
    assert [p.score for p in taken] == [9.0, 8.5, 8.0]


def test_take_by_quota_ignores_clusters_with_no_quota():
    ranked = [make_paper("a1", "a", 9.0), make_paper("z1", "z", 8.0)]
    taken = take_by_quota(ranked, {"a": 1})
    assert [p.title for p in taken] == ["a1"]
