"""Tests for LocalReranker — requires sentence-transformers, marked slow."""

import pytest

from zotero_arxiv_daily.reranker.local import LocalReranker


@pytest.mark.slow
def test_local_reranker(config):
    reranker = LocalReranker(config)
    score = reranker.get_similarity_score(["hello", "world"], ["ping"])
    assert score.shape == (2, 1)


@pytest.mark.slow
def test_local_reranker_embeds_texts(config):
    from zotero_arxiv_daily.reranker.local import LocalReranker

    vectors = LocalReranker(config).embed(["hello world", "another text"])
    assert vectors.shape[0] == 2
    assert vectors.shape[1] > 0
