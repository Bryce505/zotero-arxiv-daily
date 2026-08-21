"""Corpus vector caching.

The Zotero corpus barely changes week to week, but the runner is ephemeral,
so its embeddings are recomputed from scratch every run — measured at about
3.9 s/paper, roughly seven minutes of the weekly budget. Caching the corpus
vectors removes that fixed cost. Candidate vectors are new every week and
cannot be cached.
"""

import numpy as np
import pytest

from zotero_arxiv_daily.protocol import CorpusPaper, Paper
from zotero_arxiv_daily.reranker.base import BaseReranker
from zotero_arxiv_daily.reranker.vector_cache import (
    cached_similarity_matrix,
    load_vectors,
    save_vectors,
)
from datetime import datetime


class CountingReranker(BaseReranker):
    """Embeds deterministically and records exactly what it was asked to embed."""

    def __init__(self):
        self.config = None
        self.embedded: list[list[str]] = []

    def embed(self, texts: list[str]) -> np.ndarray:
        self.embedded.append(list(texts))
        return np.array([[float(len(t)), float(sum(map(ord, t)) % 97), 1.0] for t in texts])

    def get_similarity_score(self, s1, s2):  # pragma: no cover - unused here
        raise NotImplementedError


def make_corpus(n=3) -> list[CorpusPaper]:
    return [
        CorpusPaper(
            title=f"Corpus {i}",
            abstract=f"corpus abstract {i}",
            added_date=datetime(2026, 1, i + 1),
            paths=["文献"],
        )
        for i in range(n)
    ]


def make_candidates(n=2) -> list[Paper]:
    return [
        Paper(source="s", title=f"C{i}", authors=[], abstract=f"candidate abstract {i}", url=f"u{i}")
        for i in range(n)
    ]


def test_vectors_round_trip(tmp_path):
    path = str(tmp_path / "vectors.npz")
    vectors = {"a": np.array([1.0, 2.0]), "b": np.array([3.0, 4.0])}
    save_vectors(path, "model-x", vectors)
    loaded = load_vectors(path, "model-x")
    assert set(loaded) == {"a", "b"}
    assert np.allclose(loaded["a"], [1.0, 2.0])


def test_a_missing_cache_loads_empty():
    assert load_vectors("/nonexistent/vectors.npz", "model-x") == {}


def test_a_cache_from_another_model_is_ignored(tmp_path):
    path = str(tmp_path / "vectors.npz")
    save_vectors(path, "model-x", {"a": np.array([1.0])})
    assert load_vectors(path, "model-y") == {}


def test_an_unreadable_cache_loads_empty(tmp_path):
    path = str(tmp_path / "vectors.npz")
    with open(path, "wb") as handle:
        handle.write(b"not an npz file")
    assert load_vectors(path, "model-x") == {}


def test_the_first_run_embeds_everything(tmp_path):
    path = str(tmp_path / "vectors.npz")
    reranker = CountingReranker()
    cached_similarity_matrix(reranker, make_candidates(2), make_corpus(3), path, "model-x")
    embedded = [t for batch in reranker.embedded for t in batch]
    assert len([t for t in embedded if t.startswith("corpus")]) == 3


def test_a_warm_cache_embeds_no_corpus_text(tmp_path):
    path = str(tmp_path / "vectors.npz")
    corpus = make_corpus(3)
    cached_similarity_matrix(CountingReranker(), make_candidates(2), corpus, path, "model-x")

    second = CountingReranker()
    cached_similarity_matrix(second, make_candidates(2), corpus, path, "model-x")
    embedded = [t for batch in second.embedded for t in batch]
    assert not any(t.startswith("corpus") for t in embedded)


def test_candidates_are_always_embedded_afresh(tmp_path):
    path = str(tmp_path / "vectors.npz")
    corpus = make_corpus(3)
    cached_similarity_matrix(CountingReranker(), make_candidates(2), corpus, path, "model-x")

    second = CountingReranker()
    cached_similarity_matrix(second, make_candidates(2), corpus, path, "model-x")
    embedded = [t for batch in second.embedded for t in batch]
    assert len([t for t in embedded if t.startswith("candidate")]) == 2


def test_only_the_newly_added_corpus_papers_are_embedded(tmp_path):
    path = str(tmp_path / "vectors.npz")
    cached_similarity_matrix(CountingReranker(), make_candidates(1), make_corpus(3), path, "model-x")

    second = CountingReranker()
    cached_similarity_matrix(second, make_candidates(1), make_corpus(5), path, "model-x")
    embedded = [t for batch in second.embedded for t in batch]
    new_corpus = [t for t in embedded if t.startswith("corpus")]
    assert new_corpus == ["corpus abstract 3", "corpus abstract 4"]


def test_the_matrix_has_the_candidate_by_corpus_shape(tmp_path):
    path = str(tmp_path / "vectors.npz")
    matrix = cached_similarity_matrix(
        CountingReranker(), make_candidates(2), make_corpus(3), path, "model-x"
    )
    assert matrix.shape == (2, 3)


def test_a_warm_cache_gives_the_same_matrix_as_a_cold_one(tmp_path):
    path = str(tmp_path / "vectors.npz")
    candidates, corpus = make_candidates(2), make_corpus(3)
    cold = cached_similarity_matrix(CountingReranker(), candidates, corpus, path, "model-x")
    warm = cached_similarity_matrix(CountingReranker(), candidates, corpus, path, "model-x")
    assert np.allclose(cold, warm)


def test_the_matrix_is_cosine_similarity(tmp_path):
    path = str(tmp_path / "vectors.npz")
    reranker = CountingReranker()
    candidates, corpus = make_candidates(2), make_corpus(3)
    matrix = cached_similarity_matrix(reranker, candidates, corpus, path, "model-x")

    expected_c = reranker.embed([c.abstract for c in candidates])
    expected_k = reranker.embed([c.abstract for c in corpus])
    nc = expected_c / np.linalg.norm(expected_c, axis=1, keepdims=True)
    nk = expected_k / np.linalg.norm(expected_k, axis=1, keepdims=True)
    assert np.allclose(matrix, nc @ nk.T)


def test_the_cache_keeps_only_the_current_corpus(tmp_path):
    """Otherwise the file grows without bound as the library churns."""
    path = str(tmp_path / "vectors.npz")
    cached_similarity_matrix(CountingReranker(), make_candidates(1), make_corpus(5), path, "model-x")
    cached_similarity_matrix(CountingReranker(), make_candidates(1), make_corpus(2), path, "model-x")
    assert len(load_vectors(path, "model-x")) == 2


def test_a_reranker_without_embed_support_is_rejected(tmp_path):
    class NoEmbed(BaseReranker):
        def __init__(self):
            self.config = None

        def get_similarity_score(self, s1, s2):
            raise NotImplementedError

    with pytest.raises(NotImplementedError):
        cached_similarity_matrix(
            NoEmbed(), make_candidates(1), make_corpus(1), str(tmp_path / "v.npz"), "m"
        )


def test_a_cache_path_without_the_npz_extension_still_round_trips(tmp_path):
    """numpy appends .npz on save; loading must look in the same place."""
    path = str(tmp_path / "vectors")
    save_vectors(path, "model-x", {"a": np.array([1.0, 2.0])})
    assert set(load_vectors(path, "model-x")) == {"a"}
