"""Semantic embedder backends.

The ApiEmbedder is tested with a mocked urlopen so no network is touched and no
API key is required. The SemanticIndex is tested for graceful degradation.
"""

import io
import json
import urllib.error

import pytest

from code_index.semantic import ApiEmbedder, EmbeddingResponseError, SemanticIndex


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _fake_openai_response(inputs):
    # Return a deterministic 4-dim vector per input (order preserved via index).
    data = [{"index": i, "embedding": [float(i), 1.0, 2.0, 3.0]} for i in range(len(inputs))]
    return {"data": data, "model": "fake"}


def test_api_embedder_batches_and_parses(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.setdefault("calls", []).append(len(body["input"]))
        return _FakeResp(json.dumps(_fake_openai_response(body["input"])).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    emb = ApiEmbedder(api_base="https://example/api/v1", model="m", api_key="k", batch=2)
    vecs = emb.embed(["a", "b", "c"])  # 3 inputs, batch=2 -> calls of 2 then 1

    assert len(vecs) == 3
    assert len(vecs[0]) == 4
    assert captured["calls"] == [2, 1]


def test_api_embedder_requires_key():
    with pytest.raises(RuntimeError):
        ApiEmbedder(api_base="https://x/v1", model="m", api_key=None)


def test_api_embedder_retries_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def flaky_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:  # fail twice, succeed on the 3rd attempt
            raise urllib.error.URLError("boom")
        body = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps(_fake_openai_response(body["input"])).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", flaky_urlopen)
    monkeypatch.setattr("code_index.semantic.time.sleep", lambda *_: None)

    emb = ApiEmbedder(api_base="https://x/v1", model="m", api_key="k", max_retries=3)
    vecs = emb.embed(["only"])
    assert len(vecs) == 1
    assert calls["n"] == 3


def test_api_embedder_gives_up_after_retries(monkeypatch):
    def always_fail(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", always_fail)
    monkeypatch.setattr("code_index.semantic.time.sleep", lambda *_: None)

    emb = ApiEmbedder(api_base="https://x/v1", model="m", api_key="k", max_retries=2)
    with pytest.raises(urllib.error.URLError):
        emb.embed(["x"])


def test_api_embedder_strips_trailing_slash():
    emb = ApiEmbedder(api_base="https://example/api/v1/", model="m", api_key="k")
    assert emb._url == "https://example/api/v1/embeddings"


# --- search path: short budget, no retries, LRU cache ------------------------


def test_embed_query_uses_search_timeout_not_index_timeout(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["timeout"] = timeout
        body = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps(_fake_openai_response(body["input"])).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    emb = ApiEmbedder(
        api_base="https://x/v1", model="m", api_key="k",
        timeout=60.0, search_timeout=15.0,
    )
    emb.embed_query("hello")
    # The query path must use the short search budget, never the 60s index one.
    assert seen["timeout"] == 15.0


def test_embed_query_does_not_retry_by_default(monkeypatch):
    calls = {"n": 0}

    def always_fail(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", always_fail)
    monkeypatch.setattr("code_index.semantic.time.sleep", lambda *_: None)
    # search_retries defaults to 0: a slow provider must fail after ONE attempt,
    # not stack multiple full timeouts past the MCP tool timeout.
    emb = ApiEmbedder(api_base="https://x/v1", model="m", api_key="k", max_retries=3)
    with pytest.raises(urllib.error.URLError):
        emb.embed_query("q")
    assert calls["n"] == 1


def test_embed_query_lru_cache_skips_api(monkeypatch):
    calls = {"n": 0}

    def counting(req, timeout=None):
        calls["n"] += 1
        body = json.loads(req.data.decode("utf-8"))
        return _FakeResp(json.dumps(_fake_openai_response(body["input"])).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", counting)
    emb = ApiEmbedder(api_base="https://x/v1", model="m", api_key="k", query_cache=8)
    v1 = emb.embed_query("same")
    v2 = emb.embed_query("same")  # served from cache, no second API call
    assert v1 == v2
    assert calls["n"] == 1
    emb.embed_query("other")  # different query -> one more call
    assert calls["n"] == 2


def test_make_embedder_plumbs_timeouts_from_settings():
    from code_index.semantic import _make_embedder

    class _S:
        embed_backend = "api"
        embed_api_base = "https://x/v1"
        embed_api_model = "m"
        embed_api_key = "k"
        embed_batch = 32
        embed_timeout = 30.0
        embed_max_retries = 3
        embed_concurrency = 4
        embed_search_timeout = 15.0
        embed_search_retries = 0
        embed_query_cache = 64

    emb = _make_embedder(_S())
    assert emb._timeout == 30.0
    assert emb._search_timeout == 15.0
    assert emb._search_retries == 0
    assert emb._query_cache_size == 64


# --- response validation: HTTP 200 but garbage body --------------------------


def _emb():
    return ApiEmbedder(api_base="https://x/v1", model="m", api_key="k", max_retries=0)


def test_parse_rejects_non_json():
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(b"<html>502 Bad Gateway</html>", expected=1)


def test_parse_rejects_count_mismatch():
    body = json.dumps({"data": [{"index": 0, "embedding": [1.0, 2.0]}]}).encode()
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(body, expected=2)  # asked for 2, got 1


def test_parse_rejects_missing_embedding_field():
    body = json.dumps({"data": [{"index": 0}]}).encode()
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(body, expected=1)


def test_parse_rejects_non_numeric_and_nan():
    bad_str = json.dumps({"data": [{"index": 0, "embedding": ["a", "b"]}]}).encode()
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(bad_str, expected=1)
    # NaN is valid JSON for Python's json module (allow_nan default) -> must be caught.
    nan_body = b'{"data": [{"index": 0, "embedding": [NaN, 1.0]}]}'
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(nan_body, expected=1)


def test_parse_rejects_inconsistent_dim():
    body = json.dumps({"data": [
        {"index": 0, "embedding": [1.0, 2.0, 3.0]},
        {"index": 1, "embedding": [1.0, 2.0]},
    ]}).encode()
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(body, expected=2)


def test_parse_surfaces_api_error_object():
    body = json.dumps({"error": {"message": "rate limited", "code": 429}}).encode()
    with pytest.raises(EmbeddingResponseError):
        _emb()._parse_response(body, expected=1)


def test_parse_accepts_valid_response_and_orders_by_index():
    body = json.dumps({"data": [
        {"index": 1, "embedding": [9.0, 9.0]},
        {"index": 0, "embedding": [1.0, 1.0]},
    ]}).encode()
    vecs = _emb()._parse_response(body, expected=2)
    assert vecs == [[1.0, 1.0], [9.0, 9.0]]  # reordered by 'index'


def test_bad_response_is_retried(monkeypatch):
    calls = {"n": 0}

    def flaky(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            return _FakeResp(b"not json")  # garbage once
        body = json.dumps({"data": [{"index": 0, "embedding": [1.0, 2.0]}]})
        return _FakeResp(body.encode())

    monkeypatch.setattr("urllib.request.urlopen", flaky)
    monkeypatch.setattr("code_index.semantic.time.sleep", lambda *_: None)
    emb = ApiEmbedder(api_base="https://x/v1", model="m", api_key="k", max_retries=2)
    vecs = emb.embed(["one"])
    assert vecs == [[1.0, 2.0]]
    assert calls["n"] == 2


def test_api_embedder_parallel_preserves_order(monkeypatch):
    # Each input encodes its global index in the first vector component so we can
    # assert that parallel sub-batches are reassembled in the original order.
    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        data = [
            {"index": i, "embedding": [float(int(txt)), 0.0]}
            for i, txt in enumerate(body["input"])
        ]
        return _FakeResp(json.dumps({"data": data}).encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    emb = ApiEmbedder(
        api_base="https://x/v1", model="m", api_key="k", batch=4, concurrency=4
    )
    texts = [str(i) for i in range(20)]  # 5 sub-batches of 4
    vecs = emb.embed(texts)

    assert len(vecs) == 20
    # First component encodes each input's GLOBAL index; getting 0,1,2,...,19
    # back in order proves parallel sub-batches are reassembled correctly.
    assert [v[0] for v in vecs] == [float(i) for i in range(20)]


class _Settings:
    """Minimal stand-in for config.Settings used by SemanticIndex."""

    def __init__(self):
        self.qdrant_url = "http://localhost:1"  # unreachable on purpose
        self.qdrant_api_key = None
        self.embed_backend = "api"
        self.embed_model = "x"
        self.embed_api_base = "https://example/api/v1"
        self.embed_api_model = "m"
        self.embed_api_key = None  # no key -> embedder creation fails
        self.embed_dim = 0

    def collection_name(self):
        return "test_collection"


def test_semantic_index_degrades_without_key():
    # No API key -> embedder construction raises -> SemanticIndex disables itself.
    idx = SemanticIndex(_Settings())
    assert idx.available is False
    assert idx.search("anything") == []


class _CountingEmbedder:
    """Records how many embed() calls and how big each batch was."""

    def __init__(self):
        self.batches: list[int] = []

    def embed(self, texts):
        self.batches.append(len(texts))
        return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts))]


class _FakeQdrant:
    def __init__(self):
        self.upserts: list[int] = []  # points per upsert call

    def upsert(self, collection_name, points, wait=False):
        self.upserts.append(len(points))


class _BatchSettings(_Settings):
    def __init__(self, batch):
        super().__init__()
        self.embed_api_key = "k"  # so a real embedder COULD build (we inject one)
        self.embed_batch = batch
        self.search_score = 0


def _make_index(batch, embedder, client, concurrency=1, upsert_batch=1000):
    """Build a SemanticIndex bypassing the Qdrant/embedder construction."""
    idx = SemanticIndex.__new__(SemanticIndex)
    idx.collection = "test"
    idx.available = True
    idx._dim = 4
    idx._batch = batch
    idx._concurrency = concurrency
    idx._flush_at = batch * concurrency
    idx._upsert_batch = upsert_batch
    idx._score = 0.0
    idx._buf = []
    idx._pending_delete = set()
    idx._delete_before_upsert = False  # behave like a full run (no deletes)
    idx._embedder = embedder
    idx._client = client
    idx._qdrant_url = "http://localhost:6333"
    idx.upsert_failures = 0
    idx.last_error = None
    return idx


def test_add_chunks_batches_across_files():
    emb = _CountingEmbedder()
    client = _FakeQdrant()
    # concurrency=1 so flush_at == batch (64): isolates the buffering behavior.
    idx = _make_index(batch=64, embedder=emb, client=client, concurrency=1)

    # 100 single-chunk files -> should NOT make 100 embed calls.
    for n in range(100):
        idx.add_chunks(f"f{n}.py", [(1, 10, f"content {n}")])
    idx.flush()

    # 100 chunks, batch 64 -> auto-flush at 64, then 36 left flushed at the end.
    # _CountingEmbedder.embed gets the whole buffer at once each flush.
    assert emb.batches == [64, 36]
    assert client.upserts == [64, 36]


def test_flush_is_noop_when_empty():
    emb = _CountingEmbedder()
    client = _FakeQdrant()
    idx = _make_index(batch=64, embedder=emb, client=client)
    idx.flush()
    assert emb.batches == []
    assert client.upserts == []


def test_flush_upserts_in_subbatches():
    # 200 chunks embedded in one go, but upserted in sub-batches of 64 so a
    # high-dim payload never becomes one huge (droppable) request body.
    emb = _CountingEmbedder()
    client = _FakeQdrant()
    idx = _make_index(batch=1000, embedder=emb, client=client, upsert_batch=64)
    for n in range(200):
        idx.add_chunks(f"f{n}.py", [(1, 10, f"c{n}")])
    idx.flush()
    # One embed call (buffer < flush_at), but upserts split 64/64/64/8.
    assert emb.batches == [200]
    assert client.upserts == [64, 64, 64, 8]
    assert idx.upsert_failures == 0


def test_flush_counts_upsert_failures_without_raising():
    class _FailingQdrant:
        def upsert(self, collection_name, points, wait=False):
            raise RuntimeError("WinError 10053")

    emb = _CountingEmbedder()
    idx = _make_index(batch=1000, embedder=emb, client=_FailingQdrant(), upsert_batch=64)
    for n in range(100):
        idx.add_chunks(f"f{n}.py", [(1, 10, f"c{n}")])
    idx.flush()  # must not raise
    assert idx.upsert_failures == 100
    assert idx.last_error and "WinError 10053" in idx.last_error


def test_flush_counts_embed_failures_when_embedder_fails():
    class _FailingEmbedder:
        def embed(self, texts):
            raise RuntimeError("embeddings API down")

    idx = _make_index(batch=1000, embedder=_FailingEmbedder(), client=_FakeQdrant())
    idx.embed_failures = 0
    idx.last_search_failed = False
    for n in range(30):
        idx.add_chunks(f"f{n}.py", [(1, 10, f"c{n}")])
    idx.flush()  # must not raise; chunks are counted as lost, not silently dropped
    assert idx.embed_failures == 30
    assert idx.last_error and "embeddings API down" in idx.last_error


def test_flush_counts_shortfall_when_embedder_returns_too_few():
    class _ShortEmbedder:
        def embed(self, texts):
            # Returns fewer vectors than inputs -> zip() would silently truncate.
            return [[float(i), 0.0, 0.0, 0.0] for i in range(len(texts) - 5)]

    idx = _make_index(batch=1000, embedder=_ShortEmbedder(), client=_FakeQdrant())
    idx.embed_failures = 0
    for n in range(20):
        idx.add_chunks(f"f{n}.py", [(1, 10, f"c{n}")])
    idx.flush()
    assert idx.embed_failures == 5


def test_search_sets_last_search_failed_on_error():
    class _BoomEmbedder:
        def embed(self, texts):
            raise RuntimeError("API unreachable")

    idx = _make_index(batch=64, embedder=_BoomEmbedder(), client=_FakeQdrant())
    idx.last_search_failed = False
    hits = idx.search("q", limit=5)
    assert hits == []
    assert idx.last_search_failed is True  # degraded, distinguishable from empty


def test_search_prefers_embed_query_when_available():
    # When the embedder exposes embed_query (the short search path), search()
    # must use it rather than the indexing embed() path.
    class _DualEmbedder:
        def __init__(self):
            self.embed_called = False
            self.query_called = False

        def embed(self, texts):
            self.embed_called = True
            return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            self.query_called = True
            return [0.1, 0.2, 0.3, 0.4]

    emb = _DualEmbedder()
    pts = [_Pt("a.py", 0.9)]
    idx = _make_index(batch=64, embedder=emb, client=_SearchQdrant(pts))
    idx.search("q", limit=5)
    assert emb.query_called is True
    assert emb.embed_called is False


def test_health_ok_and_unavailable():
    class _OkClient:
        def collection_exists(self, name):
            return True

        def count(self, collection_name, exact=False):
            class _C:
                count = 42
            return _C()

    idx = _make_index(batch=64, embedder=_CountingEmbedder(), client=_OkClient())
    h = idx.health()
    assert h["status"] == "ok" and h["points"] == 42

    idx.available = False
    assert idx.health()["status"] == "unavailable"


def test_health_warming_up_when_port_open_but_count_fails(monkeypatch):
    import code_index.semantic as sem_mod

    class _SlowClient:
        def count(self, collection_name, exact=False):
            raise RuntimeError("timed out")

    idx = _make_index(batch=64, embedder=_CountingEmbedder(), client=_SlowClient())
    # Port "open" -> Docker up but Qdrant not ready yet -> warming_up, not a hard
    # failure. This is the cold-start case that used to hang the tool.
    monkeypatch.setattr(sem_mod, "_port_is_open", lambda url, timeout=1.0: True)
    h = idx.health()
    assert h["status"] == "warming_up"
    assert h["points"] is None

    # Port closed -> Qdrant genuinely absent -> unavailable.
    monkeypatch.setattr(sem_mod, "_port_is_open", lambda url, timeout=1.0: False)
    assert idx.health()["status"] == "unavailable"


def test_health_missing_collection_is_unavailable_not_warming(monkeypatch):
    import code_index.semantic as sem_mod

    class _NoCollClient:
        def count(self, collection_name, exact=False):
            raise RuntimeError("Collection `test` doesn't exist!")

    idx = _make_index(batch=64, embedder=_CountingEmbedder(), client=_NoCollClient())
    # Even with the port open, a definite "no such collection" must not be
    # mistaken for a transient warm-up.
    monkeypatch.setattr(sem_mod, "_port_is_open", lambda url, timeout=1.0: True)
    h = idx.health()
    assert h["status"] == "unavailable"
    assert h["points"] == 0


class _Pt:
    def __init__(self, path, score):
        self.score = score
        self.payload = {"path": path, "start_line": 1, "end_line": 10, "preview": path}


class _QueryResp:
    def __init__(self, points):
        self.points = points


class _SearchQdrant:
    """Fake client returning a fixed result set for query_points."""

    def __init__(self, points):
        self._points = points

    def query_points(self, collection_name, query, limit, with_payload=True):
        return _QueryResp(self._points[:limit])


def test_search_path_glob_filters_results():
    pts = [
        _Pt("backend/svc.py", 0.9),
        _Pt("backend/tests/test_svc.py", 0.8),
        _Pt("frontend/app.tsx", 0.7),
    ]
    idx = _make_index(batch=64, embedder=_CountingEmbedder(), client=_SearchQdrant(pts))

    only_backend = idx.search("q", limit=10, path_glob=["backend/**"])
    assert {h.path for h in only_backend} == {"backend/svc.py", "backend/tests/test_svc.py"}

    no_tests = idx.search("q", limit=10, exclude_glob=["**/tests/**"])
    assert "backend/tests/test_svc.py" not in {h.path for h in no_tests}
    assert "backend/svc.py" in {h.path for h in no_tests}


def test_search_no_filter_returns_all():
    pts = [_Pt("a.py", 0.9), _Pt("b.py", 0.8)]
    idx = _make_index(batch=64, embedder=_CountingEmbedder(), client=_SearchQdrant(pts))
    hits = idx.search("q", limit=10)
    assert {h.path for h in hits} == {"a.py", "b.py"}
