"""data_ops — walk, query, slice, and surgically EDIT data files (v1: YAML).

Code edits in the framework are AST-surgical (tree-sitter patch). Data edits had
no equivalent, so any change to a YAML/JSON/TOML file routed to a full-file LLM
*rewrite* — slow (a 16 KB world_data.yaml regenerated 7× ≈ 20% of one run) and
lossy. This module is the data-file analogue of the AST patcher:

  - Tier 1 — walk / query / slice: navigate by path, extract a subtree, with
    best-effort line spans. Reads use jsonpath-ng (recursive descent, filters);
    addressing uses RFC-6901 JSONPointer (+ ``-`` append, ``[key=value]``
    predicate) so a write targets exactly one node.
  - Tier 2 — apply: set / add / remove / move at a pointer, mutating the LIVE
    round-trip object (ruamel CommentedMap/Seq) so comments, key order, and
    formatting survive. ``patch_text`` is the deterministic seam callers use.

v1 implements the YAML backend (ruamel.yaml). The applier is format-parametric;
TOML (tomlkit) / JSON backends drop in later. The module is PURE — no effects,
no flows — so it is unit-tested in isolation.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

# ── Formats & errors ───────────────────────────────────────────────────


class Fmt(str, Enum):
    YAML = "yaml"
    JSON = "json"
    TOML = "toml"


_EXT_FMT = {
    "yaml": Fmt.YAML,
    "yml": Fmt.YAML,
    "json": Fmt.JSON,
    "toml": Fmt.TOML,
}


def detect_fmt(path: str, content: str | None = None) -> Fmt:
    """Format from extension first; content sniff (``{``/``[`` → JSON) as tiebreak;
    default YAML."""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _EXT_FMT:
        return _EXT_FMT[ext]
    if content and content.lstrip()[:1] in "{[":
        return Fmt.JSON
    return Fmt.YAML


class DataOpsError(Exception):
    """Base for all data_ops failures — callers fall back to rewrite on these."""


class ParseError(DataOpsError):
    """Content could not be parsed in its format."""


class PathError(DataOpsError):
    """A JSONPointer did not resolve (missing key/index, scalar descent, …)."""


class OpError(DataOpsError):
    """An op is invalid for its target."""


# ── Result dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Span:
    start_line: int  # 1-based
    end_line: int | None = None
    start_col: int | None = None
    note: str = ""


@dataclass
class Match:
    pointer: str  # concrete RFC-6901 pointer (addressable for a patch)
    value: Any
    span: Span | None = None


@dataclass
class Slice:
    pointer: str
    value: Any
    span: Span | None
    text: str  # the subtree re-serialized in its format (for prompt display)


@dataclass
class Node:
    pointer: str
    value: Any
    span: Span | None = None


@dataclass
class DataOp:
    op: Literal["set", "add", "remove", "move"]
    path: str
    value: Any = None
    from_: str | None = None


@dataclass
class ApplyResult:
    document: "Document"
    applied: list[DataOp] = field(default_factory=list)
    failed: list[tuple[DataOp, str]] = field(default_factory=list)
    changed: bool = False


@dataclass
class PatchTextResult:
    text: str
    applied: list[DataOp] = field(default_factory=list)
    failed: list[tuple[DataOp, str]] = field(default_factory=list)
    ok: bool = False


# ── JSONPointer (RFC 6901) + ``-`` append + ``[key=value]`` predicate ───

_PRED_RE = re.compile(r"^\[([^=\]]+)=(.*)\]$")


def _unescape(tok: str) -> str:
    return tok.replace("~1", "/").replace("~0", "~")


def _escape(tok: str) -> str:
    return tok.replace("~", "~0").replace("/", "~1")


def _parse_pointer(pointer: str) -> list[str]:
    if pointer in ("", "/"):
        return [] if pointer == "" else [""]
    if not pointer.startswith("/"):
        raise PathError(f"pointer must start with '/': {pointer!r}")
    return [_unescape(t) for t in pointer[1:].split("/")]


def _is_int_token(tok: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", tok))


def _seq_index(seq: list, tok: str, *, allow_append: bool) -> int:
    """Resolve a list token to an int index. ``-`` → append position;
    ``[k=v]`` → the index of the element whose child ``k`` equals ``v``."""
    if tok == "-":
        if not allow_append:
            raise PathError("'-' (append) is only valid as a set/add target")
        return len(seq)
    pred = _PRED_RE.match(tok)
    if pred:
        k, v = pred.group(1), pred.group(2)
        for i, item in enumerate(seq):
            if isinstance(item, dict) and str(item.get(k)) == v:
                return i
        raise PathError(f"no list item with {k}={v!r}")
    if not _is_int_token(tok):
        raise PathError(f"non-integer index {tok!r} on a list")
    idx = int(tok)
    return idx + len(seq) if idx < 0 else idx


def _new_container(next_tok: str) -> Any:
    if next_tok == "-" or _PRED_RE.match(next_tok) or _is_int_token(next_tok):
        return CommentedSeq()
    return CommentedMap()


def resolve_pointer(
    root: Any, pointer: str, *, create: bool = False, allow_append: bool = False
) -> tuple[Any, Any, bool]:
    """Navigate to the target's PARENT. Returns ``(parent, key, exists)`` where
    ``key`` is a dict key or an int index. ``(None, None, True)`` for the root.
    With ``create``, missing intermediate containers are made."""
    tokens = _parse_pointer(pointer)
    if not tokens:
        return (None, None, True)
    node = root
    for i, tok in enumerate(tokens):
        last = i == len(tokens) - 1
        if isinstance(node, list):
            idx = _seq_index(node, tok, allow_append=allow_append and last)
            if last:
                return (node, idx, 0 <= idx < len(node))
            if not 0 <= idx < len(node):
                raise PathError(f"index {idx} out of range at {tok!r}")
            node = node[idx]
        elif isinstance(node, dict):
            if last:
                return (node, tok, tok in node)
            if tok not in node:
                if not create:
                    raise PathError(f"missing key {tok!r} in {pointer}")
                node[tok] = _new_container(tokens[i + 1])
            node = node[tok]
        else:
            raise PathError(f"cannot descend into a scalar at {tok!r}")
    raise PathError("empty navigation")  # unreachable


# ── YAML backend helpers ───────────────────────────────────────────────


def _yaml() -> YAML:
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096  # don't wrap long scalars
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _coerce_yaml(value: Any) -> Any:
    """Wrap plain dict/list (e.g. from LLM JSON) into ruamel Commented* so nested
    inserts stay round-trippable. Already-Commented nodes (e.g. a moved subtree)
    pass through untouched so their comments survive. Scalars pass through."""
    if isinstance(value, CommentedMap | CommentedSeq):
        return value
    if isinstance(value, dict):
        m = CommentedMap()
        for k, v in value.items():
            m[k] = _coerce_yaml(v)
        return m
    if isinstance(value, list):
        s = CommentedSeq()
        for v in value:
            s.append(_coerce_yaml(v))
        return s
    return value


def _dump_value(value: Any, fmt: Fmt) -> str:
    if fmt != Fmt.YAML:
        return str(value)
    if not isinstance(value, dict | list):
        return str(value).rstrip()
    y = _yaml()
    buf = io.StringIO()
    y.dump(value, buf)
    return buf.getvalue().rstrip()


# ── jsonpath-ng → JSONPointer adapter ──────────────────────────────────


def jsonpath_to_pointer(full_path: Any) -> str:
    """Flatten a jsonpath-ng ``full_path`` (Child/Fields/Index tree) to an
    RFC-6901 pointer, so a query match is addressable for a patch."""
    toks: list[str] = []

    def rec(p: Any) -> None:
        name = type(p).__name__
        if name == "Child":
            rec(p.left)
            rec(p.right)
        elif name == "Fields":
            toks.extend(str(f) for f in p.fields)
        elif name == "Index":
            idx = getattr(p, "index", None)
            if idx is None:
                indices = getattr(p, "indices", None) or ()
                idx = indices[0] if indices else None
            if idx is not None:
                toks.append(str(idx))
        # Root / This / Descendants prefix: contribute no token

    rec(full_path)
    return "/" + "/".join(_escape(t) for t in toks) if toks else ""


# ── Document ───────────────────────────────────────────────────────────


@dataclass
class Document:
    fmt: Fmt
    root: Any
    _raw: str = ""
    readonly: bool = False  # set by read(); blocks the write path (dumps/apply)

    @classmethod
    def load(cls, content: str, fmt: Fmt) -> "Document":
        if fmt is not Fmt.YAML:
            raise DataOpsError(
                f"{fmt.value} editing is not supported in v1 (YAML only)"
            )
        try:
            root = _yaml().load(content)
        except Exception as e:  # noqa: BLE001 - any parse failure is the diagnosis
            raise ParseError(f"{type(e).__name__}: {e}") from e
        if root is None:
            root = CommentedMap()
        return cls(fmt=fmt, root=root, _raw=content)

    @classmethod
    def read(cls, content: str, fmt: Fmt) -> "Document":
        """Read-only parse for trace/inspection — supports YAML/JSON/TOML.

        The result is traversable (slice/query/walk) but NOT writable: ``dumps``
        and ``apply`` refuse a read-only Document. This keeps the YAML-only write
        invariant structural — the only way to obtain a non-YAML root is ``read``,
        which marks it read-only — while letting the data-aware trace slice the
        REAL subtree of any data file instead of reconstructing a pruned copy."""
        try:
            if fmt is Fmt.JSON:
                import json

                root = json.loads(content)
            elif fmt is Fmt.TOML:
                import tomllib

                root = tomllib.loads(content)
            else:  # YAML — reuse the round-trip loader so YAML reads are uniform
                root = _yaml().load(content)
        except Exception as e:  # noqa: BLE001 - any parse failure is the diagnosis
            raise ParseError(f"{type(e).__name__}: {e}") from e
        if root is None:
            root = {}
        return cls(fmt=fmt, root=root, _raw=content, readonly=True)

    def dumps(self) -> str:
        if self.readonly:
            raise DataOpsError("cannot serialize a read-only Document")
        if self.fmt is not Fmt.YAML:
            raise DataOpsError(f"{self.fmt.value} serialization not supported in v1")
        buf = io.StringIO()
        _yaml().dump(self.root, buf)
        return buf.getvalue()

    # ── Tier 1 ─────────────────────────────────────────────────────────

    def slice(self, pointer: str) -> Slice:
        try:
            parent, key, exists = resolve_pointer(self.root, pointer)
        except DataOpsError:
            # Reads are best-effort: a missing/unresolvable path → empty slice.
            return Slice(pointer=pointer, value=None, span=None, text="")
        if parent is None:  # root
            value = self.root
        elif not exists:
            return Slice(pointer=pointer, value=None, span=None, text="")
        else:
            value = parent[key]
        return Slice(
            pointer=pointer,
            value=value,
            span=self._span(parent, key),
            text=_dump_value(value, self.fmt),
        )

    def query(self, expr: str) -> list[Match]:
        from jsonpath_ng.ext import parse as jparse

        try:
            jp = jparse(expr)
        except Exception as e:  # noqa: BLE001
            raise PathError(f"bad jsonpath {expr!r}: {e}") from e
        out: list[Match] = []
        for m in jp.find(self.root):
            ptr = jsonpath_to_pointer(m.full_path)
            out.append(Match(pointer=ptr, value=m.value, span=self._span_at(ptr)))
        return out

    def walk(self) -> Iterator[Node]:
        def rec(node: Any, ptr: str) -> Iterator[Node]:
            yield Node(pointer=ptr, value=node)
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from rec(v, ptr + "/" + _escape(str(k)))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    yield from rec(v, ptr + "/" + str(i))

        yield from rec(self.root, "")

    # ── Tier 2 ─────────────────────────────────────────────────────────

    def apply(self, ops: list[DataOp]) -> ApplyResult:
        if self.readonly:
            return ApplyResult(
                document=self,
                applied=[],
                failed=[(o, "document is read-only") for o in ops],
                changed=False,
            )
        applied: list[DataOp] = []
        failed: list[tuple[DataOp, str]] = []
        for op in ops:
            try:
                self._apply_one(op)
                applied.append(op)
            except DataOpsError as e:
                failed.append((op, str(e)))
        return ApplyResult(
            document=self, applied=applied, failed=failed, changed=bool(applied)
        )

    # ── internals ──────────────────────────────────────────────────────

    def _apply_one(self, op: DataOp) -> None:
        if op.op == "set":
            self._place(op.path, _coerce_yaml(op.value), insert=False)
        elif op.op == "add":
            self._place(op.path, _coerce_yaml(op.value), insert=True)
        elif op.op == "remove":
            parent, key, exists = resolve_pointer(self.root, op.path)
            if parent is None:
                raise OpError("cannot remove the document root")
            if not exists:
                raise PathError(f"remove target missing: {op.path}")
            del parent[key]
        elif op.op == "move":
            if not op.from_:
                raise OpError("move requires 'from_'")
            sp, sk, se = resolve_pointer(self.root, op.from_)
            if sp is None or not se:
                raise PathError(f"move source missing: {op.from_}")
            staged = sp[sk]
            del sp[sk]
            self._place(op.path, staged, insert=True)  # live node, not re-coerced
        else:
            raise OpError(f"unknown op {op.op!r}")

    def _place(self, path: str, value: Any, *, insert: bool) -> None:
        parent, key, exists = resolve_pointer(
            self.root, path, create=True, allow_append=True
        )
        if parent is None:
            raise OpError("cannot set the document root")
        if isinstance(parent, list):
            if insert:
                if key >= len(parent):
                    parent.append(value)
                else:
                    parent.insert(key, value)
            else:
                if not 0 <= key < len(parent):
                    raise PathError(f"set index {key} out of range")
                parent[key] = value
        else:  # dict — upsert
            parent[key] = value

    def _span(self, parent: Any, key: Any) -> Span | None:
        try:
            if isinstance(parent, list) and hasattr(parent, "lc"):
                line, col = parent.lc.item(key)
                return Span(
                    start_line=line + 1, start_col=col + 1, note="approximate-end"
                )
            if isinstance(parent, dict) and hasattr(parent, "lc"):
                data = getattr(parent.lc, "data", None) or {}
                if key in data:
                    kl, kc, _vl, _vc = data[key]
                    return Span(
                        start_line=kl + 1, start_col=kc + 1, note="approximate-end"
                    )
        except Exception:  # noqa: BLE001 - spans are best-effort, never raise
            return None
        return None

    def _span_at(self, pointer: str) -> Span | None:
        try:
            parent, key, exists = resolve_pointer(self.root, pointer)
            return self._span(parent, key) if exists and parent is not None else None
        except DataOpsError:
            return None


# ── The deterministic seam Phase 2 calls ───────────────────────────────


def patch_text(content: str, fmt: Fmt, ops: list[DataOp]) -> PatchTextResult:
    """Load → apply → dumps, with a re-parse sanity check. ``ok`` is True iff
    every op applied AND the result re-parses. On any failure the caller falls
    back to a full rewrite, so this is strictly additive."""
    try:
        doc = Document.load(content, fmt)
    except DataOpsError as e:
        return PatchTextResult(
            text=content, applied=[], failed=[(o, str(e)) for o in ops], ok=False
        )
    res = doc.apply(ops)
    try:
        text = doc.dumps()
    except DataOpsError as e:
        return PatchTextResult(
            text=content,
            applied=res.applied,
            failed=[(o, str(e)) for o in ops],
            ok=False,
        )
    ok = not res.failed
    if ok:
        try:
            Document.load(text, fmt)  # re-parse guard
        except DataOpsError:
            ok = False
    return PatchTextResult(text=text, applied=res.applied, failed=res.failed, ok=ok)
