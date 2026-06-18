"""Drift tripwire for the consolidated agent/languages.py registry.

Embeds its OWN copies of the 8 retired maps (so it survives their deletion) and
asserts the registry reproduces each one EXACTLY — the consolidation is
behavior-preserving, not behavior-correcting. If a future edit drifts a value or
forgets an extension, this fails loudly.
"""

from __future__ import annotations

import ast
import pathlib

from agent import languages as L

# ── Retired-map fixtures (verbatim copies; NOT imports of the originals) ──
OLD_LANG_BY_EXT = {
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".go": "go", ".rb": "ruby", ".rs": "rust", ".java": "java",
}
OLD_DEF_NODE_KINDS = {
    "bash": {"function_definition": "function"},
    "javascript": {"function_declaration": "function", "generator_function_declaration": "function",
                   "class_declaration": "class", "method_definition": "method"},
    "typescript": {"function_declaration": "function", "generator_function_declaration": "function",
                   "class_declaration": "class", "interface_declaration": "class",
                   "method_definition": "method", "method_signature": "method"},
    "tsx": {"function_declaration": "function", "class_declaration": "class",
            "interface_declaration": "class", "method_definition": "method"},
    "go": {"function_declaration": "function", "method_declaration": "method",
           "type_declaration": "class"},
    "ruby": {"method": "method", "singleton_method": "method", "class": "class", "module": "module"},
    "rust": {"function_item": "function", "struct_item": "class", "enum_item": "class",
             "trait_item": "class"},
    "java": {"method_declaration": "method", "constructor_declaration": "method",
             "class_declaration": "class", "interface_declaration": "class"},
}
OLD_LANGUAGE_EXTENSIONS = {
    "python": "py", "javascript": "js", "typescript": "ts", "rust": "rs", "go": "go", "ruby": "rb",
    "java": "java", "swift": "swift", "kotlin": "kt", "scala": "scala", "bash": "sh", "shell": "sh",
    "sh": "sh", "yaml": "yaml", "toml": "toml", "json": "json", "markdown": "md", "md": "md",
    "html": "html", "css": "css",
}
OLD_EXTENSION_FENCE = {
    "py": "python", "js": "javascript", "ts": "typescript", "rb": "ruby", "rs": "rust",
    "yml": "yaml", "md": "markdown", "sh": "bash",
}
OLD_FRAME_LANG = {
    ".py": ("Python file", "python"),
    ".sh": ("shell script", "bash"), ".bash": ("shell script", "bash"), ".zsh": ("shell script", "bash"),
    ".js": ("JavaScript file", "javascript"), ".mjs": ("JavaScript file", "javascript"),
    ".cjs": ("JavaScript file", "javascript"), ".jsx": ("JavaScript file", "javascript"),
    ".ts": ("TypeScript file", "typescript"), ".tsx": ("TypeScript file", "tsx"),
    ".go": ("Go file", "go"), ".rb": ("Ruby file", "ruby"), ".rs": ("Rust file", "rust"),
    ".java": ("Java file", "java"),
}
OLD_SOURCE_EXTENSIONS = {"py", "js", "ts", "jsx", "tsx", "rs", "go", "rb", "java",
                         "kt", "kts", "swift", "dart", "ex", "exs", "php"}
OLD_DATA_EXTENSIONS = {"yaml", "yml", "json", "toml"}
OLD_DATA_PATCH_EXTS = {"yaml", "yml"}


# ── Parity ──
def test_grammar_for_ext_reproduces_lang_by_ext():
    for ext, grammar in OLD_LANG_BY_EXT.items():
        assert L.grammar_for_ext(ext) == grammar
    assert L.grammar_for_ext(".unknownext") is None
    # .py is special-cased in repomap's dispatch (absent from _LANG_BY_EXT); the
    # catalog still knows Python has a grammar — harmless, dispatch handles .py first.
    assert L.grammar_for_ext(".py") == "python"


def test_def_node_kinds_reproduces_old():
    for grammar, kinds in OLD_DEF_NODE_KINDS.items():
        assert L.def_node_kinds_for_grammar(grammar) == kinds
    assert L.def_node_kinds_for_grammar("python") == {}   # no _DEF_NODE_KINDS["python"]
    assert L.def_node_kinds_for_grammar("nope") == {}


