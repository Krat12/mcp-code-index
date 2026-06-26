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
import os
import platform
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

# qdrant_client pulls in fastembed -> onnxruntime, which calls platform.uname()
# at import time. On Windows boxes with a broken WMI provider that call hangs
# forever, deadlocking the first `import qdrant_client`. Pre-seed the uname cache
# from env-only values (node()/release()/version() would recurse back into the
# same WMI query) so the probe is never issued. Harmless when WMI works.
if sys.platform == "win32" and getattr(platform, "_uname_cache", None) is None:
    platform._uname_cache = platform.uname_result(
        "Windows",
        os.environ.get("COMPUTERNAME", "host"),
        "10",  # release: satisfy onnxruntime's ">= Windows 10" version check
        "",
        os.environ.get("PROCESSOR_ARCHITECTURE", ""),
    )


def _port_is_open(url: str, timeout: float = 1.0) -> bool:
    """True if a TCP connection to the URL's host:port succeeds quickly.

    Used to tell "Docker is up but Qdrant is still warming up" (port open, API
    not ready yet) from "Qdrant isn't there at all" (connection refused). A
    cheap stdlib check; never raises.
    """
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname or "localhost"
        port = parsed.port or (443 if parsed.scheme == "https" else 6333)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


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
        search_timeout: float | None = None,
        search_retries: int = 0,
        query_cache: int = 0,
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
        # Search path: a short budget + (by default) zero retries so a slow
        # provider degrades fast instead of hanging the interactive MCP tool.
        self._search_timeout = float(search_timeout) if search_timeout else timeout
        self._search_retries = max(0, search_retries)
        # Tiny in-memory LRU so repeated identical queries skip the API.
        self._query_cache_size = max(0, query_cache)
        self._query_cache: OrderedDict[str, list[float]] = OrderedDict()

    def _post_once(self, inputs: list[str], timeout: float | None = None) -> list[list[float]]:
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
        with urllib.request.urlopen(req, timeout=timeout or self._timeout) as resp:
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

    def embed_query(self, text: str) -> list[float]:
        """Embed ONE search query with the short search budget and no retries.

        Separate from `embed` (the indexing path) so interactive search never
        inherits the long indexing timeout/retries. An LRU cache short-circuits
        repeated identical queries. Raises on timeout/error so the caller marks
        the search degraded rather than silently empty.
        """
        if self._query_cache_size and text in self._query_cache:
            self._query_cache.move_to_end(text)
            return self._query_cache[text]
        vec: list[float] | None = None
        last: Exception | None = None
        for attempt in range(self._search_retries + 1):
            try:
                vec = self._post_once([text], timeout=self._search_timeout)[0]
                break
            except Exception as exc:
                last = exc
                if attempt < self._search_retries:
                    time.sleep(min(2.0, 0.5 * (2 ** attempt)))
        if vec is None:
            raise last if last is not None else RuntimeError("query embedding failed")
        if self._query_cache_size:
            self._query_cache[text] = vec
            self._query_cache.move_to_end(text)
            while len(self._query_cache) > self._query_cache_size:
                self._query_cache.popitem(last=False)
        return vec


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
        timeout=getattr(settings, "embed_timeout", 60.0),
        max_retries=getattr(settings, "embed_max_retries", 3),
        concurrency=getattr(settings, "embed_concurrency", 6),
        search_timeout=getattr(settings, "embed_search_timeout", None),
        search_retries=getattr(settings, "embed_search_retries", 0),
        query_cache=getattr(settings, "embed_query_cache", 0),
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
        self._qdrant_url = getattr(settings, "qdrant_url", "")
        try:
            # Build the embedder first: a missing API key (or other bad config)
            # must fail BEFORE importing qdrant_client, whose dependency chain
            # (fastembed -> onnxruntime) calls platform.uname() and can hang on a
            # broken WMI provider during a cold start.
            self._embedder: Embedder = embedder or _make_embedder(settings)
            from qdrant_client import QdrantClient

            kwargs: dict = {
                "url": settings.qdrant_url,
                "api_key": settings.qdrant_api_key,
                # Skip the version-compatibility round-trip in the constructor:
                # it does a network call that can also hang during a cold start.
                "check_compatibility": False,
            }
            # Hard per-request timeout so a warming-up Qdrant fails fast instead
            # of hanging past the MCP client's tool timeout (0 = client default).
            timeout = getattr(settings, "qdrant_timeout", 0) or 0
            if timeout:
                kwargs["timeout"] = int(max(1, round(timeout)))
            self._client = QdrantClient(**kwargs)
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

    def _embed_query(self, query: str) -> list[float]:
        # Use the embedder's dedicated search path (short timeout, no retries,
        # LRU cache) when available (ApiEmbedder); local embedders are fast, so
        # the plain embed() path is fine for them.
        eq = getattr(self._embedder, "embed_query", None)
        vec = eq(query) if callable(eq) else self._embed([query])[0]
        if vec and self._dim is None:
            self._dim = len(vec)
        return vec

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

    def delete_paths(self, paths: list[str], batch: int = 200) -> None:
        """Delete all points for the given paths, in chunked requests.

        A single MatchAny over thousands of paths makes one huge filter that is
        slow for Qdrant to evaluate and a big request body; chunking keeps each
        delete cheap (matches the batched SQLite deletion on the indexer side).
        """
        if not self.available or not paths:
            return
        from qdrant_client import models

        unique = list(dict.fromkeys(paths))
        batch = max(1, int(batch))
        for i in range(0, len(unique), batch):
            chunk = unique[i : i + batch]
            try:
                # wait=True so a later (wait=False) upsert of the same
                # deterministic point ids can never be clobbered by a delete
                # that lands afterwards.
                self._client.delete(
                    collection_name=self.collection,
                    points_selector=models.FilterSelector(
                        filter=models.Filter(
                            must=[
                                models.FieldCondition(
                                    key="path", match=models.MatchAny(any=chunk)
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

        Returns {"status": ..., "collection": ..., "points": int|None,
        "error": str|None}. Never raises. Status is one of:
          "ok"          - collection reachable; `points` is its size.
          "warming_up"  - the request failed/timed out BUT the Qdrant port is
                          open (Docker up, service still starting). Transient:
                          retry shortly. Distinct so callers don't report a hard
                          failure during a cold start.
          "unavailable" - the embedder/Qdrant could not be reached at all.
        """
        if not self.available:
            return {"status": "unavailable", "collection": self.collection,
                    "points": None, "error": self.last_error or "embedder/Qdrant unavailable"}
        try:
            # One bounded call (the client carries qdrant_timeout). count() also
            # implicitly verifies the collection exists.
            info = self._client.count(collection_name=self.collection, exact=False)
            points = getattr(info, "count", None)
            return {"status": "ok", "collection": self.collection,
                    "points": points, "error": None}
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}"
            # A missing collection is a definite (not transient) answer.
            if "doesn't exist" in str(exc).lower() or "not found" in str(exc).lower():
                return {"status": "unavailable", "collection": self.collection,
                        "points": 0, "error": "collection does not exist yet"}
            # Otherwise: if the port is open, Qdrant is likely still warming up.
            if _port_is_open(self._qdrant_url):
                return {"status": "warming_up", "collection": self.collection,
                        "points": None, "error": f"Qdrant not ready yet ({err})"}
            return {"status": "unavailable", "collection": self.collection,
                    "points": None, "error": err}

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
            vec = self._embed_query(query)
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
