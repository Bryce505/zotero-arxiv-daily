from .base import BaseReranker, register_reranker
from openai import OpenAI
import numpy as np
@register_reranker("api")
class ApiReranker(BaseReranker):
    def _client(self) -> OpenAI:
        return OpenAI(api_key=self.config.reranker.api.key, base_url=self.config.reranker.api.base_url)

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return the [n_texts, dim] embedding matrix, batched to the provider's limit."""
        if not texts:
            return np.empty((0, 0))
        client = self._client()
        batch_size = self.config.reranker.api.get("batch_size") or 64
        embeddings = []
        for i in range(0, len(texts), batch_size):
            response = client.embeddings.create(
                input=texts[i:i + batch_size],
                model=self.config.reranker.api.model
            )
            embeddings.extend([r.embedding for r in response.data])
        return np.array(embeddings)

    def get_similarity_score(self, s1: list[str], s2: list[str]) -> np.ndarray:
        all_embeddings = self.embed(s1 + s2)
        s1_embeddings = all_embeddings[:len(s1)]                      # [n_s1, d]
        s2_embeddings = all_embeddings[len(s1):]                      # [n_s2, d]
        s1_embeddings_normalized = s1_embeddings / np.linalg.norm(s1_embeddings, axis=1, keepdims=True)
        s2_embeddings_normalized = s2_embeddings / np.linalg.norm(s2_embeddings, axis=1, keepdims=True)
        sim = np.dot(s1_embeddings_normalized, s2_embeddings_normalized.T) # [n_s1, n_s2]
        return sim
