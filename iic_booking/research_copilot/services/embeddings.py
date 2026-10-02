"""Embedding provider abstraction (Phase AI.2)."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod

from django.conf import settings

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}", re.I)


class EmbeddingProvider(ABC):
    name: str = "base"
    version: str = "1"

    @abstractmethod
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class LocalHashEmbedding(EmbeddingProvider):
    """Deterministic bag-of-tokens embedding for offline/dev (no external API)."""

    name = "local_hash"
    version = "v1"
    DIM = 256

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        tokens = _TOKEN_RE.findall((text or "").lower())
        if not tokens:
            return vec
        for tok in tokens:
            h = int(hashlib.sha256(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.DIM
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            vec[idx] += sign
        # L2 normalize
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class OpenAIEmbedding(EmbeddingProvider):
    name = "openai"
    version = "text-embedding-3-small"

    def __init__(self, api_key: str, model: str | None = None):
        self.api_key = api_key
        self.model = model or getattr(settings, "RESEARCH_COPILOT_EMBEDDING_MODEL", "text-embedding-3-small")
        self.version = self.model

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key)
        # Batch in chunks of 64
        out: list[list[float]] = []
        for i in range(0, len(texts), 64):
            batch = texts[i : i + 64]
            resp = client.embeddings.create(model=self.model, input=batch)
            ordered = sorted(resp.data, key=lambda d: d.index)
            out.extend([list(d.embedding) for d in ordered])
        return out


class EmbeddingUnavailable(RuntimeError):
    """The embedding backend could not produce vectors (network, model missing, bad response)."""


class OllamaEmbedding(EmbeddingProvider):
    """Local Ollama embeddings (`/api/embed`). Runs on the same host; no data leaves the server."""

    name = "ollama"
    BATCH = 16

    def __init__(self, *, base_url: str | None = None, model: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or getattr(settings, "OLLAMA_BASE_URL", "") or "http://ollama:11434").rstrip("/")
        self.model = (model or getattr(settings, "RESEARCH_COPILOT_EMBEDDING_MODEL", "") or "nomic-embed-text").strip()
        if self.model.startswith("text-embedding-"):
            self.model = "nomic-embed-text"
        self.version = self.model
        self.timeout = float(timeout or getattr(settings, "RESEARCH_COPILOT_EMBEDDING_TIMEOUT_SECONDS", 120) or 120)

    def _prefix(self, kind: str) -> str:
        # nomic-embed-text is trained with task prefixes; retrieval quality drops noticeably without them.
        if "nomic" in self.model:
            return "search_query: " if kind == "query" else "search_document: "
        return ""

    def _embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        import requests

        prefix = self._prefix(kind)
        out: list[list[float]] = []
        for i in range(0, len(texts), self.BATCH):
            batch = [prefix + (t or "") for t in texts[i : i + self.BATCH]]
            try:
                resp = requests.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model, "input": batch, "truncate": True},
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise EmbeddingUnavailable(f"ollama_unreachable: {exc.__class__.__name__}") from exc
            if resp.status_code != 200:
                raise EmbeddingUnavailable(f"ollama_http_{resp.status_code}")
            vectors = (resp.json() or {}).get("embeddings") or []
            if len(vectors) != len(batch) or not all(isinstance(v, list) and v for v in vectors):
                raise EmbeddingUnavailable("ollama_bad_response")
            out.extend([[float(x) for x in v] for v in vectors])
        return out

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, kind="document")

    def embed_query(self, text: str) -> list[float]:
        """
        Chat-path embedding: cached per (model, text), with a short timeout and a brief circuit breaker so
        a slow or unreachable Ollama costs one quick failure instead of a long stall on every message.
        Bulk document indexing (embed_texts) keeps the long timeout.
        """
        from django.core.cache import cache

        norm = " ".join((text or "").lower().split())
        key = "rc_qemb:" + hashlib.sha256(f"{self.model}|{norm}".encode("utf-8")).hexdigest()
        try:
            cached = cache.get(key)
        except Exception:  # noqa: BLE001
            cached = None
        if isinstance(cached, list) and cached:
            return cached
        try:
            if cache.get(QUERY_EMBED_DOWN_KEY):
                raise EmbeddingUnavailable("ollama_recently_unavailable")
        except EmbeddingUnavailable:
            raise
        except Exception:  # noqa: BLE001
            pass
        long_timeout = self.timeout
        self.timeout = min(long_timeout, query_timeout_seconds())
        try:
            vec = self._embed([text], kind="query")[0]
        except EmbeddingUnavailable:
            try:
                cache.set(QUERY_EMBED_DOWN_KEY, 1, 60)
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            self.timeout = long_timeout
        try:
            cache.set(key, vec, 24 * 60 * 60)
        except Exception:  # noqa: BLE001
            pass
        return vec


QUERY_EMBED_DOWN_KEY = "rc_qemb:down"


def query_timeout_seconds() -> float:
    return float(getattr(settings, "RESEARCH_COPILOT_QUERY_EMBEDDING_TIMEOUT_SECONDS", 8) or 8)


def get_embedding_provider() -> EmbeddingProvider:
    provider = (getattr(settings, "RESEARCH_COPILOT_EMBEDDING_PROVIDER", None) or "auto").lower()
    api_key = (getattr(settings, "OPENAI_API_KEY", None) or "").strip()
    if provider == "local":
        return LocalHashEmbedding()
    if provider == "ollama":
        return OllamaEmbedding()
    if provider == "openai":
        if not api_key:
            logger.warning("OpenAI embedding requested but OPENAI_API_KEY missing; using local")
            return LocalHashEmbedding()
        return OpenAIEmbedding(api_key=api_key)
    # auto
    if api_key:
        try:
            return OpenAIEmbedding(api_key=api_key)
        except Exception:
            logger.warning("OpenAI embedding init failed; using local", exc_info=True)
    return LocalHashEmbedding()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return float(dot / (na * nb))