def test_fence_for_ext_reproduces_old():
    for ext, fence in OLD_EXTENSION_FENCE.items():
        assert L.fence_for_ext(ext) == fence
    # extensions absent from the map fall back to themselves — incl. the jsx
    # idiosyncrasy (jsx fences as "jsx" here, not "javascript").
    for ext in ("go", "java", "yaml", "json", "toml", "jsx", "mjs"):
        assert L.fence_for_ext(ext) == ext


def test_ext_for_name_reproduces_old():
    for name, ext in OLD_LANGUAGE_EXTENSIONS.items():
        assert L.ext_for_name(name) == ext
    assert L.ext_for_name("cobol") == "cobol"   # unknown name → itself


def test_frame_label_and_fence_reproduces_old():
    for ext, (label, fence) in OLD_FRAME_LANG.items():
        assert L.frame_label_and_fence(f"a/b{ext}") == (label, fence)
    # extensions not in _FRAME_LANG → the ('file','') default
    for ext in (".swift", ".yaml", ".json", ".unknownext"):
        assert L.frame_label_and_fence(f"x{ext}") == ("file", "")


def test_classification_sets_reproduced():
    cand = (OLD_SOURCE_EXTENSIONS | OLD_DATA_EXTENSIONS
            | {"sh", "scala", "mjs", "cjs", "md", "html", "css", "txt"})
    assert {e for e in cand if L.is_source(e)} == OLD_SOURCE_EXTENSIONS
    assert {e for e in cand if L.is_data(e)} == OLD_DATA_EXTENSIONS
    assert {e for e in cand if L.is_data_patch(e)} == OLD_DATA_PATCH_EXTS
    # the documented irregularities are preserved:
    assert L.is_source("sh") is False and L.is_source("scala") is False
    assert L.is_source("mjs") is False and L.is_source("php") is True
    assert L.is_data_patch("json") is False and L.is_data("json") is True


def test_keying_dot_normalization():
    assert L.by_ext(".py") is L.by_ext("py") is L.by_ext(".PY")
    assert L._norm_ext(".PY") == "py" and L._norm_ext("py") == "py" and L._norm_ext("") == ""
    assert L.spec_for_path("a/b.PY").name == "python"
    assert L.spec_for_path("noext") is None
    assert L.by_name("python").name == "python" and L.by_name("nope") is None


def test_registry_is_a_dependency_light_leaf():
    """No imports from agent.* or tree_sitter* — it's imported everywhere, a cycle
    or a heavy dep would be fatal."""
    src = pathlib.Path(L.__file__).read_text()
    tree = ast.parse(src)
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
    bad = [m for m in mods if m.split(".")[0] in {"agent", "tree_sitter", "tree_sitter_language_pack"}]
    assert bad == [], f"agent/languages.py must stay a stdlib-only leaf; found: {bad}"


# ── module_fix fields (comment_prefix / module_fix_placement) ──
def test_comment_prefix_populated_for_every_grammar_spec():
    # Drives the frame-editor sentinel; must be a real comment in every language.
    assert all(s.comment_prefix for s in L.LANGUAGES)
    assert L.by_name("python").comment_prefix == "#"
    assert L.by_name("bash").comment_prefix == "#"
    assert L.by_name("ruby").comment_prefix == "#"
    assert L.by_name("go").comment_prefix == "//"
    assert L.by_name("javascript").comment_prefix == "//"
    assert L.by_name("rust").comment_prefix == "//"
    # path helper defaults unknown → "#"
    assert L.comment_prefix_for_path("a.go") == "//"
    assert L.comment_prefix_for_path("a.unknownext") == "#"


def test_module_fix_placement_set_for_scope_langs_only():
    for path in ("a.py", "a.go", "a.js", "a.ts", "a.tsx", "a.sh"):
        assert L.module_fix_placement_for_path(path), path  # scope langs have a hint
    # out of first-cut scope → None (no placement guidance yet)
    assert L.module_fix_placement_for_path("a.rs") is None
    assert L.module_fix_placement_for_path("a.rb") is None
    assert L.module_fix_placement_for_path("a.unknownext") is None
