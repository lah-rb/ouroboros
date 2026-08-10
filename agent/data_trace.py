"""Data-aware symbol trace — surface the data a traced code symbol reads.

`diagnose_issue` traces *code* symbols (tree-sitter), so a data-driven bug
(e.g. a room's empty ``items: []`` in ``world_data.yaml``) is invisible — the
model blames the loader code, fails, then re-diagnoses the data, wasting a fix
round. This module closes that gap: when a traced symbol loads/reads a data
file, it surfaces that file's parsed, KEY-SCOPED content as trace evidence, so
data becomes an equal suspect to code.

The connection makes relevance/scope/gating structural rather than heuristic:
the data shown is exactly what the traced code reads. Multi-hop follows
``self.X = load(...)`` so a symbol reading ``self.rooms`` still reaches the data.

Strictly additive: every path is wrapped so a failure degrades to "no data
evidence", never a broken code trace.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from typing import Any

from ruamel.yaml import YAML

from agent.data_ops import DataOpsError, Document, detect_fmt
from agent.schema_extract import (
    _DATA_PATH_SUFFIXES,
    extract_attr_reads,
    extract_data_skeleton,
    extract_key_access_patterns,
    extract_model_defs,
    extract_module_constants,
    find_data_loads,
    find_model_instantiation_keys,
    parses_data,
)

logger = logging.getLogger(__name__)

_MAX_DATA_FILES = 2  # cap connected files per trace (common case: 1)
_WHOLE_FILE_MAX = 600  # show a keyless small data file whole below this size
_MAX_MODEL_FILES = 6  # cap sibling code files read to build the model index


async def build_data_trace_evidence(
    *,
    target_sym: dict,
    symbol_table: list[dict],
    file_content: str,
    file_context: dict | None,
    effects: Any,
    token_cap: int = 1200,
) -> str:
    """Produce the ``## Connected data`` evidence block, or ``""`` for no-op.

    Algorithm: detect the data file(s) the traced symbol loads (direct, then
    multi-hop via ``self.X``), read + parse them, scope to the keys the code
    accesses, and format. ``target_sym`` is a symbol_table entry (has ``body``,
    ``name``, ``parent``).
    """
    body = target_sym.get("body", "") or ""
    if not body:
        return ""

    module_constants = extract_module_constants(file_content)
    via_name = target_sym.get("name", "this symbol")

    # 1. Direct loads in the symbol body.
    resolved: list[tuple[str, str]] = []
    for load in find_data_loads(body):
        path = _resolve_data_path(load, file_context, module_constants)
        if path:
            resolved.append((path, f"loaded by {via_name}"))

    # 2. Multi-hop — only when no direct load (direct wins).
    if not resolved:
        resolved = _multihop_loads(
            target_sym, symbol_table, file_context, module_constants
        )

    # 3. Param/dynamic-path fallback: the symbol PARSES data but the path is a
    #    parameter (`def load(filepath): yaml.safe_load(open(filepath))`) — the
    #    literal lives at the call site, not the body (the most common loader
    #    shape). Surface the project's data files so the connection still fires.
    if not resolved and parses_data(body):
        for path in _project_data_files(file_context)[:_MAX_DATA_FILES]:
            resolved.append((path, "loaded here (path resolved at runtime)"))

    # Model-layer hop: the dict-key bridge found no data file. When the loader
    # parses data into typed model instances and this symbol reads them as
    # ATTRIBUTES (``npc.dialogue_nodes``), connect through the model instead.
    if not resolved:
        try:
            return await _build_model_trace_evidence(
                target_sym=target_sym,
                symbol_table=symbol_table,
                file_content=file_content,
                file_context=file_context,
                effects=effects,
                token_cap=token_cap,
            )
        except Exception:  # noqa: BLE001 - additive; never break the code trace
            logger.debug("model-layer trace hop failed", exc_info=True)
            return ""

    # 3. Keys the traced function reads — the scoping signal.
    target_func = (target_sym.get("name", "") or "").rsplit(".", 1)[-1]
    patterns = extract_key_access_patterns(file_content)
    keys: list[str] = list(patterns.get(target_func, []))
    for k in patterns.get(f"{target_func}:required_fields", []) or []:
        if k not in keys:
            keys.append(k)

    # 4. One evidence block per connected file (capped).
    blocks: list[str] = []
    seen: set[str] = set()
    for path, via in resolved:
        if len(blocks) >= _MAX_DATA_FILES or path in seen:
            continue
        seen.add(path)
        content = await _read_data_content(path, file_context, effects)
        if not content:
            blocks.append(_format_note(path, via, "referenced but not found on disk"))
            continue
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        obj, err = _parse_data_object(ext, content)
        if obj is None:
            blocks.append(
                _format_note(
                    path, via, f"parse error — {err}" if err else "unparseable"
                )
            )
            continue
        scoped = _scope_to_keys(obj, keys, content, path, token_cap)
        blocks.append(_format_data_evidence(path, via, keys, scoped))

    return "\n\n".join(b for b in blocks if b)


# ── Path resolution ───────────────────────────────────────────────────


def _resolve_data_path(
    load: dict, file_context: dict | None, module_constants: dict[str, str]
) -> str | None:
    """Resolve a find_data_loads entry to a concrete path, or None."""
    if load.get("resolved") and load.get("path"):
        return load["path"]
    raw = load.get("raw_arg", "")
    val = module_constants.get(raw, "")
    if val and val.endswith(_DATA_PATH_SUFFIXES):
        return val
    return None


def _project_data_files(file_context: dict | None) -> list[str]:
    """Project data-file paths known to the diagnosis — from the already-loaded
    ``data_file_contents`` and the ``data_shapes`` listing. Used by the
    param/dynamic-path fallback when a loader's path isn't resolvable."""
    if not isinstance(file_context, dict):
        return []
    out: list[str] = []
    dfc = file_context.get("data_file_contents")
    if isinstance(dfc, dict):
        out.extend(dfc.keys())
    for ds in file_context.get("data_shapes", []) or []:
        if isinstance(ds, dict) and ds.get("file") and ds["file"] not in out:
            out.append(ds["file"])
    return out


def _multihop_loads(
    target_sym: dict,
    symbol_table: list[dict],
    file_context: dict | None,
    module_constants: dict[str, str],
) -> list[tuple[str, str]]:
    """Follow ``self.X = load(...)`` so a symbol reading ``self.rooms`` reaches
    the data. Scans SIBLING METHOD bodies in the same class (covers ``__init__``,
    where the assignment usually lives — interstitial-only scanning would miss
    it). Requires both the ``self.<attr> =`` assignment AND a recognized loader
    in the same method body before connecting.
    """
    body = target_sym.get("body", "") or ""
    parent = target_sym.get("parent", "") or ""
    if not parent:
        return []  # top-level function — no self.X to chain

    self_attrs = set(re.findall(r"self\.([A-Za-z_]\w*)", body))
    known = set()
    for s in symbol_table:
        nm = s.get("name", "")
        known.add(nm.rsplit(".", 1)[-1] if "." in nm else nm)
    candidates = self_attrs - known
    if not candidates:
        return []

    sibling_bodies = [
        s.get("body", "") or ""
        for s in symbol_table
        if s.get("parent") == parent and s.get("name") != target_sym.get("name")
    ]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for attr in candidates:
        assign_re = re.compile(r"self\." + re.escape(attr) + r"\s*=")
        for region in sibling_bodies:
            if not assign_re.search(region):
                continue
            for load in find_data_loads(region):
                path = _resolve_data_path(load, file_context, module_constants)
                if path and (attr, path) not in seen:
                    seen.add((attr, path))
                    out.append((path, f"via self.{attr}"))
    return out


def _reconcile_data_key(path: str, candidates: list[str]) -> str | None:
    """Match a directory-less query path to a directory-qualified candidate
    by basename — but ONLY when exactly one candidate matches.

    Static load-detection captures a loader's literal path token and drops
    any directory prefix a ``base_dir / "rooms.yaml"`` expression contributes,
    so ``build_data_trace_evidence`` asks for a bare ``rooms.yaml`` while the
    project stores it under the qualified key ``world/rooms.yaml``. This
    reconciles the two. On 0 or ≥2 basename matches (an ambiguous filename in
    two directories) it returns None, so the read degrades to the "not found"
    note rather than surfacing the wrong file."""
    base = os.path.basename(path)
    matches = [c for c in candidates if c != path and os.path.basename(c) == base]
    return matches[0] if len(matches) == 1 else None


async def _read_data_content(path: str, file_context: dict | None, effects: Any) -> str:
    """Prefer the already-materialized data_file_contents; fall back to effects.

    Reconciles a directory-less query (a bare ``rooms.yaml`` whose ``world/``
    prefix was dropped during static load detection) against the qualified
    keys/paths the project actually stores."""
    if isinstance(file_context, dict):
        dfc = file_context.get("data_file_contents")
        if isinstance(dfc, dict):
            content = dfc.get(path) or dfc.get(os.path.basename(path))
            if not content:
                key = _reconcile_data_key(path, list(dfc.keys()))
                if key:
                    content = dfc.get(key)
            if content:
                return content
    if effects is not None:
        # Given path first, then its unique directory-qualified match among
        # the project's known data files.
        candidates = [path]
        recon = _reconcile_data_key(path, _project_data_files(file_context))
        if recon:
            candidates.append(recon)
        for cand in candidates:
            try:
                fc = await effects.read_file(cand)
            except Exception:  # noqa: BLE001 - unreadable → try next / no evidence
                continue
            if getattr(fc, "exists", False):
                return getattr(fc, "content", "") or ""
    return ""


def _parse_data_object(ext: str, content: str) -> tuple[Any | None, str]:
    """Parse a data file to a Python object → (obj_or_None, error_detail).

    Mirrors pipeline_actions._parse_data_file's graceful import handling, but
    returns the object so we can surface it (or the parse error, which is itself
    the diagnosis)."""
    try:
        if ext == "json":
            return json.loads(content), ""
        if ext == "toml":
            try:
                import tomllib
            except ModuleNotFoundError:
                return None, "tomllib unavailable"
            return tomllib.loads(content), ""
        if ext in ("yaml", "yml"):
            try:
                import yaml
            except ImportError:
                return None, "PyYAML unavailable"
            return yaml.safe_load(content), ""
    except Exception as e:  # noqa: BLE001 - any parse error IS the diagnosis
        return None, f"{type(e).__name__}: {e}"
    return None, f"unrecognized ext '{ext}'"


# ── Model-layer hop ───────────────────────────────────────────────────


async def _build_model_trace_evidence(
    *,
    target_sym: dict,
    symbol_table: list[dict],
    file_content: str,
    file_context: dict | None,
    effects: Any,
    token_cap: int,
) -> str:
    """Connect a symbol that reads MODEL ATTRIBUTES to the data file its models
    are loaded from, and surface the connected subtree.

    Path: attributes read → distinctive model fields → owning model(s) → the
    data key those models are instantiated from → that subtree of the data file.
    Closes the code-vs-data gap for typed-object (dataclass/Pydantic) games,
    which the dict-key bridge can't see. Returns ``""`` on any miss.
    """
    body = target_sym.get("body", "") or ""
    if not body:
        return ""

    # 1. Attributes this symbol reads.
    func = (target_sym.get("name", "") or "").rsplit(".", 1)[-1]
    attr_map = extract_attr_reads(body)
    attrs: set[str] = set(attr_map.get(func, []))
    if not attrs:
        for vals in attr_map.values():
            attrs.update(vals)
    if not attrs:
        return ""

    # 2. Project model definitions (traced file + sibling code files).
    model_defs = await _collect_model_defs(file_content, file_context, effects)
    data_models = {c for c, d in model_defs.items() if _is_data_model(d)}
    if not data_models:
        return ""

    # 3. Distinctive accessed fields → owning model (a field owned by exactly
    #    ONE data model is a precise connection; shared names like id/name are
    #    ambiguous and ignored).
    owners: dict[str, set[str]] = {}
    for cls in data_models:
        for fld in model_defs[cls]["fields"]:
            owners.setdefault(fld, set()).add(cls)
    connected: set[str] = set()
    for a in attrs:
        own = owners.get(a)
        if own and len(own) == 1:
            connected |= own
    if not connected:
        return ""

    # 4. Parse the available data files.
    parsed: dict[str, tuple[Any, str]] = {}
    for path, content in _iter_data_files(file_context):
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        obj, _ = _parse_data_object(ext, content)
        if obj is not None:
            parsed[path] = (obj, content)
    if not parsed:
        return ""

    # 5. Origin key per connected model — the loader's instantiation key, else a
    #    pluralized class-name guess, validated against the actual data.
    scan_src = (
        file_content + "\n" + "\n".join(s.get("body", "") or "" for s in symbol_table)
    )
    inst_keys = find_model_instantiation_keys(scan_src, connected)
    model_origin: dict[str, tuple[str, str]] = {}
    for cls in connected:
        for key in [inst_keys.get(cls), *(_plural_guesses(cls))]:
            if not key:
                continue
            hit = next(
                (p for p, (obj, _c) in parsed.items() if _find_key_subtrees(obj, key)),
                None,
            )
            if hit:
                model_origin[cls] = (hit, key)
                break
    if not model_origin:
        return ""

    # 6. Per file: keep the most specific keys (drop ancestors), surface their
    #    connected subtrees.
    by_file: dict[str, set[str]] = {}
    for _cls, (path, key) in model_origin.items():
        by_file.setdefault(path, set()).add(key)

    blocks: list[str] = []
    for path, keys in by_file.items():
        obj, _content = parsed[path]
        chosen = _drop_ancestor_keys(obj, keys)
        subtrees: list[Any] = []
        for k in sorted(chosen):
            subtrees.extend(_find_key_subtrees(obj, k))
        # A key can occur both as a full definition (``npcs: [{...}]``) and as
        # bare id-references (``npcs: [captain_mara]`` / ``[]``) elsewhere. Keep
        # the structured (dict-bearing) occurrences when any exist.
        rich = [s for s in subtrees if _is_rich(s)]
        subtrees = rich or subtrees
        if not subtrees:
            continue
        value = subtrees[0] if len(subtrees) == 1 else subtrees
        scoped = _dump_value(value, token_cap)
        models_here = sorted(c for c, (p, _k) in model_origin.items() if p == path)
        blocks.append(_format_model_evidence(path, sorted(chosen), models_here, scoped))
    return "\n\n".join(b for b in blocks if b)


async def _collect_model_defs(
    file_content: str, file_context: dict | None, effects: Any
) -> dict[str, dict]:
    """Model class defs from the traced file plus sibling code files (models live
    in ``models.py`` etc.). Model-ish filenames first; capped read count."""
    defs: dict[str, dict] = dict(extract_model_defs(file_content))
    paths = [
        p
        for p in (file_context or {}).get("project_files", []) or []
        if isinstance(p, str) and p.endswith(".py")
    ]
    paths.sort(
        key=lambda p: (
            (
                0
                if re.search(
                    r"model|schema|entit|domain|type", os.path.basename(p).lower()
                )
                else 1
            ),
            p,
        )
    )
    read = 0
    for p in paths:
        if read >= _MAX_MODEL_FILES:
            break
        content = await _read_data_content(p, file_context, effects)
        if not content or content == file_content:
            continue
        read += 1
        for cls, d in extract_model_defs(content).items():
            defs.setdefault(cls, d)
    return defs


def _is_data_model(d: dict) -> bool:
    """A class that holds parsed data: a dataclass, a known model base, or simply
    a class with ≥2 annotated fields (and no behavior we care about)."""
    decs = [x.lower() for x in d.get("decorators", [])]
    if any("dataclass" in x for x in decs):
        return True
    bases = {b.rsplit(".", 1)[-1] for b in d.get("bases", [])}
    if bases & {"BaseModel", "Struct", "TypedDict", "Model"}:
        return True
    return len(d.get("fields", {})) >= 2


def _iter_data_files(file_context: dict | None) -> list[tuple[str, str]]:
    if not isinstance(file_context, dict):
        return []
    dfc = file_context.get("data_file_contents")
    if not isinstance(dfc, dict):
        return []
    return [(p, c) for p, c in dfc.items() if isinstance(c, str) and c]


def _plural_guesses(cls: str) -> list[str]:
    base = cls.lower()
    return [base + "s", base, base + "es"]


def _find_key_subtrees(
    data: Any, key: str, _depth: int = 0, _max: int = 6
) -> list[Any]:
    """Every value found at any occurrence of ``key`` (recursive)."""
    if _depth > _max:
        return []
    found: list[Any] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k == key:
                found.append(v)
            else:
                found.extend(_find_key_subtrees(v, key, _depth + 1, _max))
    elif isinstance(data, list):
        for item in data:
            found.extend(_find_key_subtrees(item, key, _depth + 1, _max))
    return found


def _is_rich(value: Any) -> bool:
    """True if the value carries structure (a dict, or a list containing one) —
    i.e. a model definition rather than a bare id-reference or empty list."""
    if isinstance(value, dict):
        return True
    if isinstance(value, list):
        return any(isinstance(i, dict) for i in value)
    return False


def _drop_ancestor_keys(data: Any, keys: set[str]) -> set[str]:
    """Drop a key whose subtree contains another connected key, so ``npcs``
    (nested under ``rooms``) wins over ``rooms`` — tighter, less redundant."""
    keep = set(keys)
    for k in keys:
        others = keys - {k}
        if not others:
            continue
        for sub in _find_key_subtrees(data, k):
            if any(_find_key_subtrees(sub, o) for o in others):
                keep.discard(k)
                break
    return keep or set(keys)


def _format_model_evidence(
    path: str, keys: list[str], models: list[str], scoped: str
) -> str:
    keys_str = ", ".join(keys) if keys else "(whole file)"
    models_str = ", ".join(models)
    return (
        f"## Connected data: {path} (via model attrs → {models_str}) — data keys: "
        f"{keys_str}\n"
        f"{scoped}\n"
        "(the traced code reads these as typed model fields — a bug may live in "
        "this data, not the code; the full file is a valid trace target)"
    )


# ── Key-scoping ───────────────────────────────────────────────────────
#
# We surface the data the code reads by slicing the REAL subtree out of the file
# with data_ops, then rendering it as-is. We do NOT reconstruct a pruned copy:
# rebuilding dropped each room's ``id`` and made a model hallucinate "rooms are
# missing their id fields", rewriting a 16KB YAML 7× to add ids that existed.
# Because a shown entry IS the real node, it can never lack its identifier — the
# id-drop bug class is structurally impossible, no special-casing required.

_MAX_KEY_MATCHES = 12  # cap recursive-descent matches per accessed key


def _scope_to_keys(
    data: Any, keys: list[str], content: str, path: str, max_chars: int
) -> str:
    """Slice the real subtree(s) the traced code reads, rendered as-is; keyless →
    whole file if small, else compact skeleton."""
    if not keys:
        if content and len(content) < _WHOLE_FILE_MAX:
            return content.strip()
        return extract_data_skeleton(content, path) or _dump_value(data, max_chars)

    try:
        doc = Document.read(content, detect_fmt(path, content))
    except DataOpsError:
        # A ruamel-vs-PyYAML parse divergence must never drop evidence.
        return extract_data_skeleton(content, path) or _dump_value(data, max_chars)

    rendered = _slice_keys(doc, keys, max_chars)
    if not rendered:
        # Accessed keys don't appear in this file — show its shape instead.
        return extract_data_skeleton(content, path) or _dump_value(data, max_chars)
    return rendered


def _slice_keys(doc: Document, keys: list[str], budget: int) -> str:
    """Gather the real subtree at each accessed key — top-level ``/key`` first,
    else the enclosing entry of each recursive-descent match — dedup by
    containment, and render within budget. A nested leaf (e.g. ``items``) renders
    inside its enclosing entry so the value shows with its id/context."""
    chosen: list[tuple[str, str | None, Any]] = []  # (pointer, label, value)
    included: list[str] = []
    for key in keys:
        top = "/" + _esc_token(key)
        sl = doc.slice(top)
        if sl.value is not None:
            candidates = [(top, key, sl.value)]
        else:
            try:
                matches = doc.query(f"$..{key}")[:_MAX_KEY_MATCHES]
            except DataOpsError:
                matches = []
            candidates = []
            for m in matches:
                parent_ptr, label = _parent_pointer(m.pointer)
                pval = doc.slice(parent_ptr).value if parent_ptr else None
                if pval is None:  # match is at the root level — show it directly
                    parent_ptr, label, pval = (
                        m.pointer,
                        _terminal_label(m.pointer),
                        m.value,
                    )
                candidates.append((parent_ptr, label, pval))
        for ptr, label, val in candidates:
            if val is None or any(_within(ptr, inc) for inc in included):
                continue
            included.append(ptr)
            chosen.append((ptr, label, val))

    out: list[str] = []
    remaining = budget
    for _ptr, label, val in chosen:
        if remaining <= 0:
            break
        block = _render_within_budget(label, val, remaining)
        if block:
            out.append(block)
            remaining -= len(block) + 1
    return "\n".join(out)


_NOTE_RESERVE = 24  # headroom kept for the "… (N more items)" note


def _render_within_budget(label: str | None, value: Any, budget: int) -> str:
    """Render a real subtree within budget. Overflow head-samples COMPLETE entries
    (each shown entry keeps its id/comments) + a ``… (N more items)`` note, rather
    than char-truncating mid-entry and severing an identifier. The note and any
    shown entry are never char-truncated; only a giant bare scalar is."""
    full = _render_subtree(label, value)
    if len(full) <= budget:
        return full

    items: list[tuple[Any, Any]] | None = None
    is_map = False
    if isinstance(value, list) and value:
        items = list(enumerate(value))
    elif isinstance(value, dict) and value:
        items, is_map = list(value.items()), True

    if items:
        # Grow the kept set against the WRAPPED render (accurate size, incl. the
        # label indent), reserving headroom for the note; always keep ≥1 entry.
        kept: list[tuple[Any, Any]] = []
        for it in items:
            kept.append(it)
            body_val = dict(kept) if is_map else [v for _k, v in kept]
            if (
                len(kept) > 1
                and len(_render_subtree(label, body_val)) > budget - _NOTE_RESERVE
            ):
                kept.pop()
                break
        more = len(items) - len(kept)
        body_val = dict(kept) if is_map else [v for _k, v in kept]
        body = _render_subtree(label, body_val)
        if more > 0:
            body = body.rstrip() + f"\n… ({more} more items)"
        return body

    return full[:budget].rstrip() + "\n… (truncated)"


def _plain(value: Any) -> Any:
    """Strip a node down to plain dict/list/scalar.

    ruamel represents what it knows (its own CommentedMap/Seq, and stdlib
    types); it raises RepresenterError on anything else. tomlkit's Table/Array
    are dict/list SUBCLASSES, so they traverse fine but do not dump — which is
    how a TOML trace slice degraded to a Python repr, `{'host': 'h'}`, the day
    Document.read started returning round-trip nodes. Rebuilding as plain
    containers costs the comments, which TOML slices never had here anyway.
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, str):
        return str(value)
    if isinstance(value, bool):  # before int — bool IS an int
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return value


