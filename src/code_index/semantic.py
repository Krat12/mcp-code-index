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
import math
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol


@dataclass
class SemanticHit:
    path: str
    start_line: int
    end_line: int
    score: float
    preview: str


class EmbeddingResponseError(Exception):
    """The embeddings endpoint returned HTTP 200 but a malformed/unusable body.

    Distinct from network/HTTP errors: it means the *model/proxy* gave us junk
    (wrong shape, missing vectors, wrong count, non-finite numbers). We treat it
    as retryable (a flaky proxy may return an HTML error page once) but, if it
    persists, it surfaces clearly instead of silently dropping vectors.
    """


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class ApiEmbedder:
    """OpenAI-compatible /embeddings client over stdlib urllib (batched).

    Retries a failed request `max_retries` times with exponential backoff so a
    transient network/API blip does not abort a whole indexing run.
    """

    def __init__(
        self,
        api_base: str,
        model: str,
        api_key: str | None,
        batch: int = 64,
        timeout: float = 60.0,
        max_retries: int = 3,
        concurrency: int = 6,
    ) -> None:
        if not api_key:
            raise RuntimeError("CODE_INDEX_EMBED_API_KEY is not set for the api embed backend")
        self._url = api_base.rstrip("/") + "/embeddings"
        self._model = model
        self._key = api_key
        self._batch = batch
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._concurrency = max(1, concurrency)

    def _post_once(self, inputs: list[str]) -> list[list[float]]:
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
            raw = resp.read()
        return self._parse_response(raw, expected=len(inputs))

    def _parse_response(self, raw: bytes, expected: int) -> list[list[float]]:
        """Parse and VALIDATE an embeddings response body.

        Guards against HTTP-200-but-garbage: non-JSON bodies (proxy/HTML error
        pages), missing/short `data`, missing/empty `embedding`, non-numeric or
        non-finite (NaN/Inf) values, and inconsistent vector dimensions. Raises
        EmbeddingResponseError so the caller can retry / surface it rather than
        silently truncating via zip().
        """
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            snippet = raw[:120].decode("utf-8", errors="replace")
            raise EmbeddingResponseError(f"non-JSON embeddings response: {snippet!r}") from exc

        if not isinstance(body, dict):
            raise EmbeddingResponseError(f"embeddings response is not an object: {type(body).__name__}")
        # Surface an OpenAI-style error object instead of treating it as empty.
        if "data" not in body and isinstance(body.get("error"), (dict, str)):
            raise EmbeddingResponseError(f"embeddings API error: {body['error']}")
        data = body.get("data")
        if not isinstance(data, list):
            raise EmbeddingResponseError("embeddings response missing a 'data' list")
        if len(data) != expected:
            raise EmbeddingResponseError(
                f"embeddings count mismatch: got {len(data)} for {expected} inputs"
            )

        data = sorted(data, key=lambda d: d.get("index", 0) if isinstance(d, dict) else 0)
        out: list[list[float]] = []
        dim: int | None = None
        for i, d in enumerate(data):
            if not isinstance(d, dict) or "embedding" not in d:
                raise EmbeddingResponseError(f"embeddings[{i}] has no 'embedding' field")
            emb = d["embedding"]
            if not isinstance(emb, list) or not emb:
                raise EmbeddingResponseError(f"embeddings[{i}] is empty or not a list")
            try:
                vec = [float(x) for x in emb]
            except (TypeError, ValueError) as exc:
                raise EmbeddingResponseError(f"embeddings[{i}] has non-numeric values") from exc
            if any(not math.isfinite(x) for x in vec):
                raise EmbeddingResponseError(f"embeddings[{i}] contains NaN/Inf")
            if dim is None:
                dim = len(vec)
            elif len(vec) != dim:
                raise EmbeddingResponseError(
                    f"inconsistent vector dim: {len(vec)} vs {dim}"
                )
            out.append(vec)
        return out

    def _post(self, inputs: list[str]) -> list[list[float]]:
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return self._post_once(inputs)
            except Exception as exc:  # network/HTTP/parse errors -> retry
                last = exc
                if attempt < self._max_retries:
                    time.sleep(min(8.0, 0.5 * (2 ** attempt)))  # 0.5,1,2,4,...
        raise last if last is not None else RuntimeError("embeddings request failed")

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Split into sub-batches and run requests in parallel: the cost is API
        # latency (we just wait on the network), so this barely touches local
        # CPU/RAM yet cuts wall-time several-fold on big repos.
        sub_batches = [texts[i : i + self._batch] for i in range(0, len(texts), self._batch)]
        if len(sub_batches) <= 1 or self._concurrency == 1:
            out: list[list[float]] = []
            for sb in sub_batches:
                out.extend(self._post(sb))
            return out

        workers = min(self._concurrency, len(sub_batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # executor.map preserves input order, so vectors stay aligned.
            results = list(pool.map(self._post, sub_batches))
        out = []
        for r in results:
            out.extend(r)
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
        batch=getattr(settings, "embed_batch", 64),
        max_retries=getattr(settings, "embed_max_retries", 3),
        concurrency=getattr(settings, "embed_concurrency", 6),
    )


class SemanticIndex:
    """Pluggable embedder + qdrant-client. Degrades gracefully if unavailable."""

    def __init__(self, settings, embedder: Embedder | None = None) -> None:
        self.collection = settings.collection_name()
        self.available = True
        self._dim: int | None = settings.embed_dim or None
        self._batch: int = max(1, int(getattr(settings, "embed_batch", 64)))
        self._concurrency: int = max(1, int(getattr(settings, "embed_concurrency", 6)))
        # Accumulate enough chunks to feed every parallel worker before flushing.
        self._flush_at: int = self._batch * self._concurrency
        # Points per Qdrant upsert (small: 4096-dim vectors -> big request body).
        self._upsert_batch: int = max(1, int(getattr(settings, "upsert_batch", 64)))
        self._score: float = float(getattr(settings, "search_score", 0) or 0)
        # Diagnostics for the run (surfaced in the index report / status / UI):
        #   upsert_failures - points Qdrant refused (e.g. wrong dim, dropped body)
        #   embed_failures  - chunks lost because the embeddings API kept failing
        self.upsert_failures: int = 0
        self.embed_failures: int = 0
        self.last_error: str | None = None
        # Set True when the most recent search() raised (degraded, not empty).
        self.last_search_failed: bool = False
        # Cross-file buffer of pending chunks: (path, start, end, text).
        self._buf: list[tuple[str, int, int, str]] = []
        # Paths whose stale vectors must be deleted before the next upsert
        # (incremental runs only; a full run recreates the collection instead).
        self._pending_delete: set[str] = set()
        self._delete_before_upsert: bool = True
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
            self._create()

    def _create(self) -> None:
        from qdrant_client import models

        self._client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=self._dim or 384, distance=models.Distance.COSINE
            ),
        )

    def begin(self, full: bool) -> None:
        """Prepare for an indexing run.

        full=True  -> recreate the collection once (no per-file deletes needed).
        full=False -> keep the collection; stale vectors of changed/removed
                      files are batch-deleted just before the matching upsert.
        """
        if not self.available:
            return
        if full:
            self._delete_before_upsert = False
            self.recreate_collection()
        else:
            self._delete_before_upsert = True

    def recreate_collection(self) -> None:
        """Drop and recreate the collection (used for a `--full` rebuild).

        One cheap operation instead of a per-file delete for every path; the
        whole collection is being repopulated anyway.
        """
        if not self.available:
            return
        if self._dim is None:
            # Need a vector size before (re)creating; probe once.
            try:
                self._embed(["dimension probe"])
            except Exception:
                self.available = False
                return
        try:
            if self._client.collection_exists(self.collection):
                self._client.delete_collection(self.collection)
            self._create()
        except Exception:
            self.available = False

    def delete_path(self, path: str) -> None:
        self.delete_paths([path])

    def delete_paths(self, paths: list[str]) -> None:
        """Delete all points for the given paths in ONE request."""
        if not self.available or not paths:
            return
        from qdrant_client import models

        try:
            # wait=True so a later (wait=False) upsert of the same deterministic
            # point ids can never be clobbered by a delete that lands afterwards.
            self._client.delete(
                collection_name=self.collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="path", match=models.MatchAny(any=list(paths))
                            )
                        ]
                    )
                ),
                wait=True,
            )
        except Exception:
            pass

    def add_chunks(self, path: str, chunks: list[tuple[int, int, str]]) -> None:
        """Queue a file's chunks; flush automatically once the buffer is full.

        chunks: list of (start_line, end_line, text). Embedding+upsert happen in
        `flush()` so chunks from MANY files share one API request / upsert.
        """
        if not self.available or not chunks:
            return
        if self._delete_before_upsert:
            self._pending_delete.add(path)
        for start, end, text in chunks:
            self._buf.append((path, start, end, text))
        if len(self._buf) >= self._flush_at:
            self.flush()

    def flush(self) -> None:
        """Embed and upsert all buffered chunks (no-op if the buffer is empty)."""
        # Drop stale vectors for changed paths first (incremental runs).
        if self._pending_delete:
            self.delete_paths(list(self._pending_delete))
            self._pending_delete.clear()
        if not self.available or not self._buf:
            return
        from qdrant_client import models

        pending = self._buf
        self._buf = []
        texts = [c[3] for c in pending]
        try:
            vectors = self._embed(texts)
        except Exception as exc:
            # Embeddings API kept failing after retries -> these chunks are lost
            # for this run. Count them (don't silently drop) so the report/UI can
            # warn that the semantic layer is incomplete.
            self.embed_failures += len(pending)
            self.last_error = f"{type(exc).__name__}: {exc}"
            return
        # A short/mismatched vector batch would silently drop chunks via zip();
        # count any shortfall instead.
        if len(vectors) < len(pending):
            self.embed_failures += len(pending) - len(vectors)
        points = []
        for (path, start, end, text), vec in zip(pending, vectors):
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
        # Upsert in small sub-batches: high-dim vectors make a few hundred points
        # a multi-MB body that Qdrant can drop mid-stream (WinError 10053).
        for i in range(0, len(points), self._upsert_batch):
            sub = points[i : i + self._upsert_batch]
            try:
                self._client.upsert(collection_name=self.collection, points=sub, wait=False)
            except Exception as exc:
                # Don't kill the whole run, but DON'T swallow silently either:
                # count the loss and remember the last error for diagnostics.
                self.upsert_failures += len(sub)
                self.last_error = f"{type(exc).__name__}: {exc}"

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

    def health(self) -> dict:
        """Cheap liveness probe of the semantic backend (for status/diagnostics).

        Returns {"status": "ok"|"unavailable", "collection": ..., "points": int|None,
        "error": str|None}. Never raises. "unavailable" means the embedder/Qdrant
        could not be reached, so semantic search will return degraded results.
        """
        if not self.available:
            return {"status": "unavailable", "collection": self.collection,
                    "points": None, "error": self.last_error or "embedder/Qdrant unavailable"}
        try:
            if not self._client.collection_exists(self.collection):
                return {"status": "unavailable", "collection": self.collection,
                        "points": 0, "error": "collection does not exist yet"}
            info = self._client.count(collection_name=self.collection, exact=False)
            points = getattr(info, "count", None)
            return {"status": "ok", "collection": self.collection,
                    "points": points, "error": None}
        except Exception as exc:
            return {"status": "unavailable", "collection": self.collection,
                    "points": None, "error": f"{type(exc).__name__}: {exc}"}

    def search(
        self,
        query: str,
        limit: int = 10,
        path_glob: list[str] | None = None,
        exclude_glob: list[str] | None = None,
    ) -> list[SemanticHit]:
        if not self.available:
            return []
        from .walker import PathFilter

        pf = PathFilter(path_glob, exclude_glob)
        # Over-fetch when filtering so post-filtering still fills `limit`.
        fetch = limit * 8 if pf else limit
        self.last_search_failed = False
        try:
            vec = self._embed([query])[0]
            res = self._query(vec, fetch)
        except Exception as exc:
            # Distinguish "search failed" (API/Qdrant down) from "no matches" so
            # the caller can tell the agent the layer is degraded, not empty.
            self.last_search_failed = True
            self.last_error = f"{type(exc).__name__}: {exc}"
            return []
        hits: list[SemanticHit] = []
        for r in res:
            score = float(r.score)
            if self._score and score < self._score:
                continue
            p = r.payload or {}
            path = p.get("path", "?")
            if pf and not pf.match(path):
                continue
            hits.append(
                SemanticHit(
                    path=path,
                    start_line=int(p.get("start_line", 0)),
                    end_line=int(p.get("end_line", 0)),
                    score=score,
                    preview=p.get("preview", ""),
                )
            )
            if len(hits) >= limit:
                break
        return hits
