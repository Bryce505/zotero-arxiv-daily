"""Tests for ApiReranker — uses stub OpenAI client via monkeypatch."""

from zotero_arxiv_daily.reranker.api import ApiReranker


def test_api_reranker_similarity_shape(config, patch_openai):
    reranker = ApiReranker(config)
    score = reranker.get_similarity_score(["hello", "world"], ["ping"])
    assert score.shape == (2, 1)


def test_api_reranker_batching(config, patch_openai):
    reranker = ApiReranker(config)
    s1 = [f"text {i}" for i in range(5)]
    s2 = [f"corpus {i}" for i in range(3)]
    score = reranker.get_similarity_score(s1, s2)
    assert score.shape == (5, 3)


def test_api_reranker_embeds_texts(config, patch_openai):
    reranker = ApiReranker(config)
    vectors = reranker.embed(["hello", "world", "again"])
    assert vectors.shape == (3, 3)


def test_api_reranker_embed_respects_the_batch_size(config, patch_openai, monkeypatch):
    sizes = []
    original = patch_openai.embeddings.create

    def recording(**kwargs):
        sizes.append(len(kwargs["input"]))
        return original(**kwargs)

    monkeypatch.setattr(patch_openai.embeddings, "create", recording)
    config.reranker.api.batch_size = 2
    ApiReranker(config).embed([f"t{i}" for i in range(5)])
    assert sizes == [2, 2, 1]


def test_api_reranker_embed_of_nothing_is_empty(config, patch_openai):
    assert ApiReranker(config).embed([]).shape[0] == 0


def test_similarity_and_embed_agree(config, patch_openai):
    import numpy as np

    reranker = ApiReranker(config)
    sim = reranker.get_similarity_score(["a", "b"], ["c"])
    v1, v2 = reranker.embed(["a", "b"]), reranker.embed(["c"])
    n1 = v1 / np.linalg.norm(v1, axis=1, keepdims=True)
    n2 = v2 / np.linalg.norm(v2, axis=1, keepdims=True)
    assert np.allclose(sim, n1 @ n2.T)
