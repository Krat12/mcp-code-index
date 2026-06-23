"""Semantic layer: pluggable embeddings -> Qdrant vector store.

Embedding backends (selected via Settings.embed_backend):
  - "api":       OpenAI-compatible /embeddings endpoint (e.g. routerai.ru with
                 qwen/qwen3-embedding-8b). Uses stdlib urllib, no extra deps.
  - "fastembed": local on-device embeddings (offline once the model is cached).

Qdrant isolation is per-service via a unique collection name; ONE Qdrant
instance (your Docker) serves every project/microservice.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Protocol


@dataclass
class SemanticHit:
    path: str
    start_line: int
    end_line: int
    score: float
    preview: str


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ApiEmbedder:
    """OpenAI-compatible /embeddings client over stdlib urllib (batched)."""

    def __init__(self, api_base: str, model: str, api_key: str | None, batch: int = 64, timeout: float = 60.0) -> None:
        if not api_key:
            raise RuntimeError("CODE_INDEX_EMBED_API_KEY is not set for the api embed backend")
        self._url = api_base.rstrip("/") + "/embeddings"
        self._model = model
        self._key = api_key
        self._batch = batch
        self._timeout = timeout

    def _post(self, inputs: list[str]) -> list[list[float]]:
        payload = json.dumps({"model": self._model, "input": inputs}).encode("utf-8")
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self._key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        # OpenAI shape: {"data": [{"embedding": [...]}, ...]}
        data = sorted(body.get("data", []), key=lambda d: d.get("index", 0))
        return [list(map(float, d["embedding"])) for d in data]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self._batch):
            out.extend(self._post(texts[i : i + self._batch]))
        return out


class FastEmbedEmbedder:
    """Local fastembed model (downloaded once, then offline)."""

    def __init__(self, model_name: str) -> None:
        from fastembed import TextEmbedding

        self._embedder = TextEmbedding(model_name=model_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._embedder.embed(texts)]


def _make_embedder(settings) -> Embedder:
    backend = (getattr(settings, "embed_backend", "api") or "api").lower()
    if backend == "fastembed":
        return FastEmbedEmbedder(settings.embed_model)
    # default: api
    return ApiEmbedder(
        api_base=settings.embed_api_base,
        model=settings.embed_api_model,
        api_key=settings.embed_api_key,
    )


class SemanticIndex:
    """Pluggable embedder + qdrant-client. Degrades gracefully if unavailable."""

    def __init__(self, settings, embedder: Embedder | None = None) -> None:
        self.collection = settings.collection_name()
        self.available = True
        self._dim: int | None = settings.embed_dim or None
        try:
            from qdrant_client import QdrantClient

            self._embedder: Embedder = embedder or _make_embedder(settings)
            self._client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
        except Exception:
            # Missing deps, bad config, or Qdrant unreachable -> semantic disabled.
            self.available = False
            self._embedder = None  # type: ignore[assignment]
            self._client = None

    def _embed(self, texts: list[str]) -> list[list[float]]:
        vecs = self._embedder.embed(texts)
        if vecs and self._dim is None:
            self._dim = len(vecs[0])
        return vecs

    def ensure_collection(self) -> None:
        if not self.available:
            return
        from qdrant_client import models

        if self._dim is None:
            try:
                self._embed(["dimension probe"])
            except Exception:
                # Embedding endpoint unreachable -> disable semantic gracefully.
                self.available = False
                return
        try:
            exists = self._client.collection_exists(self.collection)
        except Exception:
            exists = False
        if not exists:
            self._client.create_collection(
                collection_name=self.collection,
                vectors_config=models.VectorParams(
                    size=self._dim or 384, distance=models.Distance.COSINE
                ),
            )

    def delete_path(self, path: str) -> None:
        if not self.available:
            return
        from qdrant_client import models

        try:
            self._client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[models.FieldCondition(key="path", match=models.MatchValue(value=path))]
                    )
                ),
            )
        except Exception:
            pass

    def index_chunks(self, path: str, chunks: list[tuple[int, int, str]]) -> None:
        """chunks: list of (start_line, end_line, text)."""
        if not self.available or not chunks:
            return
        from qdrant_client import models

        texts = [c[2] for c in chunks]
        try:
            vectors = self._embed(texts)
        except Exception:
            return
        points = []
        for (start, end, text), vec in zip(chunks, vectors):
            pid = uuid.uuid5(uuid.NAMESPACE_URL, f"{path}:{start}-{end}").hex
            points.append(
                models.PointStruct(
                    id=pid,
                    vector=vec,
                    payload={
                        "path": path,
                        "start_line": start,
                        "end_line": end,
                        "preview": text[:400],
                    },
                )
            )
        self._client.upsert(collection_name=self.collection, points=points, wait=True)

    def _query(self, vec: list[float], limit: int):
        """Vector search, compatible with both new (query_points) and old (search) clients."""
        if hasattr(self._client, "query_points"):
            resp = self._client.query_points(
                collection_name=self.collection, query=vec, limit=limit, with_payload=True
            )
            return resp.points
        # Fallback for qdrant-client < 1.10
        return self._client.search(
            collection_name=self.collection, query_vector=vec, limit=limit
        )

    def search(self, query: str, limit: int = 10) -> list[SemanticHit]:
        if not self.available:
            return []
        try:
            vec = self._embed([query])[0]
            res = self._query(vec, limit)
        except Exception:
            return []
        hits: list[SemanticHit] = []
        for r in res:
            p = r.payload or {}
            hits.append(
                SemanticHit(
                    path=p.get("path", "?"),
                    start_line=int(p.get("start_line", 0)),
                    end_line=int(p.get("end_line", 0)),
                    score=float(r.score),
                    preview=p.get("preview", ""),
                )
            )
        return hits
