"""The serialized round trip, checked against the DESIGN's declared contract.

WHY THIS REPLACED THE OLD CHECK. `_serialized_roundtrip_violations` used to
REDISCOVER the save payload from code idioms — dict literals beside a
`json.dump`, class methods named `to_dict`, annotated loaders, tuple-unpacked
loaders — a list that grew one live miss at a time. On 2026-09-22 the next
miss arrived: `save.py` built its payload in a module-level `state_to_dict`
with helper functions, the checker read zero keys, reported "UNVERIFIED" as a
violation naming no file, and the session walk charged it to `main.py`. Two
repair turns wrote 330 lines of dead shadow serializer into `main.py`; the
sweep then renamed correct code in `save.py` on an inferred theory of the
checker and closed the goal on a proxy. Re-run afterwards, the old check still
said UNVERIFIED. A wrong check is about as damaging as no check.

WHAT THIS DOES INSTEAD.
  * SCOPE — only a contract the design DECLARED: a state shape naming a
    persisted file (``save.json``, ``savegame.yaml`` …) with a Python owner
    and a brace schema. No declared contract, nothing to check. The save and
    load calls are looked for in the contract's files, then in what those
    files call — never anywhere else.
  * KEYS — from the declared schema, parsed into a tree (records, and maps or
    lists of records), not inferred from code.
  * FOLLOW THE DATA — from the argument of a ``json.dump`` back to whatever
    builds it, and from the result of a ``json.load`` forward to whatever
    reads it: through module functions, methods, constructors, parameters and
    returns, under any name, each call followed in its own calling context.
    Not a list of idioms.
  * PRECISION OVER RECALL — anything built or consumed opaquely (a library
    call, ``**`` unpacking into unknown code, an attribute store, dynamic
    keys) leaves that level UNJUDGED. A side that cannot be followed produces
    no finding at all: the checker's blind spot is not the code's defect, and
    the runtime save/load goals are the ground truth for what static reading
    cannot see. A value the code itself declares DERIVED (a ``@property``, a
    ``field(init=False)``) needs no round trip and is never reported unread.
  * THE FILE TO FIX IS NAMED — a key never written is booked on the file that
    builds that level of the payload; a key never read, on the file that reads
    it. Never on whichever file happens to be current.
"""

from __future__ import annotations

import ast
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════
# The declared contract
# ══════════════════════════════════════════════════════════════════════

_PERSISTED_NAME = re.compile(r"[\w./*-]+\.(?:json|ya?ml|toml)\b", re.I)
# A design may name the persisted shape by its FORMAT rather than its file
# ("Saved game JSON" — tier_20260923-165314, whose save contract a file-token
# rule missed entirely).
_FORMAT_WORD = re.compile(r"\b(?:json|ya?ml|toml)\b", re.I)
_PY_PATH = re.compile(r"[\w./-]+\.py\b")
# `name: …`, `"name": …`, `name?: …`
_KEYED = re.compile(r"\s*[\"']?([A-Za-z_]\w*)[\"']?\s*\??\s*:")
# `{health, equipment, inventory}` — a field named without a type.
_BARE = re.compile(r"\s*[\"']?([A-Za-z_]\w*)[\"']?\s*(?=[,;}])")
# `dict[str, {...}]`, `list[{...}]`
_GENERIC = re.compile(r"\s*[A-Za-z_][\w.]*\s*\[")
# A lone key naming a TYPE or an ID is a map placeholder
# (`flags: {str: bool}`, `rooms: {room_id: {...}}`), never a literal key.
_TYPE_WORDS = {"str", "string", "int", "float", "bool", "any", "key", "name", "id"}
# Written for humans and migrations, not read back by a loader — flagging
# them unread is true and useless, and findings here drive repairs.
_AUDIT_FIELDS = {
    "version",
    "schema_version",
    "save_version",
    "game_version",
    "format_version",
    "timestamp",
    "save_timestamp",
    "saved_at",
    "created_at",
    "generated_at",
}
_DATA_EXT = (".json", ".yaml", ".yml", ".toml")


@dataclass
class DeclaredNode:
    """A declared record (``fields``) or a map/list of records (``element``)."""

    fields: dict[str, Optional["DeclaredNode"]] = field(default_factory=dict)
    element: Optional["DeclaredNode"] = None
    is_collection: bool = False


@dataclass
class PersistedContract:
    name: str
    owner: str
    consumers: list[str]
    shape: DeclaredNode

    @property
    def persisted_file(self) -> str:
        m = _PERSISTED_NAME.search(self.name)
        return (m.group(0) if m else self.name).rsplit("/", 1)[-1]

    @property
    def modules(self) -> Optional[set[str]]:
        """The serializer modules this contract's format is written and read
        with — a JSON save pairs json calls only, so a YAML world load in the
        same files is never mistaken for the save coming back. None when the
        name gives no format (a transient file named in prose)."""
        m = _PERSISTED_NAME.search(self.name) or _FORMAT_WORD.search(self.name)
        if not m:
            return None
        ext = m.group(0).rsplit(".", 1)[-1].lower()
        return _FORMAT_MODULES.get(ext)


def _skip_segment(s: str, i: int) -> int:
    """Advance past prose up to the next top-level ',' / ';' or a closing
    bracket (not consumed), honouring nested brackets and quotes."""
    depth = 0
    quote = ""
    while i < len(s):
        c = s[i]
        if quote:
            if c == quote:
                quote = ""
        elif c in "'\"":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                return i
            depth -= 1
        elif c in ",;" and depth == 0:
            return i
        i += 1
    return i


def _parse_object(s: str, i: int) -> tuple[DeclaredNode, int]:
    """Parse ``{k: v, ...}`` starting at ``s[i] == '{'``."""
    i += 1
    fields: dict[str, Optional[DeclaredNode]] = {}
    while i < len(s):
        while i < len(s) and s[i] in " \t\r\n,;":
            i += 1
        if i >= len(s):
            break
        if s[i] == "}":
            i += 1
            break
        m = _KEYED.match(s, i)
        if m:
            child, i = _parse_value(s, m.end())
            fields[m.group(1)] = child
            continue
        m = _BARE.match(s, i)
        if m:
            fields[m.group(1)] = None
            i = m.end()
            continue
        i = _skip_segment(s, i)
        if i < len(s) and s[i] not in ",;}":
            i += 1
    if len(fields) == 1:
        (only, child), *_ = fields.items()
        low = only.lower()
        if low in _TYPE_WORDS or low.endswith(("_id", "_name", "_key")):
            return DeclaredNode(element=child, is_collection=True), i
    return DeclaredNode(fields=fields), i


