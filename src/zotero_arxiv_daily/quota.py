"""Per-cluster quota allocation.

``BaseReranker.rerank`` sums similarity across the whole corpus, so a global
top-N is dominated by whichever theme the user happens to have collected most
of.  With a 28x spread between the largest and smallest cluster that crowds
the small themes out entirely.  Quotas are therefore allocated on the *square
root* of each cluster's corpus size, which compresses the imbalance, and a
floor rescues any cluster the proportion would still starve.

The floor is applied only where it binds: a cluster already above it keeps its
proportional share, so the sqrt distribution is not distorted for free.
"""

import math

from .protocol import Paper


def _proportional(names: list[str], weights: dict[str, float], total: int) -> dict[str, int]:
    """Largest-remainder apportionment of *total* across *names*."""
    weight_sum = sum(weights.values())
    exact = {name: total * weights[name] / weight_sum for name in names}
    quota = {name: int(exact[name]) for name in names}
    leftover = total - sum(quota.values())
    # Ties broken by name so the allocation is deterministic run to run.
    by_remainder = sorted(names, key=lambda n: (-(exact[n] - quota[n]), n))
    for name in by_remainder[:leftover]:
        quota[name] += 1
    return quota


def allocate_quota(
    cluster_sizes: dict[str, int],
    total: int,
    min_per_cluster: int = 1,
) -> dict[str, int]:
    """Split *total* slots across clusters by sqrt of their corpus size."""
    if not cluster_sizes or total <= 0:
        return {}

    names = sorted(cluster_sizes)
    weights = {name: math.sqrt(max(cluster_sizes[name], 0)) or 1.0 for name in names}

    # Not enough slots to honour the requested floor everywhere: lower it to
    # what the total can actually pay for, rather than shipping a short digest.
    min_per_cluster = min(min_per_cluster, total // len(names))

    quota = _proportional(names, weights, total)

    # Raise whoever the proportion starved, and pay for it from the largest
    # allocations that can spare a slot.
    debt = sum(max(0, min_per_cluster - quota[name]) for name in names)
    if debt:
        for name in names:
            quota[name] = max(quota[name], min_per_cluster)
        donors = sorted(names, key=lambda n: (-quota[n], n))
        while debt > 0:
            progressed = False
            for name in donors:
                if debt == 0:
                    break
                if quota[name] > min_per_cluster:
                    quota[name] -= 1
                    debt -= 1
                    progressed = True
            if not progressed:
                break
    return quota


def _rank_key(paper: Paper) -> float:
    """Best-first sort key: the composite score when the paper was scored,
    falling back to embedding similarity for any paper that reached here
    without a ScoreBreakdown (should not happen once every survivor is
    gated, but this must never crash)."""
    if paper.scoring is not None:
        return paper.scoring.rank_score
    return paper.score or 0.0


def take_by_quota(ranked: list[Paper], quota: dict[str, int]) -> list[Paper]:
    """Take each cluster's quota from *ranked*, then redistribute shortfalls.

    *ranked* must already be sorted best-first.  Clusters holding fewer
    candidates than they are owed release the surplus to the remaining papers
    in score order, so the digest still reaches its target length.
    """
    remaining = dict(quota)
    taken: list[Paper] = []
    overflow: list[Paper] = []
    for paper in ranked:
        cluster = paper.cluster
        if cluster not in remaining:
            continue
        if remaining[cluster] > 0:
            remaining[cluster] -= 1
            taken.append(paper)
        else:
            overflow.append(paper)

    shortfall = sum(remaining.values())
    if shortfall > 0:
        taken.extend(overflow[:shortfall])

    return sorted(taken, key=lambda p: -_rank_key(p))