def _dump_yaml(payload: Any) -> str:
    y = YAML()
    y.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    y.dump(payload, buf)
    return buf.getvalue().rstrip()


def _render_subtree(label: str | None, value: Any) -> str:
    """Serialize a sliced value as YAML via ruamel — handles round-trip nodes
    (comments preserved) and plain dict/list (JSON/TOML reads). Wraps under
    ``label`` when it is a mapping key, so the key name shows.

    Tries the node AS-IS first, because that is the only way ruamel emits a
    YAML slice's comments; falls back to a plain rebuild for backends ruamel
    cannot represent. `str()` is the last resort, not the second one — a
    Python repr in a trace is evidence the model has to decode."""
    payload: Any = {label: value} if label is not None else value
    if not isinstance(payload, (dict, list)):
        return str(payload).rstrip()
    try:
        return _dump_yaml(payload)
    except Exception:  # noqa: BLE001 — a node type ruamel has no representer for
        try:
            return _dump_yaml(_plain(payload))
        except Exception:  # noqa: BLE001 - rendering must never break the trace
            return str(value).rstrip()


def _esc_token(token: str) -> str:
    """Escape a key as one RFC-6901 pointer token (~ → ~0, / → ~1)."""
    return token.replace("~", "~0").replace("/", "~1")


