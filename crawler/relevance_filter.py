"""Lightweight, local, non-LLM relevance gate: a Jakarta location keyword check, and a
multilingual embedding-similarity check against fault_lines exemplars (not keyword
matching - real posts about tree removal or ERP pricing rarely say "climate" outright).
Runs directly on raw Indonesian text, since this uses its own multilingual model
(separate from the AI service's English-only one) purely for filtering."""

import numpy as np
from sentence_transformers import SentenceTransformer

from crawler.config import CrawlerSettings


class RelevanceFilter:
    def __init__(self, settings: CrawlerSettings, exemplar_texts: list[str]) -> None:
        self._settings = settings
        self._model = SentenceTransformer(settings.RELEVANCE_MODEL_NAME)
        self._exemplar_embeddings = (
            self._model.encode(exemplar_texts, normalize_embeddings=True)
            if exemplar_texts
            else None
        )

    def location_matches(self, text: str) -> bool:
        lowered = text.lower()
        return any(keyword in lowered for keyword in self._settings.LOCATION_KEYWORDS)

    def is_relevant(self, text: str) -> bool:
        if self._exemplar_embeddings is None:
            return True  # no exemplars yet (fault_lines empty) - don't block everything
        vector = self._model.encode(text, normalize_embeddings=True)
        similarity = float(np.max(self._exemplar_embeddings @ vector))
        return similarity >= self._settings.RELEVANCE_THRESHOLD
