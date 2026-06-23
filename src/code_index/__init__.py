"""code-index-mcp: self-hosted hybrid code search for Kilo CLI / OpenCode.

Layers:
- text:     SQLite FTS5 (exact strings, fast, cheap)
- symbols:  tree-sitter (function/class/method definitions, references-ish)
- semantic: fastembed (local embeddings) -> Qdrant (fuzzy meaning search)

Everything runs locally. No third-party MCP servers, no LSP.
"""

__version__ = "0.1.0"