def _ptr_tokens(pointer: str) -> list[str]:
    if not pointer or pointer == "/":
        return []
    return [t.replace("~1", "/").replace("~0", "~") for t in pointer.split("/")[1:]]


def _is_index(token: str) -> bool:
    return token.lstrip("-").isdigit()


def _terminal_label(pointer: str) -> str | None:
    toks = _ptr_tokens(pointer)
    if not toks or _is_index(toks[-1]):
        return None
    return toks[-1]


def _parent_pointer(pointer: str) -> tuple[str, str | None]:
    """Pointer of the enclosing entry + its label (None when the parent is a list
    element, so the entry renders bare rather than wrapped under an index)."""
    toks = _ptr_tokens(pointer)
    if len(toks) <= 1:
        return "", None
    parent = toks[:-1]
    ptr = "/" + "/".join(_esc_token(t) for t in parent)
    label = None if _is_index(parent[-1]) else parent[-1]
    return ptr, label


def _within(child: str, ancestor: str) -> bool:
    return bool(ancestor) and (child == ancestor or child.startswith(ancestor + "/"))


def _dump_value(value: Any, max_chars: int) -> str:
    """Render a Python object as compact, readable block text, truncated."""
    try:
        import yaml

        s = yaml.safe_dump(
            value, default_flow_style=False, sort_keys=False, allow_unicode=True
        ).rstrip()
    except Exception:  # noqa: BLE001 - PyYAML missing → JSON
        s = json.dumps(value, indent=2, default=str)
    if len(s) > max_chars:
        s = s[:max_chars].rstrip() + "\n… (truncated)"
    return s