def _parse_bracketed(s: str, i: int) -> tuple[Optional[DeclaredNode], int]:
    """``[ ... ]`` starting at ``s[i] == '['``: a collection whose element is
    the first record inside (``[{...}]``, ``dict[str, {...}]``)."""
    j = i + 1
    depth = 1
    elem: Optional[DeclaredNode] = None
    while j < len(s) and depth:
        c = s[j]
        if c == "{" and depth == 1 and elem is None:
            elem, j = _parse_object(s, j)
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
        j += 1
    return (DeclaredNode(element=elem, is_collection=True) if elem else None), j


def _parse_value(s: str, i: int) -> tuple[Optional[DeclaredNode], int]:
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    child: Optional[DeclaredNode] = None
    if i < len(s) and s[i] == "{":
        child, i = _parse_object(s, i)
    elif i < len(s) and s[i] == "[":
        child, i = _parse_bracketed(s, i)
    else:
        g = _GENERIC.match(s, i)
        if g:
            child, i = _parse_bracketed(s, g.end() - 1)
    return child, _skip_segment(s, i)


def parse_declared_shape(structure: str) -> Optional[DeclaredNode]:
    """The design's compact schema as a tree, or None when it is prose
    ("identical to GameState schema, persisted as JSON")."""
    start = structure.find("{")
    if start < 0:
        return None
    node, _ = _parse_object(structure, start)
    return node if node.fields else None


def declared_persisted_contracts(mission: Any) -> list[PersistedContract]:
    """State shapes that name a persisted file or format, have a Python
    owner, and a brace schema. Everything else is out of this check's
    scope."""
    from agent.actions.contract_swarm_actions import _state_contracts

    arch = getattr(mission, "architecture", None) if mission else None
    transient = {str(t) for t in (getattr(arch, "transient_files", None) or [])}
    out: list[PersistedContract] = []
    for sc in _state_contracts(mission):
        name = sc.get("name", "")
        if not (
            _PERSISTED_NAME.search(name)
            or _FORMAT_WORD.search(name)
            or name in transient
        ):
            continue
        owners = _PY_PATH.findall(sc.get("owner") or "")
        if not owners:
            continue
        shape = parse_declared_shape(sc.get("structure") or "")
        if shape is None:
            continue
        consumers = [
            p for p in _PY_PATH.findall(sc.get("consumed_by") or "") if p != owners[0]
        ]
        out.append(PersistedContract(name, owners[0], consumers, shape))
    return out


def _resolve_path(declared: str, paths: list[str]) -> Optional[str]:
    """The written file a design path names: exact, else a unique suffix or
    basename match (a design may say ``engine.py`` for ``src/engine.py``)."""
    if declared in paths:
        return declared
    tail = [p for p in paths if p.endswith("/" + declared)]
    if len(tail) == 1:
        return tail[0]
    base = declared.rsplit("/", 1)[-1]
    named = [p for p in paths if p.rsplit("/", 1)[-1] == base]
    return named[0] if len(named) == 1 else None


# ══════════════════════════════════════════════════════════════════════
# Following the data
# ══════════════════════════════════════════════════════════════════════

# Resolved payload trees are plain dicts: key -> subtree. A writer subtree of
# None means "a value with no key information" (a scalar, an attribute).
# "*" holds a collection's element (dynamic keys / list items). Reserved:
# "__open__" (keys may exist that could not be seen — an absence at this
# level proves nothing), "__sites__" (the files where this level was built or
# read — where a finding about it is booked), "__derived__" (keys whose
# written value the code itself declares computed).
_OPEN = "__open__"
_SITES = "__sites__"
_DERIVED = "__derived__"
_ELEM = "*"
_RESERVED = {_OPEN, _SITES, _DERIVED}
# A writer value reached again through a cycle: neutral in a merge.
_CYCLE: Any = object()

_DUMP_MODULES = {"json", "yaml", "toml", "tomli_w", "tomlkit"}
_DUMP_ATTRS = {"dump", "dumps", "safe_dump"}
_LOAD_MODULES = {"json", "yaml", "toml", "tomllib", "tomli", "tomlkit"}
_LOAD_ATTRS = {"load", "loads", "safe_load"}
_FORMAT_MODULES = {
    "json": {"json"},
    "yaml": {"yaml"},
    "yml": {"yaml"},
    "toml": {"toml", "tomllib", "tomli", "tomli_w", "tomlkit"},
}
# Calls whose result carries the payload through unchanged.
_PASSTHROUGH = {"dict", "deepcopy", "copy", "OrderedDict"}
# Calls that consume a value WITHOUT reading its keys.
_SCALAR_SINKS = {
    "int",
    "str",
    "float",
    "bool",
    "len",
    "isinstance",
    "round",
    "abs",
    "print",
    "repr",
    "hash",
}

# A calling context: the (caller, call) frames that led into a function, so a
# value returned or a parameter read goes back to THE call it came from — not
# to every caller of a shared helper.
Stack = tuple


@dataclass(eq=False)
class _Fn:
    path: str
    name: str
    node: Any
    cls: Optional[str]
    params: list[str]
    parents: dict[int, ast.AST] = field(default_factory=dict)
    own_returns: list[ast.Return] = field(default_factory=list)

    @property
    def is_method(self) -> bool:
        return (
            bool(self.cls) and bool(self.params) and self.params[0] in ("self", "cls")
        )


@dataclass
class _Class:
    path: str
    fields: list[str]
    bases: list[str]
    attr_types: dict[str, str] = field(default_factory=dict)
    derived: set[str] = field(default_factory=set)


def _callee(call: ast.Call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _module_call(node: ast.AST, modules: set[str], attrs: set[str]) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id in modules
        and node.func.attr in attrs
    )


