"""Canonical per-language / per-extension registry — the single source of truth.

Before this module, language facts were scattered across 8 tables in 6 files,
keyed three incompatible ways (ext-with-dot ".py", ext-no-dot "py", language-name
"python"). This consolidates them so there is one place to look — and so the
upcoming per-file-type "module-level fix" patterns attach to one record per
language rather than spawning a 9th table.

Two shapes coexist, on purpose, because the original data does NOT reduce to a
single per-language record:

  • ``LanguageSpec`` (the catalog) holds the genuinely PER-LANGUAGE structural
    facts — grammar, definition node-kinds, frame label/fence — which are
    consistent across all of a language's extensions.

  • The idiosyncratic PER-EXTENSION tables (``_FENCE_BY_EXT``, ``_NAME_TO_EXT``,
    ``SOURCE_EXTENSIONS``, ``DATA_EXTENSIONS``, ``DATA_PATCH_EXTENSIONS``) are kept
    VERBATIM, because their membership is per-extension and irregular — e.g.
    ``js``/``jsx`` are source extensions but ``mjs``/``cjs`` are not; ``jsx``
    fences as ``jsx`` here but as ``javascript`` in the frame editor; ``sh`` and
    ``scala`` are code yet absent from the import-scan set. A per-language flag
    would silently change those answers.

This module is a LEAF: it imports only the stdlib and NOTHING from ``agent`` or
``tree_sitter*`` (it is imported by repomap, turn_renderer, frame_actions,
pipeline_actions, ast_actions, batch_structural_actions, diagnosis_session_actions
— a cycle would be fatal). The tree-sitter machinery that turns a grammar NAME
into a parser stays in ``repomap.py``; this module holds only the static strings.
"""

from __future__ import annotations

import os.path
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LanguageSpec:
    """Per-language structural facts (consistent across the language's extensions)."""

    name: str                                    # canonical: "python", "javascript"
    extensions: tuple[str, ...]                  # ext-NO-dot variants: ("js","mjs","cjs","jsx")
    grammar: str | None = None                   # EXACT tree-sitter-language-pack name; None = none
    def_node_kinds: dict[str, str] = field(default_factory=dict)  # {ts_node_type: SymbolDef.kind}
    label: str = ""                              # frame-editor display label ("shell script")
    frame_fence: str = ""                        # frame-editor code fence ("bash")
    # ── forward-looking: the per-file-type "module-level fix" patterns fill these
    #    per spec (a comment prefix, a placement rule). Intentionally unpopulated. ──
    comment_prefix: str | None = None
    module_fix_placement: str | None = None


# ── The catalog. Only languages with a grammar (and thus a frame label/fence)
#    need a spec today; grammar-less source/data/markup extensions are served by
#    the verbatim tables + helper fallbacks below. Grow this as fix patterns or
#    grammars are added. Values copied verbatim from the retired maps. ──
LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python", extensions=("py",), grammar="python",
        def_node_kinds={},  # Python is dispatched by repomap's dedicated extractor, never via these
        label="Python file", frame_fence="python",
    ),
    LanguageSpec(
        name="bash", extensions=("sh", "bash", "zsh"), grammar="bash",
        def_node_kinds={"function_definition": "function"},
        label="shell script", frame_fence="bash",
    ),
    LanguageSpec(
        name="javascript", extensions=("js", "mjs", "cjs", "jsx"), grammar="javascript",
        def_node_kinds={
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "method",
        },
        label="JavaScript file", frame_fence="javascript",
    ),
    LanguageSpec(
        name="typescript", extensions=("ts",), grammar="typescript",
        def_node_kinds={
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "class_declaration": "class",
            "interface_declaration": "class",
            "method_definition": "method",
            "method_signature": "method",
        },
        label="TypeScript file", frame_fence="typescript",
    ),
    LanguageSpec(
        name="tsx", extensions=("tsx",), grammar="tsx",
        def_node_kinds={
            "function_declaration": "function",
            "class_declaration": "class",
            "interface_declaration": "class",
            "method_definition": "method",
        },
        label="TypeScript file", frame_fence="tsx",
    ),
    LanguageSpec(
        name="go", extensions=("go",), grammar="go",
        def_node_kinds={
            "function_declaration": "function",
            "method_declaration": "method",
            "type_declaration": "class",
        },
        label="Go file", frame_fence="go",
    ),
    LanguageSpec(
        name="ruby", extensions=("rb",), grammar="ruby",
        def_node_kinds={
            "method": "method",
            "singleton_method": "method",
            "class": "class",
            "module": "module",
        },
        label="Ruby file", frame_fence="ruby",
    ),
    LanguageSpec(
        name="rust", extensions=("rs",), grammar="rust",
        def_node_kinds={
            "function_item": "function",
            "struct_item": "class",
            "enum_item": "class",
            "trait_item": "class",
        },
        label="Rust file", frame_fence="rust",
    ),
    LanguageSpec(
        name="java", extensions=("java",), grammar="java",
        def_node_kinds={
            "method_declaration": "method",
            "constructor_declaration": "method",
            "class_declaration": "class",
            "interface_declaration": "class",
        },
        label="Java file", frame_fence="java",
    ),
)


# ── Verbatim per-extension / per-name tables (irregular membership — do NOT
#    fold into LanguageSpec flags; see the module docstring). ──

# ext-no-dot → markdown fence tag, where it differs from the extension itself
# (was turn_renderer._EXTENSION_FENCE).
_FENCE_BY_EXT: dict[str, str] = {
    "py": "python",
    "js": "javascript",
    "ts": "typescript",
    "rb": "ruby",
    "rs": "rust",
    "yml": "yaml",
    "md": "markdown",
    "sh": "bash",
}

