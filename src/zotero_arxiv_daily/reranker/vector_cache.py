"""Cache the Zotero corpus's embeddings between runs.

The corpus barely changes week to week, but the Actions runner is ephemeral,
so its vectors are recomputed from scratch every run — measured at about
3.9 s/paper on the runner's CPU, roughly seven minutes of the weekly budget.
Caching removes that fixed cost.

Candidate vectors are new every week and are never cached.
"""

import hashlib
import os

import numpy as np
from loguru import logger

from ..protocol import CorpusPaper, Paper

_MODEL_KEY = "__model__"


def _text_key(text: str) -> str:
    """A filesystem- and npz-safe key for an abstract of arbitrary length."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_vectors(path: str, model: str) -> dict[str, np.ndarray]:
    """Load cached vectors, or an empty mapping when unusable.

    A cache built by a different embedding model is discarded: its vectors
    live in another space and mixing them would silently corrupt every score.
    """
    if not os.path.exists(path):
        return {}
    try:
        with np.load(path, allow_pickle=False) as data:
            cached_model = str(data[_MODEL_KEY].item()) if _MODEL_KEY in data else ""
            if cached_model != model:
                logger.info(f"Vector cache was built by {cached_model!r}, not {model!r}; rebuilding")
                return {}
            return {k: data[k] for k in data.files if k != _MODEL_KEY}
    except Exception as exc:  # noqa: BLE001 - a corrupt cache must not break the run
        logger.warning(f"Ignoring unreadable vector cache {path}: {exc}")
        return {}


def save_vectors(path: str, model: str, vectors: dict[str, np.ndarray]) -> None:
    """Write *vectors* alongside the model that produced them."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    np.savez_compressed(path, **{_MODEL_KEY: np.array(model), **vectors})


def cached_similarity_matrix(
    reranker,
    candidates: list[Paper],
    corpus: list[CorpusPaper],
    cache_path: str,
    model: str,
) -> np.ndarray:
    """Return the [n_candidate, n_corpus] cosine similarity matrix.

    Corpus vectors come from the cache where available; only papers added
    since the last run are embedded. Columns follow *corpus* as given.
    """
    embed = getattr(reranker, "embed", None)
    if embed is None:
        raise NotImplementedError(
            f"{type(reranker).__name__} does not implement embed(); vector caching needs it"
        )

    cached = load_vectors(cache_path, model)
    corpus_texts = [c.abstract for c in corpus]
    keys = [_text_key(t) for t in corpus_texts]

    missing = [(key, text) for key, text in zip(keys, corpus_texts) if key not in cached]
    if missing:
        logger.info(f"Embedding {len(missing)} corpus papers ({len(corpus) - len(missing)} cached)")
        fresh = embed([text for _, text in missing])
        for (key, _), vector in zip(missing, fresh):
            cached[key] = np.asarray(vector)
    else:
        logger.info(f"All {len(corpus)} corpus vectors served from cache")

    corpus_matrix = np.vstack([cached[k] for k in keys])
    candidate_matrix = np.asarray(embed([c.abstract for c in candidates]))

    # Keep only what the current corpus needs, so the committed cache does not
    # grow without bound as the library churns.
    save_vectors(cache_path, model, {k: cached[k] for k in keys})

    return _cosine(candidate_matrix, corpus_matrix)


def _cosine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a_norm = a / np.linalg.norm(a, axis=1, keepdims=True)
    b_norm = b / np.linalg.norm(b, axis=1, keepdims=True)
    return a_norm @ b_norm.T