def _const_str(node: Any) -> Optional[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _leaf(sub: Any) -> Any:
    return None if sub is _CYCLE else sub


def _merge(a: Any, b: Any) -> Any:
    """Union two WRITER trees. Real keys merged with an unresolved branch keep
    the keys but open the node — an absence there proves nothing."""
    if a is _CYCLE:
        return b
    if b is _CYCLE:
        return a
    if a is None and b is None:
        return None
    if a is None or b is None:
        keep = dict(a if a is not None else b)
        keep[_OPEN] = True
        return keep
    out = dict(a)
    for k, v in b.items():
        if k == _OPEN:
            out[_OPEN] = bool(out.get(_OPEN)) or bool(v)
        elif k in (_SITES, _DERIVED):
            out[k] = set(out.get(k) or ()) | set(v)
        elif k in out:
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _merge_read(a: Optional[dict], b: Optional[dict]) -> dict:
    """Union two READER trees ({} = read, with no key reads below)."""
    out = dict(a or {})
    for k, v in (b or {}).items():
        if k == _OPEN:
            out[_OPEN] = bool(out.get(_OPEN)) or bool(v)
        elif k == _SITES:
            out[_SITES] = set(out.get(_SITES) or ()) | set(v)
        else:
            out[k] = _merge_read(out.get(k), v)
    return out


def _opaque() -> dict:
    return {_OPEN: True}


def _has_keys(tree: Any) -> bool:
    return isinstance(tree, dict) and any(k not in _RESERVED for k in tree)


def _own_nodes(root: ast.AST):
    """Nodes of a function body, not descending into nested defs/lambdas."""
    todo = list(ast.iter_child_nodes(root))
    while todo:
        n = todo.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            todo.extend(ast.iter_child_nodes(n))


def _ann_name(ann: Any) -> Optional[str]:
    """The class an annotation names: `Player`, `"Player"`,
    `Optional[Player]`, `Player | None`."""
    if isinstance(ann, ast.Name):
        return ann.id
    if isinstance(ann, ast.Constant) and isinstance(ann.value, str):
        return ann.value.strip("'\" ").split("|")[0].strip() or None
    if isinstance(ann, ast.Subscript) and isinstance(ann.value, ast.Name):
        if ann.value.id == "Optional":
            return _ann_name(ann.slice)
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        for side in (ann.left, ann.right):
            n = _ann_name(side)
            if n and n != "None":
                return n
    return None


def _is_derived_decl(node: ast.AST) -> Optional[str]:
    """The attribute a class-body statement declares COMPUTED: a property, or
    a dataclass field the constructor never takes (``field(init=False)``)."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for d in node.decorator_list:
            name = d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
            if name in ("property", "cached_property"):
                return node.name
        return None
    if (
        isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Call)
        and _callee(node.value) == "field"
        and any(
            kw.arg == "init"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.value.keywords
        )
    ):
        return node.target.id
    return None


class PayloadFlow:
    """A project-wide index for following a payload between functions."""

    def __init__(self, sources: dict[str, str]) -> None:
        self.by_name: dict[str, list[_Fn]] = defaultdict(list)
        self.call_sites: dict[str, list[tuple[_Fn, ast.Call]]] = defaultdict(list)
        self.classes: dict[str, list[_Class]] = defaultdict(list)
        self.imports: dict[str, dict[str, str]] = defaultdict(dict)
        self.constants: dict[str, dict[str, str]] = defaultdict(dict)
        self.funcs: list[_Fn] = []
        self.unparsed: set[str] = set()
        self._memo: dict[tuple, Any] = {}
        self._active: set[tuple] = set()
        self._cuts = 0
        for path, src in sources.items():
            try:
                tree = ast.parse(src)
            except SyntaxError:
                self.unparsed.add(path)
                continue
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module:
                    for a in n.names:
                        self.imports[path][a.asname or a.name] = n.module
            for n in tree.body:
                if isinstance(n, ast.Assign) and len(n.targets) == 1:
                    t = n.targets[0]
                    if isinstance(t, ast.Name) and _const_str(n.value) is not None:
                        self.constants[path][t.id] = n.value.value
            self._index(path, tree, None)
        for fn in self.funcs:
            for n in ast.walk(fn.node):
                for child in ast.iter_child_nodes(n):
                    fn.parents[id(child)] = n
                if isinstance(n, ast.Call) and _callee(n):
                    self.call_sites[_callee(n)].append((fn, n))
            fn.own_returns = [
                n
                for n in _own_nodes(fn.node)
                if isinstance(n, ast.Return) and n.value is not None
            ]
        for fn in self.funcs:
            if fn.is_method:
                self._method_attr_types(fn)

    # ── index ──────────────────────────────────────────────────────────

    def _index(self, path: str, node: ast.AST, cls: Optional[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                info = _Class(
                    path,
                    [
                        n.target.id
                        for n in child.body
                        if isinstance(n, ast.AnnAssign)
                        and isinstance(n.target, ast.Name)
                    ],
                    [
                        b.id if isinstance(b, ast.Name) else getattr(b, "attr", "?")
                        for b in child.bases
                    ],
                )
                for n in child.body:
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                        t = _ann_name(n.annotation)
                        if t:
                            info.attr_types[n.target.id] = t
                    d = _is_derived_decl(n)
                    if d:
                        info.derived.add(d)
                self.classes[child.name].append(info)
                self._index(path, child, child.name)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = child.args
                params = [x.arg for x in (a.posonlyargs + a.args + a.kwonlyargs)]
                fn = _Fn(path, child.name, child, cls, params)
                self.funcs.append(fn)
                self.by_name[child.name].append(fn)
                self._index(path, child, cls)
            else:
                self._index(path, child, cls)

    def _method_attr_types(self, fn: _Fn) -> None:
        """`self.state: GameState = …` / `self.state = GameState(…)`."""
        info = self._class(fn.cls)
        if info is None:
            return
        for n in ast.walk(fn.node):
            target = value = ann = None
            if isinstance(n, ast.AnnAssign):
                target, ann, value = n.target, n.annotation, n.value
            elif isinstance(n, ast.Assign) and len(n.targets) == 1:
                target, value = n.targets[0], n.value
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
            t = _ann_name(ann) if ann is not None else None
            if (
                t is None
                and isinstance(value, ast.Call)
                and self._class(_callee(value))
            ):
                t = _callee(value)
            if t and target.attr not in info.attr_types:
                info.attr_types[target.attr] = t

    def _class(self, name: Optional[str]) -> Optional[_Class]:
        infos = self.classes.get(name or "") or []
        return infos[0] if len(infos) == 1 else None

    def _mro(self, name: str) -> list[str]:
        out: list[str] = []
        todo = [name]
        while todo:
            n = todo.pop(0)
            if n in out:
                continue
            out.append(n)
            info = self._class(n)
            if info:
                todo.extend(info.bases)
        return out

    def _dataclass_fields(self, name: str) -> Optional[set[str]]:
        """Annotated fields including inherited ones; None when a base is not
        a project class (its fields cannot be seen)."""
        out: set[str] = set()
        for n in self._mro(name):
            info = self._class(n)
            if info is None:
                if n in ("object", "ABC", "Generic"):
                    continue
                return None
            out |= set(info.fields)
        return out

    def _guard(self, key: tuple, compute: Callable[[], Any], on_cycle: Any) -> Any:
        """Memoised, cycle-safe evaluation. A result computed while a cycle
        was cut is not cached — it may be partial."""
        if key in self._memo:
            return self._memo[key]
        if key in self._active:
            self._cuts += 1
            return on_cycle
        self._active.add(key)
        before = self._cuts
        try:
            out = compute()
        finally:
            self._active.discard(key)
        if self._cuts == before:
            self._memo[key] = out
        return out

    # ── resolving calls ────────────────────────────────────────────────

    def _receiver_class(self, expr: ast.AST, fn: _Fn) -> Optional[str]:
        if isinstance(expr, ast.Name):
            if expr.id in ("self", "cls"):
                return fn.cls
            if self._class(expr.id):
                return expr.id
            args = fn.node.args
            for a in args.posonlyargs + args.args + args.kwonlyargs:
                if a.arg == expr.id and a.annotation is not None:
                    return _ann_name(a.annotation)
            for n in ast.walk(fn.node):
                if (
                    isinstance(n, ast.Assign)
                    and any(
                        isinstance(t, ast.Name) and t.id == expr.id for t in n.targets
                    )
                    and isinstance(n.value, ast.Call)
                    and self._class(_callee(n.value))
                ):
                    return _callee(n.value)
            return None
        if isinstance(expr, ast.Attribute):
            owner = self._receiver_class(expr.value, fn)
            for c in self._mro(owner) if owner else []:
                info = self._class(c)
                if info and expr.attr in info.attr_types:
                    return info.attr_types[expr.attr]
            return None
        if isinstance(expr, ast.Call) and self._class(_callee(expr)):
            return _callee(expr)
        return None

    def targets(self, call: ast.Call, fn: _Fn) -> list[_Fn]:
        """The project functions a call can reach. [] = library code, or a
        name the call site cannot disambiguate — both are opaque."""
        name = _callee(call)
        f = call.func
        if isinstance(f, ast.Name) and (self._class(name) or name == "cls"):
            cls = fn.cls if name == "cls" else name
            for c in self._mro(cls) if cls else []:
                inits = [t for t in self.by_name.get("__init__", []) if t.cls == c]
                if inits:
                    return inits
            return []
        cands = self.by_name.get(name) or []
        if isinstance(f, ast.Name):
            cands = [c for c in cands if c.cls is None]
            if len(cands) > 1:
                same = [c for c in cands if c.path == fn.path]
                if same:
                    return same
                mod = self.imports.get(fn.path, {}).get(name)
                if mod:
                    stem = mod.replace(".", "/")
                    cands = [c for c in cands if c.path[:-3].endswith(stem)]
            return cands if len(cands) == 1 else []
        if len(cands) <= 1:
            return cands
        cls = self._receiver_class(f.value, fn)
        if cls:
            for c in self._mro(cls):
                narrowed = [t for t in cands if t.cls == c]
                if narrowed:
                    return narrowed
            return []
        if isinstance(f.value, ast.Name):  # module-qualified: save.state_to_dict
            mod = [
                t
                for t in cands
                if t.cls is None and t.path.rsplit("/", 1)[-1][:-3] == f.value.id
            ]
            if len(mod) == 1:
                return mod
        return []

    @staticmethod
    def _bound(call: ast.Call, t: _Fn) -> bool:
        return t.is_method and (
            isinstance(call.func, ast.Attribute) or t.name == "__init__"
        )

    def _param(self, call: ast.Call, t: _Fn, node: ast.AST) -> Optional[str]:
        pos = next((i for i, a in enumerate(call.args) if a is node), None)
        if pos is None or any(isinstance(a, ast.Starred) for a in call.args[:pos]):
            return None
        pos += 1 if self._bound(call, t) else 0
        return t.params[pos] if pos < len(t.params) else None

    def _arg(self, call: ast.Call, t: _Fn, param: str) -> Optional[ast.AST]:
        for kw in call.keywords:
            if kw.arg == param:
                return kw.value
        idx = t.params.index(param) - (1 if self._bound(call, t) else 0)
        if 0 <= idx < len(call.args) and not any(
            isinstance(a, ast.Starred) for a in call.args[: idx + 1]
        ):
            return call.args[idx]
        return None

    def callers(self, fn: _Fn) -> list[tuple[_Fn, ast.Call]]:
        key = fn.cls if fn.name == "__init__" and fn.cls else fn.name
        return [
            (caller, call)
            for caller, call in self.call_sites.get(key, [])
            if any(t is fn for t in self.targets(call, caller))
        ]

    @staticmethod
    def _skey(stack: Stack) -> tuple:
        return tuple((id(f), id(c)) for f, c in stack)

    @staticmethod
    def _recursive(stack: Stack, call: ast.Call) -> bool:
        return any(c is call for _, c in stack)

    # ── anchors ────────────────────────────────────────────────────────

    def anchors(
        self, fns: list[_Fn], modules: Optional[set[str]] = None
    ) -> tuple[list, list]:
        """(dump anchors, load anchors) inside ``fns``: (fn, payload
        expression) and (fn, the load call), each under its innermost
        function. ``modules`` narrows them to the contract's format."""
        dump_mods = _DUMP_MODULES & modules if modules else _DUMP_MODULES
        load_mods = _LOAD_MODULES & modules if modules else _LOAD_MODULES
        dumps: list[tuple[_Fn, ast.AST]] = []
        loads: list[tuple[_Fn, ast.AST]] = []
        for fn in fns:
            for n in _own_nodes(fn.node):
                if _module_call(n, dump_mods, _DUMP_ATTRS) and n.args:
                    dumps.append((fn, n.args[0]))
                elif _module_call(n, load_mods, _LOAD_ATTRS):
                    loads.append((fn, n))
        return dumps, loads

    def functions_in(self, files: set[str]) -> list[_Fn]:
        return [fn for fn in self.funcs if fn.path in files]

    def reachable(self, roots: list[_Fn]) -> list[_Fn]:
        """Every project function the roots can call, transitively."""
        seen: dict[int, _Fn] = {id(f): f for f in roots}
        todo = list(roots)
        while todo:
            fn = todo.pop()
            for n in _own_nodes(fn.node):
                if isinstance(n, ast.Call):
                    for t in self.targets(n, fn):
                        if id(t) not in seen:
                            seen[id(t)] = t
                            todo.append(t)
        return list(seen.values())

    def anchor_file(self, fn: _Fn, anchor: ast.AST, dump: bool) -> Optional[str]:
        """The basename of the file a dump/load anchor touches, when the code
        names it with a constant (literal, module constant, parameter
        default); None when it cannot be told."""
        handle: Optional[ast.AST] = None
        if dump:
            call = fn.parents.get(id(anchor))
            if isinstance(call, ast.Call) and len(call.args) > 1:
                handle = call.args[1]
        elif isinstance(anchor, ast.Call) and anchor.args:
            handle = anchor.args[0]
        if handle is None:
            return None
        opened = self._opened_path(handle, fn)
        return self._const_path(opened, fn) if opened is not None else None

    def _opened_path(self, handle: ast.AST, fn: _Fn) -> Optional[ast.AST]:
        """``with open(P) as f`` / ``f = open(P)`` / ``Path(P).read_text()``."""
        if isinstance(handle, ast.Call):
            if _callee(handle) == "open" and handle.args:
                return handle.args[0]
            f = handle.func
            if isinstance(f, ast.Attribute) and f.attr in ("read_text", "open"):
                inner = f.value
                if (
                    isinstance(inner, ast.Call)
                    and _callee(inner) == "Path"
                    and inner.args
                ):
                    return inner.args[0]
                return inner
            return None
        if not isinstance(handle, ast.Name):
            return None
        for n in ast.walk(fn.node):
            if isinstance(n, (ast.With, ast.AsyncWith)):
                for item in n.items:
                    v = item.optional_vars
                    if isinstance(v, ast.Name) and v.id == handle.id:
                        return self._opened_path(item.context_expr, fn)
            if isinstance(n, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == handle.id for t in n.targets
            ):
                return self._opened_path(n.value, fn)
        return None

    def _const_path(self, expr: ast.AST, fn: _Fn) -> Optional[str]:
        s = _const_str(expr)
        if s is None and isinstance(expr, ast.Name):
            s = self.constants.get(fn.path, {}).get(expr.id)
            if s is None:
                a = fn.node.args
                pos = a.posonlyargs + a.args
                defaults = (
                    dict(zip([x.arg for x in pos][-len(a.defaults) :], a.defaults))
                    if a.defaults
                    else {}
                )
                for x, d in zip(a.kwonlyargs, a.kw_defaults):
                    if d is not None:
                        defaults[x.arg] = d
                if expr.id in defaults:
                    s = _const_str(defaults[expr.id])
        elif s is None and isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            return self._const_path(expr.right, fn)
        elif s is None and isinstance(expr, ast.Call) and expr.args:
            if _callee(expr) in ("join", "Path"):
                return self._const_path(expr.args[-1], fn)
        return s.replace("\\", "/").rsplit("/", 1)[-1] if s else None

    # ── writer side: what builds the payload ───────────────────────────

    def written(self, expr: ast.AST, fn: _Fn, stack: Stack = ()) -> Any:
        return self._guard(
            ("w", id(fn), id(expr), self._skey(stack)),
            lambda: self._written(expr, fn, stack),
            _CYCLE,
        )

    def _written(self, expr: ast.AST, fn: _Fn, stack: Stack) -> Any:
        if isinstance(expr, ast.Dict):
            tree: Any = {_SITES: {fn.path}}
            for k, v in zip(expr.keys, expr.values):
                if k is None:  # {**other}
                    tree = _merge(tree, _leaf(self.written(v, fn, stack)))
                    continue
                ks = _const_str(k)
                slot = ks if ks is not None else _ELEM
                sub = _leaf(self.written(v, fn, stack))
                tree[slot] = _merge(tree[slot], sub) if slot in tree else sub
                if ks is not None and self._derived(v, fn):
                    tree.setdefault(_DERIVED, set()).add(ks)
            return tree
        if isinstance(expr, ast.DictComp):
            ks = _const_str(expr.key)
            return {
                _SITES: {fn.path},
                (ks if ks is not None else _ELEM): _leaf(
                    self.written(expr.value, fn, stack)
                ),
            }
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            elems = [self.written(e, fn, stack) for e in expr.elts]
            real = [e for e in elems if e is not None and e is not _CYCLE]
            if not real:
                return None
            merged = real[0]
            for e in real[1:] + ([None] if None in elems else []):
                merged = _merge(merged, e)
            return {_SITES: {fn.path}, _ELEM: merged}
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            sub = _leaf(self.written(expr.elt, fn, stack))
            return None if sub is None else {_SITES: {fn.path}, _ELEM: sub}
        if isinstance(expr, ast.IfExp):
            return _merge(
                self.written(expr.body, fn, stack), self.written(expr.orelse, fn, stack)
            )
        if isinstance(expr, ast.BoolOp):
            out: Any = _CYCLE
            for v in expr.values:
                out = _merge(out, self.written(v, fn, stack))
            return out
        if isinstance(expr, ast.Call):
            return self._written_call(expr, fn, stack)
        if isinstance(expr, ast.Name):
            return self._written_name(expr.id, fn, stack)
        return None  # attribute, subscript, constant: no key information

    def _derived(self, value: ast.AST, fn: _Fn) -> bool:
        """Whether a written value comes straight from an attribute its class
        declares computed. Follows one-argument wrappers (``int(p.attack)``,
        ``_as_int(getattr(p, "attack", 0))``) to the source."""
        while isinstance(value, ast.Call):
            if _callee(value) == "getattr" and len(value.args) >= 2:
                attr = _const_str(value.args[1])
                obj = value.args[0]
                break
            positional = [a for a in value.args if not isinstance(a, ast.Starred)]
            if len(positional) != 1:
                return False
            value = positional[0]
        else:
            if not isinstance(value, ast.Attribute):
                return False
            attr, obj = value.attr, value.value
        if attr is None:
            return False
        cls = self._receiver_class(obj, fn)
        return any(
            attr in (self._class(c).derived if self._class(c) else ())
            for c in (self._mro(cls) if cls else [])
        )

    def _written_call(self, call: ast.Call, fn: _Fn, stack: Stack) -> Any:
        name = _callee(call)
        if name == "dict" and isinstance(call.func, ast.Name):
            tree: Any = {_SITES: {fn.path}}
            for kw in call.keywords:
                if kw.arg is None:
                    tree = _merge(tree, _leaf(self.written(kw.value, fn, stack)))
                else:
                    tree[kw.arg] = _leaf(self.written(kw.value, fn, stack))
                    if self._derived(kw.value, fn):
                        tree.setdefault(_DERIVED, set()).add(kw.arg)
            for a in call.args:
                tree = _merge(tree, _leaf(self.written(a, fn, stack)))
            return tree
        if name == "asdict":
            cls = self._receiver_class(call.args[0], fn) if call.args else None
            fields_ = self._dataclass_fields(cls) if cls else None
            if fields_ is None:
                return None
            # asdict converts nested dataclasses too: their keys stay unseen.
            tree = {_SITES: {fn.path}, **{f: None for f in fields_}}
            derived = set()
            for c in self._mro(cls):
                info = self._class(c)
                derived |= info.derived if info else set()
            if derived & fields_:
                tree[_DERIVED] = derived & fields_
            return tree
        if _module_call(call, _DUMP_MODULES, {"dumps"}) and call.args:
            return self.written(call.args[0], fn, stack)
        if self._recursive(stack, call):
            return _CYCLE
        frame = stack + ((fn, call),)
        out: Any = None
        first = True
        for t in self.targets(call, fn):
            for ret in t.own_returns:
                sub = self.written(ret.value, t, frame)
                out = sub if first else _merge(out, sub)
                first = False
        return out

    def _written_name(self, var: str, fn: _Fn, stack: Stack) -> Any:
        out: Any = _CYCLE
        found = False

        def add(sub: Any) -> None:
            nonlocal out, found
            out = _merge(out, sub)
            found = True

        for n in ast.walk(fn.node):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id == var:
                        add(self.written(n.value, fn, stack))
                    elif (
                        isinstance(t, ast.Subscript)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == var
                    ):
                        ks = _const_str(t.slice)
                        sub = _leaf(self.written(n.value, fn, stack))
                        slot = ks if ks is not None else _ELEM
                        rec: dict = {_SITES: {fn.path}, slot: sub}
                        if ks is not None and self._derived(n.value, fn):
                            rec[_DERIVED] = {ks}
                        add(rec)
            elif isinstance(n, (ast.AnnAssign, ast.AugAssign)) and n.value is not None:
                if isinstance(n.target, ast.Name) and n.target.id == var:
                    add(self.written(n.value, fn, stack))
            elif (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == var
            ):
                if n.func.attr == "update":
                    for a in n.args:
                        add(self.written(a, fn, stack))
                    for kw in n.keywords:
                        add({_SITES: {fn.path}, kw.arg: None} if kw.arg else None)
                elif n.func.attr == "setdefault" and n.args:
                    ks = _const_str(n.args[0])
                    sub = (
                        _leaf(self.written(n.args[1], fn, stack))
                        if len(n.args) > 1
                        else None
                    )
                    add({_SITES: {fn.path}, (ks if ks is not None else _ELEM): sub})
        if var in fn.params:
            if stack:  # back to the call that brought us here
                caller, call = stack[-1]
                arg = self._arg(call, fn, var)
                add(self.written(arg, caller, stack[:-1]) if arg is not None else None)
            else:  # the anchor's own parameter: every caller
                for caller, call in self.callers(fn):
                    arg = self._arg(call, fn, var)
                    add(self.written(arg, caller, ()) if arg is not None else None)
        if not found or out is _CYCLE:
            return None if not found else _CYCLE
        return out

    # ── reader side: what reads the loaded payload ─────────────────────

    def read_value(self, node: ast.AST, fn: _Fn, stack: Stack = ()) -> dict:
        """Keys read off the payload VALUE at ``node``, following where it
        goes. Any use this cannot interpret opens the level."""
        return self._guard(
            ("r", id(fn), id(node), self._skey(stack)),
            lambda: self._read_value(node, fn, stack),
            {},
        )

    def _read_value(self, node: ast.AST, fn: _Fn, stack: Stack) -> dict:
        parent = fn.parents.get(id(node))
        tree: dict = {}

        def read(k: str, sub: dict) -> dict:
            tree.setdefault(_SITES, set()).add(fn.path)
            tree[k] = _merge_read(tree.get(k), sub)
            return tree

        if parent is None:
            return _opaque()
        if isinstance(parent, ast.Subscript):
            if parent.slice is node:
                return {}  # used AS a key: a leaf use
            if not isinstance(parent.ctx, ast.Load):
                return {}  # a store into the payload is not a read
            ks = _const_str(parent.slice)
            slot = ks if ks is not None else _ELEM
            return read(slot, self.read_value(parent, fn, stack))
        if isinstance(parent, ast.Attribute):
            gp = fn.parents.get(id(parent))
            if not (isinstance(gp, ast.Call) and gp.func is parent):
                return _opaque()
            if parent.attr in ("get", "pop", "setdefault"):
                if not gp.args:
                    return _opaque()
                ks = _const_str(gp.args[0])
                slot = ks if ks is not None else _ELEM
                return read(slot, self.read_value(gp, fn, stack))
            if parent.attr in ("items", "values"):
                return read(
                    _ELEM, self._iterated(gp, fn, stack, parent.attr == "items")
                )
            if parent.attr == "keys":
                return read(_ELEM, {})
            if parent.attr == "copy":
                return self.read_value(gp, fn, stack)
            return _opaque()
        if isinstance(parent, ast.Compare):
            if any(c is node for c in parent.comparators) and any(
                isinstance(op, (ast.In, ast.NotIn)) for op in parent.ops
            ):
                ks = _const_str(parent.left)
                if ks is not None:
                    return read(ks, {})
            return {}  # compared as a value
        if isinstance(parent, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
            if getattr(parent, "value", None) is not node:
                return _opaque()
            targets = (
                parent.targets if isinstance(parent, ast.Assign) else [parent.target]
            )
            for t in targets:
                if isinstance(t, ast.Name):
                    tree = _merge_read(tree, self.read_name(t.id, fn, stack))
                else:  # stored on an attribute / unpacked: used wholesale
                    tree[_OPEN] = True
            return tree
        if isinstance(parent, ast.Call):
            if node is parent.func:
                return _opaque()
            return self._read_into_call(parent, node, None, fn, stack)
        if isinstance(parent, ast.keyword):
            call = fn.parents.get(id(parent))
            if not isinstance(call, ast.Call):
                return _opaque()
            if parent.arg is None:
                return self._read_unpacked(call, fn, stack)
            return self._read_into_call(call, node, parent.arg, fn, stack)
        if isinstance(parent, ast.Return):
            if not any(parent is r for r in fn.own_returns):
                return _opaque()  # a nested function's return
            if stack:  # back to the call that brought us here
                caller, call = stack[-1]
                return self.read_value(call, caller, stack[:-1])
            callers = self.callers(fn)
            if not callers:
                return _opaque()
            for caller, call in callers:
                tree = _merge_read(tree, self.read_value(call, caller, ()))
            return tree
        if isinstance(parent, ast.Tuple) and isinstance(parent.ctx, ast.Load):
            # `return obj, data` → `player, save_data = load_save(...)`: the
            # payload is ONE position of the tuple; follow that position.
            ret = fn.parents.get(id(parent))
            if not (
                isinstance(ret, ast.Return) and any(ret is r for r in fn.own_returns)
            ):
                return _opaque()
            idx = next(i for i, e in enumerate(parent.elts) if e is node)
            sites = [stack[-1]] if stack else self.callers(fn)
            if not sites:
                return _opaque()
            for caller, call in sites:
                tree = _merge_read(
                    tree,
                    self._unpacked_at(
                        call, caller, stack[:-1] if stack else (), idx, len(parent.elts)
                    ),
                )
            return tree
        if isinstance(parent, ast.IfExp):
            if parent.test is node:
                return {}
            return self.read_value(parent, fn, stack)
        if isinstance(parent, (ast.BoolOp, ast.Await)):
            return self.read_value(parent, fn, stack)
        if isinstance(parent, (ast.If, ast.While, ast.Assert)) and parent.test is node:
            return {}
        if isinstance(parent, (ast.UnaryOp, ast.BinOp, ast.FormattedValue, ast.Expr)):
            return {}
        if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node:
            return read(_ELEM, self._loop_target(parent, fn, stack))
        return _opaque()

    def _unpacked_at(
        self, call: ast.Call, fn: _Fn, stack: Stack, idx: int, n: int
    ) -> dict:
        """Reads of position ``idx`` of a returned tuple at its call site."""
        asg = fn.parents.get(id(call))
        if isinstance(asg, ast.Assign) and asg.value is call and len(asg.targets) == 1:
            t = asg.targets[0]
            if (
                isinstance(t, (ast.Tuple, ast.List))
                and len(t.elts) == n
                and isinstance(t.elts[idx], ast.Name)
            ):
                return self.read_name(t.elts[idx].id, fn, stack)
        return _opaque()

    def read_name(
        self, var: str, fn: _Fn, stack: Stack, scope: Optional[ast.AST] = None
    ) -> dict:
        tree: dict = {}
        for n in ast.walk(scope if scope is not None else fn.node):
            if isinstance(n, ast.Name) and n.id == var and isinstance(n.ctx, ast.Load):
                tree = _merge_read(tree, self.read_value(n, fn, stack))
        return tree

    def _iterated(self, call: ast.Call, fn: _Fn, stack: Stack, pairs: bool) -> dict:
        """Element reads for ``for k, v in payload.items()`` / ``.values()``."""
        loop = fn.parents.get(id(call))
        if not (isinstance(loop, (ast.For, ast.comprehension)) and loop.iter is call):
            return _opaque()
        return self._loop_target(loop, fn, stack, pairs=pairs)

    def _loop_target(
        self, loop: ast.AST, fn: _Fn, stack: Stack, pairs: bool = False
    ) -> dict:
        """Reads of a loop variable, within that loop only — sibling loops
        reuse names (`for key, value in …` four times in one function)."""
        t = loop.target
        if pairs:
            if not (isinstance(t, ast.Tuple) and len(t.elts) == 2):
                return _opaque()
            t = t.elts[1]
        if not isinstance(t, ast.Name):
            return _opaque()
        scope = loop if isinstance(loop, ast.For) else fn.parents.get(id(loop))
        return self.read_name(t.id, fn, stack, scope=scope)

    def _read_into_call(
        self, call: ast.Call, node: ast.AST, kw: Optional[str], fn: _Fn, stack: Stack
    ) -> dict:
        name = _callee(call)
        if isinstance(call.func, ast.Name) and name in _SCALAR_SINKS:
            return {}
        if name in _PASSTHROUGH and not self.by_name.get(name):
            return self.read_value(call, fn, stack)
        if _module_call(call, _DUMP_MODULES, _DUMP_ATTRS):
            return _opaque()  # re-serialized wholesale
        if self._recursive(stack, call):
            return {}
        frame = stack + ((fn, call),)
        tree: dict = {}
        mapped = False
        for t in self.targets(call, fn):
            param = kw if kw is not None else self._param(call, t, node)
            if param and param in t.params:
                tree = _merge_read(tree, self.read_name(param, t, frame))
                mapped = True
        return tree if mapped else _opaque()

    def _read_unpacked(self, call: ast.Call, fn: _Fn, stack: Stack) -> dict:
        """``Target(**payload)``: the keys are the target's parameters."""
        if self._recursive(stack, call):
            return {}
        frame = stack + ((fn, call),)
        tree: dict = {}
        mapped = False
        for t in self.targets(call, fn):
            if t.node.args.kwarg is not None:
                return _opaque()
            skip = 1 if self._bound(call, t) else 0
            for p in t.params[skip:]:
                tree.setdefault(_SITES, set()).add(fn.path)
                tree[p] = _merge_read(tree.get(p), self.read_name(p, t, frame))
            mapped = True
        if mapped:
            return tree
        name = _callee(call)
        cls = fn.cls if name == "cls" else name
        fields_ = self._dataclass_fields(cls) if cls and self._class(cls) else None
        if fields_ is None:
            return _opaque()
        # A dataclass stores each value as given: nested keys unseen.
        return {_SITES: {fn.path}, **{f: _opaque() for f in fields_}}


# ══════════════════════════════════════════════════════════════════════
# Comparing the code against the contract
# ══════════════════════════════════════════════════════════════════════


def _book(tree: dict, contract_files: list[str], fallback: str) -> str:
    """The file where this level of the payload is built / read: a contract
    file when one is among them, else the first site."""
    sites = set(tree.get(_SITES) or ())
    for p in contract_files:
        if p in sites:
            return p
    return sorted(sites)[0] if sites else fallback


def _compare(
    declared: DeclaredNode,
    w: Any,
    r: Any,
    path: str,
    ctx: dict,
    out: list[tuple[str, str]],
) -> None:
    if not isinstance(w, dict) or not isinstance(r, dict):
        ctx["unjudged"].append(path)  # a side with no key information here
        return
    if declared.is_collection:
        if declared.element is not None:
            if _ELEM in w and _ELEM in r:
                _compare(declared.element, w[_ELEM], r[_ELEM], f"{path}[*]", ctx, out)
            else:
                ctx["unjudged"].append(f"{path}[*]")
        return
    # Dynamic keys at a RECORD level (payload[var], `for k in payload`) mean
    # any key may be written / read: an absence there proves nothing.
    w_open = bool(w.get(_OPEN)) or _ELEM in w
    r_open = bool(r.get(_OPEN)) or _ELEM in r
    # Only a DISAGREEMENT between the writer and the loader is reported. A
    # declared key that both halves spell differently (the design's nested
    # `player: {health}` saved flat as `player_health` by both) is design
    # drift, not a broken round trip — reporting it would send a repair to
    # restructure a pair that already agrees (tier_20260730 qwen3.5).
    derived = set(w.get(_DERIVED) or ())
    where = f"{path}." if path else ""
    files = ctx["files"]
    head = f"serialized round trip ({ctx['name']}, declared owner {ctx['owner']})"
    writer = _book(w, files, ctx["writer"])
    reader = _book(r, files, ctx["reader"])
    for k, child in declared.fields.items():
        wk, rk = k in w, k in r
        if (wk or not w_open) and (rk or not r_open):
            ctx["judged"] += 1
        else:
            ctx["unjudged"].append(f"{where}{k}")
        if (
            wk
            and not rk
            and not r_open
            and k not in derived
            and k.lower() not in _AUDIT_FIELDS
        ):
            out.append(
                (
                    reader,
                    f"{head}: `{where}{k}` is written into the saved payload "
                    f"({writer}) but the loader in {reader} never reads it "
                    f"back — a save whose loader ignores what the writer "
                    f"stored restores an incomplete world. File to fix: "
                    f"{reader}.",
                )
            )
        elif rk and not wk and not w_open and not ctx["reads_uncertain"]:
            out.append(
                (
                    writer,
                    f"{head}: the loader in {reader} reads `{where}{k}` but the "
                    f"writer in {writer} never puts it in the saved payload. "
                    f"File to fix: {writer}.",
                )
            )
        if wk and rk and child is not None:
            _compare(child, w[k], r[k], f"{where}{k}", ctx, out)


def roundtrip_contract_findings(
    sources: dict[str, str],
    contracts: list[PersistedContract],
) -> list[tuple[str, str]]:
    """(file to fix, message) for every declared persisted contract whose code
    disagrees with it. ``sources`` is every written file (code and data). A
    contract whose writer or reader cannot be followed produces nothing —
    logged, never charged to a file."""
    if not contracts:
        return []
    paths = [p for p in sources if p.endswith(".py")]
    data_files = {
        p.rsplit("/", 1)[-1] for p in sources if p.lower().endswith(_DATA_EXT)
    }
    flow = PayloadFlow({p: sources[p] for p in paths})
    findings: list[tuple[str, str]] = []
    for c in contracts:
        owner = _resolve_path(c.owner, paths)
        consumers = [p for p in (_resolve_path(x, paths) for x in c.consumers) if p]
        files = [p for p in dict.fromkeys([owner, *consumers]) if p]
        if not files:
            logger.info("round trip %s: none of its files are written", c.name)
            continue
        broken = [p for p in files if p in flow.unparsed]
        if broken:
            logger.info(
                "round trip %s: %s does not parse — the per-file gate owns it",
                c.name,
                ", ".join(broken),
            )
            continue
        # Where the save and load happen: the owner, then the contract's
        # other files, then whatever those files call.
        roots = flow.functions_in({owner} if owner else set())
        dumps, loads = flow.anchors(roots, c.modules)
        for scope in (
            lambda: flow.functions_in(set(files)),
            lambda: flow.reachable(flow.functions_in(set(files))),
        ):
            if dumps and loads:
                break
            more_d, more_l = flow.anchors(scope(), c.modules)
            dumps = dumps or more_d
            loads = loads or more_l
        # A load of a data file the project SHIPS (world.yaml) is not the
        # save coming back; a dump into one is not the save going out.
        target = c.persisted_file

        def _other_file(fn: _Fn, anchor: ast.AST, dump: bool) -> bool:
            f = flow.anchor_file(fn, anchor, dump)
            return f is not None and f in data_files and f != target

        dumps = [(fn, a) for fn, a in dumps if not _other_file(fn, a, True)]
        loads = [(fn, a) for fn, a in loads if not _other_file(fn, a, False)]
        if not dumps or not loads:
            logger.info(
                "round trip %s: no %s reachable from %s — left to the runtime "
                "save/load goals",
                c.name,
                "serializer call" if not dumps else "deserializer call",
                ", ".join(files),
            )
            continue
        w: Any = None
        for i, (fn, expr) in enumerate(dumps):
            sub = _leaf(flow.written(expr, fn))
            w = sub if i == 0 else _merge(w, sub)
        r: dict = {}
        for fn, node in loads:
            r = _merge_read(r, flow.read_value(node, fn))
        if not _has_keys(w) or not _has_keys(r):
            logger.info(
                "round trip %s: the %s cannot be followed statically — left to "
                "the runtime save/load goals, not charged to any file",
                c.name,
                "writer" if not _has_keys(w) else "loader",
            )
            continue
        ctx = {
            "name": c.name,
            "owner": owner or c.owner,
            "files": files,
            "writer": dumps[0][0].path,
            "reader": loads[0][0].path,
            # Two loads whose files cannot be told apart: a key one of them
            # reads is not proof the SAVE's loader reads it, so "read but
            # never written" is not judged. The union still proves what NO
            # loader reads.
            "reads_uncertain": len(loads) > 1,
            "judged": 0,
            "unjudged": [],
        }
        before = len(findings)
        _compare(c.shape, w, r, "", ctx, findings)
        # A pass and a blind pass must not read the same: say how much of the
        # declared shape was actually compared.
        logger.info(
            "round trip %s: %d declared key(s) compared, %d finding(s)%s",
            c.name,
            ctx["judged"],
            len(findings) - before,
            (
                f"; not judgeable statically: {', '.join(ctx['unjudged'])}"
                if ctx["unjudged"]
                else ""
            ),
        )
    return findings