# language-NAME → ext-no-dot (was turn_renderer._LANGUAGE_EXTENSIONS). Includes
# aliases (shell/sh → sh, md → md) and non-code (yaml/json/markdown/html/css).
_NAME_TO_EXT: dict[str, str] = {
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
    "rust": "rs",
    "go": "go",
    "ruby": "rb",
    "java": "java",
    "swift": "swift",
    "kotlin": "kt",
    "scala": "scala",
    "bash": "sh",
    "shell": "sh",
    "sh": "sh",
    "yaml": "yaml",
    "toml": "toml",
    "json": "json",
    "markdown": "md",
    "md": "md",
    "html": "html",
    "css": "css",
}

# "Is this a source-code file we should scan for imports?" (was
# pipeline_actions._SOURCE_EXTENSIONS). NOTE: irregular — includes grammar-less
# php/swift/kt/kts/dart/ex/exs but EXCLUDES sh/scala/mjs/cjs.
SOURCE_EXTENSIONS: frozenset[str] = frozenset({
    "py", "js", "ts", "jsx", "tsx", "rs", "go", "rb", "java",
    "kt", "kts", "swift", "dart", "ex", "exs", "php",
})

# "Is this a structured-data file?" (was pipeline_actions._DATA_EXTENSIONS).
DATA_EXTENSIONS: frozenset[str] = frozenset({"yaml", "yml", "json", "toml"})

# "Is this surgically data-patchable?" (was ast_actions._DATA_PATCH_EXTS). A
# STRICT SUBSET of DATA_EXTENSIONS — only YAML has a surgical patch backend.
DATA_PATCH_EXTENSIONS: frozenset[str] = frozenset({"yaml", "yml"})


# ── Indices (built once at import) ──
_BY_EXT: dict[str, LanguageSpec] = {}
for _spec in LANGUAGES:
    for _ext in _spec.extensions:
        if _ext in _BY_EXT:  # structural guard: no two specs claim the same extension
            raise ValueError(f"duplicate extension {_ext!r} in LANGUAGES registry")
        _BY_EXT[_ext] = _spec
_BY_NAME: dict[str, LanguageSpec] = {s.name: s for s in LANGUAGES}
_BY_GRAMMAR: dict[str, LanguageSpec] = {s.grammar: s for s in LANGUAGES if s.grammar}


# ── Helpers (the consumer-facing API; each replaces one retired map access) ──

def _norm_ext(ext: str) -> str:
    """Lowercase, strip a single leading dot. '.PY' -> 'py', 'py' -> 'py', '' -> ''."""
    ext = (ext or "").lower()
    return ext[1:] if ext.startswith(".") else ext


def by_ext(ext: str) -> LanguageSpec | None:
    """Spec for an extension (leading dot optional). None if unknown/grammar-less."""
    return _BY_EXT.get(_norm_ext(ext))


def by_name(name: str) -> LanguageSpec | None:
    """Spec for a canonical language name ('python'). None if unknown."""
    return _BY_NAME.get(name)


def spec_for_path(path: str) -> LanguageSpec | None:
    """Spec for a file path's extension. None for extensionless/unknown."""
    return by_ext(os.path.splitext(path or "")[1])


def grammar_for_ext(ext: str) -> str | None:
    """Tree-sitter grammar name for an extension, or None. Replaces
    ``_LANG_BY_EXT.get(ext)``. (Python carries grammar='python' for catalog
    completeness, but repomap dispatches '.py' via its dedicated extractor BEFORE
    consulting this — so the non-Python dispatch is unaffected.)"""
    spec = by_ext(ext)
    return spec.grammar if spec else None


def def_node_kinds_for_grammar(grammar: str) -> dict[str, str]:
    """{tree_sitter_node_type: kind} for a grammar; {} if none. Replaces
    ``_DEF_NODE_KINDS.get(grammar, {})``."""
    spec = _BY_GRAMMAR.get(grammar)
    return spec.def_node_kinds if spec else {}


def fence_for_ext(ext: str) -> str:
    """Markdown fence tag for an extension, falling back to the extension itself.
    Replaces ``_EXTENSION_FENCE.get(ext, ext)``."""
    norm = _norm_ext(ext)
    return _FENCE_BY_EXT.get(norm, norm)


def ext_for_name(name: str) -> str:
    """Primary extension for a language name, falling back to the name itself.
    Replaces ``_LANGUAGE_EXTENSIONS.get(name, name)``."""
    return _NAME_TO_EXT.get(name, name)


def frame_label_and_fence(path: str) -> tuple[str, str]:
    """(display label, code fence) for the frame editor, or ('file', '') when the
    path's language has no frame metadata. Replaces frame_actions._frame_lang."""
    spec = spec_for_path(path)
    if spec and spec.label:
        return spec.label, spec.frame_fence
    return "file", ""


def is_source(ext: str) -> bool:
    """Source-code file (import-scannable)? Replaces ``ext in _SOURCE_EXTENSIONS``."""
    return _norm_ext(ext) in SOURCE_EXTENSIONS


def is_data(ext: str) -> bool:
    """Structured-data file? Replaces ``ext in _DATA_EXTENSIONS``."""
    return _norm_ext(ext) in DATA_EXTENSIONS


def is_data_patch(ext: str) -> bool:
    """Surgically data-patchable (YAML only)? Replaces ``ext in _DATA_PATCH_EXTS``."""
    return _norm_ext(ext) in DATA_PATCH_EXTENSIONS
