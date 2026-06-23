"""Semantic embedder backends.

The ApiEmbedder is tested with a mocked urlopen so no network is touched and no
API key is required. The SemanticIndex is tested for graceful degradation.
"""

import io
import json

import pytest

from code_index.semantic import ApiEmbedder, SemanticIndex


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


def test_api_embedder_strips_trailing_slash():
    emb = ApiEmbedder(api_base="https://example/api/v1/", model="m", api_key="k")
    assert emb._url == "https://example/api/v1/embeddings"


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
