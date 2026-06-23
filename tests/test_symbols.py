"""Symbol extraction via tree-sitter, resilient to binding API differences.

These tests are skipped if tree-sitter isn't installed (symbols layer is
optional; text + semantic still work without it).
"""

import pytest

from code_index.symbols import SymbolExtractor


@pytest.fixture(scope="module")
def extractor():
    ex = SymbolExtractor()
    if not ex.available:
        pytest.skip("tree-sitter not installed")
    return ex


def test_python_symbols(extractor):
    src = "def foo():\n    pass\n\nclass Bar:\n    def m(self):\n        pass\n"
    syms = {(s.kind, s.name) for s in extractor.extract("python", src)}
    assert ("function", "foo") in syms
    assert ("class", "Bar") in syms
    assert ("method", "m") in syms or ("function", "m") in syms


def test_typescript_symbols(extractor):
    src = "export function calc(a: number) { return a }\nexport class Svc {}\ninterface IFoo {}\n"
    syms = {(s.kind, s.name) for s in extractor.extract("typescript", src)}
    assert ("function", "calc") in syms
    assert ("class", "Svc") in syms
    assert ("interface", "IFoo") in syms


def test_java_symbols_including_record(extractor):
    src = (
        "public class Billing {\n"
        "    public void charge() {}\n"
        "    public record Money(long cents) {}\n"
        "    interface Repo {}\n"
        "}\n"
    )
    syms = {(s.kind, s.name) for s in extractor.extract("java", src)}
    assert ("class", "Billing") in syms
    assert ("method", "charge") in syms
    assert ("record", "Money") in syms
    assert ("interface", "Repo") in syms


def test_line_numbers_are_1_based(extractor):
    src = "x = 1\ndef foo():\n    pass\n"
    syms = extractor.extract("python", src)
    foo = next(s for s in syms if s.name == "foo")
    assert foo.start_line == 2


def test_unknown_language_returns_empty(extractor):
    assert extractor.extract("no_such_lang", "whatever") == []
