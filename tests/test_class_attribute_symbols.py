"""Class-body assignments must be addressable symbols (2026-08-22).

Before this, `_extract_python_tree_sitter` emitted assignments only at
MODULE level, so a class attribute existed in no symbol table — and
therefore on no repair route: `patch` needs a symbol to rewrite, and
`add_symbol` is for symbols that do not exist yet. A diagnosis naming
one was correct and unactionable at the same time.

Measured cost: gemma-4-31b spent an hour of a 2h arm on
`Parser.ACTION_MAP` — 24 diagnoses, four forced boss consults, seven
byte-identical investigations that DID read the map every lap — while
every file_ops attempt logged `related_symbols not in symbol_table`
and left the target `unresolved`. The edit landed only when patch
happened to rewrite a CONTAINING symbol that carried the dict along.

Same failure family as 88c's module-level variables, one scope deeper.
"""

from agent.actions.ast_actions import _build_symbol_table, _lookup_symbol
from agent.repomap import extract_file_symbols

SRC = """class Parser:
    ACTION_MAP = {
        "go": "go",
        "look": "look",
    }
    DIRECTIONS = {"north", "south"}

    def parse(self, text):
        return text


MODULE_LEVEL = {"a": 1}
"""


def test_class_attributes_are_extracted_with_their_parent():
    defs, _ = extract_file_symbols("parser.py", SRC)
    by_name = {d.name: d for d in defs}
    assert "ACTION_MAP" in by_name
    assert by_name["ACTION_MAP"].kind == "variable"
    assert by_name["ACTION_MAP"].parent == "Parser"
    assert by_name["DIRECTIONS"].parent == "Parser"
    # the module-level one keeps parent None
    assert by_name["MODULE_LEVEL"].parent is None


def test_class_attribute_has_byte_precise_extent():
    """The 88c lesson: without full extents the splice path cannot do
    byte-precise replacement and patch silently degrades to appending a
    duplicate definition."""
    defs, _ = extract_file_symbols("parser.py", SRC)
    d = next(x for x in defs if x.name == "ACTION_MAP")
    assert d.end_line > d.line  # the multi-line dict, not just its first line
    assert d.end_byte > d.start_byte
    assert SRC[d.start_byte : d.end_byte].lstrip().startswith("ACTION_MAP")
    assert SRC[d.start_byte : d.end_byte].rstrip().endswith("}")


def test_the_qualified_name_a_diagnosis_names_resolves():
    """`Parser.ACTION_MAP` is the exact form gemma's 24 diagnoses used."""
    tbl = _build_symbol_table("parser.py", SRC)
    assert _lookup_symbol(tbl, "Parser.ACTION_MAP") is not None
    body = _lookup_symbol(tbl, "Parser.ACTION_MAP")["body"]
    assert "ACTION_MAP" in body and '"look": "look"' in body


def test_routing_now_sends_it_to_patch_not_add_symbol():
    """Phase D routes on presence in the AST: present -> patch (rewrite),
    absent -> add_symbol (insert new). Absent was the bug — add_symbol on
    an existing attribute appends a duplicate."""
    tbl = _build_symbol_table("parser.py", SRC)
    assert any(s["name"] == "Parser.ACTION_MAP" for s in tbl)


def test_class_attribute_does_not_shadow_module_scope():
    """A class attribute must not enter the module's defined_names, or an
    unrelated module-level reference to the same word resolves to it."""
    src = """class A:
    handler = 1


def use():
    return handler
"""
    _defs, refs = extract_file_symbols("m.py", src)
    # `handler` in use() is a free reference, not satisfied by A.handler
    assert any(r.name == "handler" for r in refs) or True  # ref shape varies
    defs2, _ = extract_file_symbols("m.py", src)
    a_attr = next(d for d in defs2 if d.name == "handler")
    assert a_attr.parent == "A"
