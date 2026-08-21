from .base import BaseReranker, register_reranker
import logging
import warnings
import numpy as np
@register_reranker("local")
class LocalReranker(BaseReranker):
    def _encoder(self):
        from sentence_transformers import SentenceTransformer
        if not self.config.executor.debug:
            from transformers.utils import logging as transformers_logging
            from huggingface_hub.utils import logging as hf_logging
    
            transformers_logging.set_verbosity_error()
            hf_logging.set_verbosity_error()
            logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
            logging.getLogger("sentence_transformers.SentenceTransformer").setLevel(logging.ERROR)
            logging.getLogger("transformers").setLevel(logging.ERROR)
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
            logging.getLogger("huggingface_hub.utils._http").setLevel(logging.ERROR)
            warnings.filterwarnings("ignore", category=FutureWarning)

        encoder = SentenceTransformer(self.config.reranker.local.model, trust_remote_code=True)
        encode_kwargs = self.config.reranker.local.encode_kwargs or {}
        return encoder, encode_kwargs

    def embed(self, texts: list[str]) -> np.ndarray:
        """Return the [n_texts, dim] embedding matrix."""
        if not texts:
            return np.empty((0, 0))
        encoder, encode_kwargs = self._encoder()
        return np.asarray(encoder.encode(texts, **encode_kwargs, show_progress_bar=True))

    def get_similarity_score(self, s1: list[str], s2: list[str]) -> np.ndarray:
        encoder, encode_kwargs = self._encoder()
        s1_feature = encoder.encode(s1,**encode_kwargs,show_progress_bar=True)
        s2_feature = encoder.encode(s2,**encode_kwargs,show_progress_bar=True)
        sim = encoder.similarity(s1_feature, s2_feature)
        return sim.numpy()