# ── Formatting ────────────────────────────────────────────────────────


def _format_data_evidence(path: str, via: str, keys: list[str], scoped: str) -> str:
    keys_str = ", ".join(keys) if keys else "(whole file)"
    return (
        f"## Connected data: {path} ({via}) — keys accessed: {keys_str}\n"
        f"{scoped}\n"
        "(scoped to the keys this code reads — a bug may live in the data, not "
        "the code; the full file is a valid trace target)"
    )


def _format_note(path: str, via: str, note: str) -> str:
    return f"## Connected data: {path} ({via}) — {note}"


def render_data_file(content: str, path: str, budget: int) -> str:
    """Public scope-don't-truncate renderer for a whole data file.

    The interact tester's "map of the territory" was byte-cut at 4,000 chars
    (OPEN_TASKS §21): arm01's 13.7KB world.yaml reached the play-tester ~29%
    complete, mid-entry, and every functional test navigated a partial world.
    This is the ladder that replaces it:

      fits in budget      -> the whole file, verbatim
      over budget, parses -> COMPLETE head-sampled entries + "(N more items)"
                             (`_render_within_budget` — an entry is never cut
                             mid-identifier) with the schema skeleton appended
                             so the tail's SHAPE stays visible
      over budget, broken -> schema skeleton alone, else a hard-capped dump

    Never raises; returns "" only for empty content.
    """
    if not content:
        return ""
    if len(content) <= budget:
        return content
    try:
        doc = Document.read(content, detect_fmt(path, content))
    except Exception:  # noqa: BLE001 — parse failure takes the skeleton rung
        skel = extract_data_skeleton(content, path)
        if skel:
            return skel
        return content[:budget].rstrip() + "\n… (truncated)"
    # Descend through single-key wrappers so sampling happens at the level
    # that HAS siblings: a file shaped `rooms: {...60 rooms...}` must sample
    # the rooms, not return the one oversized top-level entry whole (the
    # sampler keeps >=1 complete child, and one child IS the whole file).
    label, value = None, doc.root
    while (
        isinstance(value, dict)
        and len(value) == 1
        and len(_render_subtree(label, value)) > budget
    ):
        ((k, v),) = value.items()
        if not isinstance(v, (dict, list)):
            break
        label, value = str(k), v
    sampled = _render_within_budget(label, value, budget)
    skel = extract_data_skeleton(content, path)
    if skel and "more items)" in sampled:
        return f"{sampled}\n\n(structure of the full file:)\n{skel}"
    return sampled
