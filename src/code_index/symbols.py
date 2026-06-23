"""Symbol extraction via tree-sitter (no LSP, no Serena).

We parse each supported file and collect definitions (functions, classes,
methods, etc.) using a small per-language set of node types. This is good
enough for "jump to definition of X" / "list symbols of file" without a
language server.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import LANG_BY_EXT

# tree-sitter node types that represent a "definition" worth indexing,
# keyed by language. The grammar names follow tree-sitter-language-pack.
DEF_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "class_declaration", "method_definition", "generator_function_declaration"},
    "typescript": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
    },
    "tsx": {
        "function_declaration", "class_declaration", "method_definition",
        "interface_declaration", "type_alias_declaration", "enum_declaration",
    },
    "rust": {"function_item", "struct_item", "enum_item", "trait_item", "impl_item", "mod_item"},
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "java": {
        "class_declaration", "method_declaration", "interface_declaration",
        "enum_declaration", "record_declaration", "constructor_declaration",
        "annotation_type_declaration",
    },
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
    "c": {"function_definition", "struct_specifier", "enum_specifier"},
    "cpp": {"function_definition", "class_specifier", "struct_specifier", "enum_specifier"},
    "c_sharp": {"method_declaration", "class_declaration", "interface_declaration", "struct_declaration", "enum_declaration"},
    "ruby": {"method", "class", "module"},
    "php": {"function_definition", "class_declaration", "method_declaration", "interface_declaration"},
    "lua": {"function_declaration"},
}

# Map node type -> human label shown in results.
KIND_LABEL: dict[str, str] = {
    "function_definition": "function",
    "function_declaration": "function",
    "function_item": "function",
    "method_declaration": "method",
    "method_definition": "method",
    "method": "method",
    "class_definition": "class",
    "class_declaration": "class",
    "class_specifier": "class",
    "struct_item": "struct",
    "struct_specifier": "struct",
    "struct_declaration": "struct",
    "interface_declaration": "interface",
    "trait_item": "trait",
    "enum_item": "enum",
    "enum_specifier": "enum",
    "enum_declaration": "enum",
    "type_alias_declaration": "type",
    "type_declaration": "type",
    "impl_item": "impl",
    "mod_item": "module",
    "module": "module",
    "object_declaration": "object",
    "record_declaration": "record",
    "constructor_declaration": "constructor",
    "annotation_type_declaration": "annotation",
}


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int  # 1-based
    end_line: int


def lang_for_ext(ext: str) -> str | None:
    return LANG_BY_EXT.get(ext.lower())


# ---------------------------------------------------------------------------
# tree-sitter API adapter.
#
# Different tree-sitter bindings expose nodes differently:
#   * classic py-tree-sitter: node.type, node.children, node.start_point (props)
#   * some 0.25+/language-pack builds: node.kind(), node.child_count(),
#     node.start_position() (methods), node.root_node() callable, etc.
# These helpers normalize both so SymbolExtractor works regardless of build.
# ---------------------------------------------------------------------------
def _call(v):
    return v() if callable(v) else v


def _node_kind(node) -> str:
    if hasattr(node, "kind"):
        return _call(node.kind)
    return _call(node.type)


def _node_children(node) -> list:
    ch = getattr(node, "children", None)
    val = ch() if callable(ch) else ch
    if val is not None:
        return list(val)
    count = _call(node.child_count)
    return [node.child(i) for i in range(count)]


def _node_start_line(node) -> int:
    if hasattr(node, "start_position"):
        return _call(node.start_position).row + 1
    return _call(node.start_point)[0] + 1


def _node_end_line(node) -> int:
    if hasattr(node, "end_position"):
        return _call(node.end_position).row + 1
    return _call(node.end_point)[0] + 1


def _node_bytes(node) -> tuple[int, int]:
    return _call(node.start_byte), _call(node.end_byte)


def _root_node(tree):
    return _call(tree.root_node)


class SymbolExtractor:
    """Lazily loads tree-sitter parsers per language and extracts definitions."""

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}
        self._available = True
        try:
            from tree_sitter_language_pack import get_parser  # noqa: F401
        except Exception:
            # tree-sitter not installed -> symbols layer disabled, text/semantic still work.
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def _parser(self, lang: str):
        if lang in self._parsers:
            return self._parsers[lang]
        from tree_sitter_language_pack import get_parser

        try:
            parser = get_parser(lang)
        except Exception:
            parser = None
        self._parsers[lang] = parser
        return parser

    def extract(self, lang: str, source: str) -> list[Symbol]:
        if not self._available:
            return []
        def_types = DEF_NODE_TYPES.get(lang)
        if not def_types:
            return []
        parser = self._parser(lang)
        if parser is None:
            return []

        data = source.encode("utf-8")
        tree = None
        # Some bindings want bytes, others want str. Try both.
        for candidate in (data, source):
            try:
                tree = parser.parse(candidate)
                break
            except TypeError:
                continue
            except Exception:
                return []
        if tree is None:
            return []

        out: list[Symbol] = []
        try:
            self._walk(_root_node(tree), data, def_types, out)
        except Exception:
            return out
        return out

    def _walk(self, node, data: bytes, def_types: set[str], out: list[Symbol]) -> None:
        kind = _node_kind(node)
        if kind in def_types:
            name = self._name_of(node, data)
            if name:
                out.append(
                    Symbol(
                        name=name,
                        kind=KIND_LABEL.get(kind, kind),
                        start_line=_node_start_line(node),
                        end_line=_node_end_line(node),
                    )
                )
        for child in _node_children(node):
            self._walk(child, data, def_types, out)

    @staticmethod
    def _name_of(node, data: bytes) -> str | None:
        """Best-effort name resolution: prefer a child named 'name', else first identifier."""
        name_node = None
        if hasattr(node, "child_by_field_name"):
            try:
                name_node = node.child_by_field_name("name")
            except Exception:
                name_node = None
        if name_node is not None:
            s, e = _node_bytes(name_node)
            return data[s:e].decode("utf-8", "replace")
        # Fallback: scan immediate children for an identifier-ish node.
        for child in _node_children(node):
            ckind = _node_kind(child)
            if "identifier" in ckind or ckind in {"name", "type_identifier", "field_identifier"}:
                s, e = _node_bytes(child)
                return data[s:e].decode("utf-8", "replace")
        return None
