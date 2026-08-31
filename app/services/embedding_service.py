import logging
from functools import lru_cache

from sentence_transformers import SentenceTransformer

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmbeddingService:
    """Dense text embeddings via a local sentence-transformers model."""

    def __init__(self, model_name: str | None = None) -> None:
        self._model_name = model_name or settings.EMBEDDING_MODEL_NAME
        logger.info("Loading embedding model: %s", self._model_name)
        self._model = SentenceTransformer(self._model_name)

    def embed(self, text: str) -> list[float]:
        vector = self._model.encode(text, normalize_embeddings=True)
        return vector.tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(texts, normalize_embeddings=True, batch_size=32)
        return [v.tolist() for v in vectors]


@lru_cache
def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()
