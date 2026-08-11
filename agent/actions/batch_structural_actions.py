"""Batch structural creation: slice, gate, and book-keep a one-shot build.

The parallel structural mode generates EVERY architecture file in a
single completion (shared context → cross-file coherence), then this
module takes over deterministically:

  slice_batch_files       — parse `# === FILE:` blocks, diff against the
                            architecture manifest, write declared files
  run_batch_file_checks   — per-file gates: env syntax/import/lint for
                            code, parse-validity for data files
  apply_batch_results     — per-goal DirectiveReports, complete goals
                            that pass the same gate serial mode uses

Files the model omitted no longer simply stay missing. A short generation
climbs the fallback LADDER (see BATCH_MAX_ATTEMPTS below): keep a
substantially complete batch, resample a cheap short one, and only then
fall through to serial creation — so reaching serial is itself a signal
about the model rather than the routine cost of one bad sampling roll.
Files that fail their gate keep their goal incomplete with a failed
report; the sweep routes them to repair. Goals are never created or
destroyed here — only reported on.
"""

from __future__ import annotations

import ast as stdlib_ast
import logging
import re
from typing import Any

from agent import languages
from agent.actions.file_ops_actions import guarded_write_file
from agent.actions.pipeline_actions import (
    _load_env_config,
    _parse_data_file,
    action_run_validation_checks_from_env,
)
from agent.actions.pipeline_actions import _cap_diagnostic
from agent.markdown_fence import parse_file_blocks
from agent.models import FlowMeta, StepInput, StepOutput

logger = logging.getLogger(__name__)

# Extensions with no checkable content (mirrors pipeline _SKIP semantics
# for things like README.md a blueprint may legitimately include).
_UNCHECKED_NOTE = "no checker for this extension — accepted as written"

# ── INCREMENTAL BATCH FALLBACK (operator, 2026-08-05) ─────────────────
# A short generation used to fall STRAIGHT through to serial creation, and
# serial is where cross-file seam bugs are born — the files are authored
# alone, each blind to its siblings.
#
# Measured on the 13-arm GUARDIAN batch: the only two arms whose batch turn
# collapsed (hy3[g] 1/6 files at 700 tokens, glm-4.7-flash 3/8 at 3,521)
# produced the two WORST artifacts in the field — 38/47 and 30/47, both
# losses, both with no win path anywhere in the tree. Every arm with an
# intact batch turn scored 37-47 and took 7 of the 10 wins. hy3 is the
# clean natural experiment: the SAME config, temperature and prompt
# generated 7,843 tokens and 6/6 files as a contemplator (judged 47/47,
# played to victory) and 700 tokens and 1/6 as a grinder, `truncated:
# False` both times. One bad sampling roll, unrecoverable.
#
# So the fallback is now a ladder rather than a cliff:
#   1. significantly complete -> accept the partial, serial the remainder
#   2. cheap failed attempt   -> RESAMPLE the whole batch (<= 2 retries)
#   3. neither                -> serial, which now MEANS something: a
#      variable or struggling model, not one stray roll of the dice.
BATCH_MAX_ATTEMPTS = 3  # one generation + two retries

# "Significantly complete." qwen3-next salvaged 6/7 (86%) and placed
# mid-field; the two collapses sat at 17% and 38%. Two thirds separates
# them with room on either side.
BATCH_COVERAGE_FLOOR = 2 / 3

# "Cheap." A retry is worth taking when the failed attempt cost LESS than
# the serial fallback it would otherwise trigger — so the comparison is
# against real work avoided, not an absolute ceiling that would need
# re-tuning per model. Serial cost is measured, not assumed: 33 `create`
# generations across the 08-03/08-05 tier runs have a median of 2,332
# tokens each (p25 1,150 / p75 3,702). 2,000 is that rounded DOWN, so the
# estimate under-counts serial cost and the rule retries less eagerly than
# the evidence would license.
SERIAL_CREATE_TOKENS = 2000


def _opt_int(v: Any) -> int | None:
    """None stays None — see the unknown-cost rung in _batch_retry_verdict."""
    return None if v is None else int(v)


def _batch_retry_verdict(
    *,
    resolved: int,
    declared: int,
    missing: int,
    tokens: int | None,
    attempt: int,
    truncated: bool,
    salvaged: bool,
) -> tuple[bool, str]:
    """Which rung of the fallback ladder this batch attempt lands on.

    Returns ``(should_retry, why)``. The reason is returned either way and
    goes into the observations, the log line and the manifest, so a trace
    can always say WHICH rung fired and why — the old code logged only
    that serial had been chosen, never that it was the last resort.
    """
    coverage = (resolved / declared) if declared else 0.0
    pct = f"{resolved}/{declared} ({coverage:.0%})"

    if coverage >= BATCH_COVERAGE_FLOOR:
        return False, f"batch is substantially complete at {pct} — keeping it"
    # A ceiling-bound generation will hit the same ceiling on a resample;
    # only a model that stopped SHORT of its budget is worth re-rolling.
    if truncated:
        return (
            False,
            f"{pct} but the generation was TRUNCATED — a resample truncates too",
        )
    # A degenerate abort already consumed its second chance on the salvage
    # path; re-rolling a model mid-orbit throws good tokens after bad.
    if salvaged:
        return False, f"{pct} from a salvaged abort — already the recovery rung"
    if attempt >= BATCH_MAX_ATTEMPTS:
        return False, f"{pct} after {attempt} attempts — retries exhausted"

    # UNKNOWN COST IS NOT CHEAP. `tokens is None` means the step never
    # received a measurement — the context key was not declared, not
    # published, or the caller is a harness that does not model it. A
    # missing measurement must not read as a free attempt, or the budget
    # test degrades into "always retry" exactly where it stops protecting
    # anything. A measured 0 (an empty generation) is a real, and very
    # cheap, number and does license a resample.
    if tokens is None:
        return (
            False,
            f"{pct} but the attempt's token cost is unknown — not resampling blind",
        )

    serial_cost = missing * SERIAL_CREATE_TOKENS
    if tokens > serial_cost:
        return False, (
            f"{pct} but the attempt cost {tokens} tokens against ~{serial_cost} "
            f"for serial ({missing} files) — a resample is the more expensive path"
        )
    return True, (
        f"{pct} at only {tokens} tokens, under the ~{serial_cost} a serial "
        f"fallback for {missing} files would cost — resampling the batch "
        f"(attempt {attempt + 1} of {BATCH_MAX_ATTEMPTS})"
    )


def _normalize_path(path: str) -> str:
    return path.strip().lstrip("./").strip()


def _declared_files(mission: Any) -> list[str]:
    from agent.actions.mission_actions import _get_sweep_files

    arch = getattr(mission, "architecture", None)
    if not arch:
        return []
    return _get_sweep_files(arch)


def _declared_data_paths(mission: Any) -> list[str]:
    """The architecture's declared data-file paths (data_shapes[].file)."""
    arch = getattr(mission, "architecture", None) if mission else None
    shapes = getattr(arch, "data_shapes", None) if arch else None
    out: list[str] = []
    for ds in shapes or []:
        get = ds.get if isinstance(ds, dict) else (lambda k: getattr(ds, k, ""))
        file = str(get("file") or "").strip().lstrip("./")
        if file:
            out.append(file)
    return out


# Directory names code plausibly invents for data files when the design
# declared none (the seam that broke three 2026-07 structural runs: code
# addressed `data/rooms.yaml` / joined `/ "data"` while the design declared
# top-level paths). Deliberately small — the check must be loud and right.
_INVENTED_DATA_DIRS = {"data", "assets", "resources", "config", "configs", "world"}


def _data_boundary_violations(code_text: str, declared: list[str]) -> list[str]:
    """Deterministic data-locus check: does this code address the declared
    data files at their declared paths?

    Two violation classes (see dev/serving_perf_reference.md §6 history):
      (a) prefixed reference — a string literal whose basename matches a
          declared data file but whose path differs ("data/rooms.yaml" when
          the design declares "rooms.yaml");
      (b) invented data dir — a directory-ish literal (_INVENTED_DATA_DIRS)
          used as a path-join operand (``base / "data"``,
          ``os.path.join(x, "data")``, ``Path("data")``) when no declared
          data path carries that directory.

    Returns human-actionable violation strings (empty when clean). A file
    that does not parse returns [] — the syntax gate owns that failure.
    """
    declared_norm = [d.strip().lstrip("./") for d in declared if d and d.strip()]
    if not declared_norm or not code_text:
        return []
    try:
        tree = stdlib_ast.parse(code_text)
    except SyntaxError:
        return []

    by_base: dict[str, set[str]] = {}
    for d in declared_norm:
        by_base.setdefault(d.rsplit("/", 1)[-1], set()).add(d)
    declared_dirs = {
        seg for d in declared_norm if "/" in d for seg in d.split("/")[:-1]
    }
    declared_list = ", ".join(sorted(declared_norm))

    violations: list[str] = []
    seen: set[str] = set()

    def _flag(msg: str) -> None:
        if msg not in seen:
            seen.add(msg)
            violations.append(msg)

    def _dir_literal(node: Any) -> str | None:
        if isinstance(node, stdlib_ast.Constant) and isinstance(node.value, str):
            v = node.value.strip().strip("/")
            if v.lower() in _INVENTED_DATA_DIRS and v not in declared_dirs:
                return v
        return None

    for node in stdlib_ast.walk(tree):
        # (a) any string literal referencing a declared basename off-path
        if isinstance(node, stdlib_ast.Constant) and isinstance(node.value, str):
            # Docstring-sized prose is scanned separately (prose helper);
            # here only path-sized literals count.
            if "\n" in node.value or len(node.value) > 200:
                continue
            ref = node.value.strip().lstrip("./")
            base = ref.rsplit("/", 1)[-1]
            if "/" in ref and base in by_base and ref not in by_base[base]:
                _flag(
                    f"references data file as '{node.value}' but the design "
                    f"declares it at '{sorted(by_base[base])[0]}' — open it by "
                    "EXACTLY the declared path"
                )
        # (b) invented directory as a join operand
        if isinstance(node, stdlib_ast.BinOp) and isinstance(node.op, stdlib_ast.Div):
            d = _dir_literal(node.right)
            if d:
                _flag(
                    f"builds data paths under an undeclared '{d}/' directory "
                    f"(path join); the design declares data files at: "
                    f"{declared_list} — address those exact paths"
                )
        if isinstance(node, stdlib_ast.Call):
            fn = node.func
            fn_name = (
                fn.attr
                if isinstance(fn, stdlib_ast.Attribute)
                else (fn.id if isinstance(fn, stdlib_ast.Name) else "")
            )
            if fn_name in ("join", "joinpath", "Path", "PurePath"):
                for arg in node.args:
                    d = _dir_literal(arg)
                    if d:
                        _flag(
                            f"builds data paths under an undeclared '{d}/' "
                            f"directory ({fn_name}(...)); the design declares "
                            f"data files at: {declared_list} — address those "
                            "exact paths"
                        )
    return violations


def _data_boundary_prose_violations(text: str, declared: list[str]) -> list[str]:
    """Prose-level data-locus check for CONTRACT stubs.

    At contract time the bodies are ``...`` — the drift lives in docstring
    PROSE (the 2026-07-21 case: main.py's contract said "Load world data
    from the 'data' directory"), invisible to the AST join-scan. Two narrow
    patterns:
      (a') a path-like token whose basename matches a declared data file
           under a different prefix ("data/rooms.yaml");
      (b') an explicit "<dir> directory" phrase naming an undeclared
           directory from _INVENTED_DATA_DIRS ("the 'data' directory").
    """
    import re

    declared_norm = [d.strip().lstrip("./") for d in declared if d and d.strip()]
    if not declared_norm or not text:
        return []
    by_base: dict[str, set[str]] = {}
    for d in declared_norm:
        by_base.setdefault(d.rsplit("/", 1)[-1], set()).add(d)
    declared_dirs = {
        seg for d in declared_norm if "/" in d for seg in d.split("/")[:-1]
    }
    declared_list = ", ".join(sorted(declared_norm))
    violations: list[str] = []

    for token in re.findall(r"[\w.\-/]+", text):
        if "/" not in token:
            continue
        norm = token.lstrip("./")
        base = norm.rsplit("/", 1)[-1]
        if base in by_base and norm not in by_base[base]:
            v = (
                f"contract references data file as '{token}' but the design "
                f"declares it at '{sorted(by_base[base])[0]}' — the contract "
                "must state EXACTLY the declared path"
            )
            if v not in violations:
                violations.append(v)

    for m in re.finditer(
        r"['\"]?(\w+)['\"]?\s+(?:sub)?director(?:y|ies)", text, re.IGNORECASE
    ):
        d = m.group(1).lower()
        if d in _INVENTED_DATA_DIRS and d not in declared_dirs:
            v = (
                f"contract describes data files in a '{d}' directory the "
                f"design never declared; the design declares data files at: "
                f"{declared_list} — the contract must use those exact paths"
            )
            if v not in violations:
                violations.append(v)
    return violations


# ── transfer-shape gate ────────────────────────────────────────────────
# Producer→consumer dict-key agreement across modules. The seam class that
# decided the 2026-07-21 fair ablation: combat.CombatEngine.run() only ever
# returns {"outcome","message"} (every return site a dict literal) while
# engine.py reads result.get("monster_defeated") — so victory never
# registers. Statically checkable with high precision exactly when the
# producer's returns are ALL dict literals; everything else is skipped
# (conservative: unknown producers are never checked). Paradigm-neutral —
# the same class broke the single-author batch (Player(**dict) key drift).


def _producer_dict_keys(sources: dict[str, str]) -> dict[tuple[str, str], set[str]]:
    """Pass 1: index (module_stem, qualname) -> union of returned dict keys.

    A producer qualifies only when it has >=1 dict-literal return and EVERY
    value-carrying return is a dict literal with all-string-constant keys
    and no ** unpacking. Bare return / return None early-exits are allowed.
    Qualnames: "fn" for top-level functions, "Class.method" for methods.
    """
    index: dict[tuple[str, str], set[str]] = {}

    def _keys_of(fn: Any) -> set[str] | None:
        returns = [
            n
            for n in stdlib_ast.walk(fn)
            if isinstance(n, stdlib_ast.Return)
            and not (isinstance(n.value, stdlib_ast.Constant) and n.value.value is None)
            and n.value is not None
        ]
        dict_returns = [r for r in returns if isinstance(r.value, stdlib_ast.Dict)]
        if not dict_returns or len(dict_returns) != len(returns):
            return None
        keys: set[str] = set()
        for r in dict_returns:
            for k in r.value.keys:
                if not (
                    isinstance(k, stdlib_ast.Constant) and isinstance(k.value, str)
                ):
                    return None  # dynamic key or ** unpacking (key=None)
                keys.add(k.value)
        return keys

    for path, src in sources.items():
        stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
                keys = _keys_of(node)
                if keys is not None:
                    index[(stem, node.name)] = keys
            elif isinstance(node, stdlib_ast.ClassDef):
                for m in node.body:
                    if isinstance(
                        m, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
                    ):
                        keys = _keys_of(m)
                        if keys is not None:
                            index[(stem, f"{node.name}.{m.name}")] = keys
    return index


# ══════════════════════════════════════════════════════════════════════
# The graph/placement seam family
#
# Third of the three recorded seam families and the one no gate covered:
# call-shape is caught by the contract typecheck, value/key vocabulary by
# the round-trip check, and this — a boss wing with no inbound edge, an
# entity authored into no room — by nothing at all. It is the second most
# common DECISIVE defect in the blind-panel field.
#
# SHAPES ARE TAKEN FROM 62 REAL WORLD FILES, not from a guess. That survey
# overturned three assumptions worth stating, because each would have made
# this check wrong on the majority of artifacts:
#
#   * 55 of 62 declare NO start room. Reachability therefore falls back to
#     the first room in document order, which is what the games themselves
#     do. Requiring an explicit start would flag nearly every artifact.
#   * Placement is usually ROOM-EMBEDDED (`room.items: [id, ...]`), not a
#     `location` field on the entity — only ~24 of 62 use location/room_id.
#     Checking only for `location` would find nothing on most worlds.
#   * Rooms come list-shaped (40) AND dict-shaped (22), with edges under
#     `exits` or `connections`.
#
# It reports only what it can prove. An unrecognised shape is skipped, not
# flagged: this check drives repairs, and today a false positive already
# cost three rewrites of a correct file.
# ══════════════════════════════════════════════════════════════════════

_ROOM_KEYS = ("rooms", "locations", "areas")
_EDGE_KEYS = ("exits", "connections", "neighbors", "links")
_START_KEYS = (
    "start_room_id",
    "start_room",
    "starting_room_id",
    "starting_room",
    "player_start",
    "start",
)
_ENTITY_KEYS = ("items", "monsters", "npcs", "entities", "characters", "creatures")
_PLACE_KEYS = ("location", "room", "room_id", "place", "start_room")


def _as_id_map(coll: Any) -> dict[str, dict]:
    """Normalise a room/entity collection to {id: mapping} for both shapes."""
    out: dict[str, dict] = {}
    if isinstance(coll, dict):
        for k, v in coll.items():
            if isinstance(v, dict) and isinstance(k, str):
                out[k] = v
    elif isinstance(coll, list):
        for v in coll:
            if not isinstance(v, dict):
                continue
            ident = v.get("id") or v.get("name")
            if isinstance(ident, str):
                out[ident] = v
    return out


def _edge_targets(room: dict) -> list[str]:
    """Room ids this room leads to, across every edge shape observed."""
    targets: list[str] = []
    for key in _EDGE_KEYS:
        edges = room.get(key)
        if isinstance(edges, dict):
            for v in edges.values():
                if isinstance(v, str):
                    targets.append(v)
                elif isinstance(v, dict):
                    for k2 in ("room", "room_id", "target", "to", "destination", "id"):
                        if isinstance(v.get(k2), str):
                            targets.append(v[k2])
                            break
        elif isinstance(edges, list):
            for v in edges:
                if isinstance(v, str):
                    targets.append(v)
                elif isinstance(v, dict):
                    for k2 in ("room", "room_id", "target", "to", "destination", "id"):
                        if isinstance(v.get(k2), str):
                            targets.append(v[k2])
                            break
    return targets


def _graph_placement_violations(data_sources: dict[str, str]) -> list[str]:
    """Unreachable rooms, exits to nowhere, and entities placed in no room.

    Returns [] when the world cannot be understood — a shape this does not
    recognise is not evidence of a defect.
    """
    import json as stdlib_json

    out: list[str] = []
    for path, text in sorted(data_sources.items()):
        doc: Any = None
        try:
            if path.endswith((".yaml", ".yml")):
                import yaml as stdlib_yaml

                doc = stdlib_yaml.safe_load(text)
            elif path.endswith(".json"):
                doc = stdlib_json.loads(text)
        except Exception:  # noqa: BLE001 — a malformed file is the syntax gate's job
            continue
        if not isinstance(doc, dict):
            continue

        rooms_raw = next((doc[k] for k in _ROOM_KEYS if k in doc), None)
        rooms = _as_id_map(rooms_raw)
        if len(rooms) < 2:
            continue  # nothing to be disconnected from

        ids = set(rooms)

        # ── exits that lead nowhere ───────────────────────────────────
        dangling: list[str] = []
        adjacency: dict[str, list[str]] = {}
        for rid, room in rooms.items():
            tgts = _edge_targets(room)
            adjacency[rid] = [t for t in tgts if t in ids]
            dangling.extend(f"{rid} -> {t}" for t in tgts if t not in ids)
        if dangling:
            out.append(
                f"world graph ({path}): {len(dangling)} exit(s) lead to a room that "
                f"does not exist — {', '.join(sorted(dangling)[:6])}. Walking that "
                f"direction cannot work."
            )

        # ── rooms nothing can reach ───────────────────────────────────
        if any(adjacency.values()):
            start = ""
            for k in _START_KEYS:
                v = doc.get(k)
                if isinstance(v, str) and v in ids:
                    start = v
                    break
            if not start:
                start = next(iter(rooms))
            seen = {start}
            queue = [start]
            while queue:
                cur = queue.pop()
                for nxt in adjacency.get(cur, []):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
            unreachable = sorted(ids - seen)
            if unreachable:
                out.append(
                    f"world graph ({path}): {len(unreachable)} room(s) cannot be "
                    f"reached from '{start}' — {', '.join(unreachable[:6])}. A room "
                    f"no path leads to is content the player can never see."
                )

        # ── entities placed in no room ────────────────────────────────
        # Two placement conventions, and a world may use either. An entity
        # is placed if a room embeds its id, or if it names a real room.
        embedded: set[str] = set()
        for room in rooms.values():
            for key in _ENTITY_KEYS:
                v = room.get(key)
                if isinstance(v, list):
                    embedded.update(x for x in v if isinstance(x, str))
                elif isinstance(v, dict):
                    embedded.update(k for k in v if isinstance(k, str))

        for coll_key in _ENTITY_KEYS:
            if coll_key not in doc:
                continue
            entities = _as_id_map(doc.get(coll_key))
            if not entities:
                continue
            unplaced, misplaced = [], []
            for eid, ent in entities.items():
                where = next(
                    (ent[k] for k in _PLACE_KEYS if isinstance(ent.get(k), str)), ""
                )
                if where:
                    if where not in ids:
                        misplaced.append(f"{eid} -> '{where}'")
                elif eid not in embedded:
                    unplaced.append(eid)
            if misplaced:
                out.append(
                    f"world graph ({path}): {len(misplaced)} {coll_key} name a room "
                    f"that does not exist — {', '.join(sorted(misplaced)[:6])}."
                )
            # Only meaningful when SOME entity of this kind is placed; a
            # world that places none of them uses a convention this cannot
            # see, and silence is the honest answer.
            if unplaced and len(unplaced) < len(entities):
                out.append(
                    f"world graph ({path}): {len(unplaced)} {coll_key} are in no "
                    f"room — {', '.join(sorted(unplaced)[:6])}. Authored but "
                    f"unreachable."
                )
    return out


def _transfer_shape_violations(sources: dict[str, str]) -> dict[str, list[str]]:
    """Cross-module transfer-dict check over the whole fileset.

    Returns {consumer_path: [violation, ...]} — a violation is a .get("k")
    or ["k"] access on a variable whose value came from a cross-module
    producer that only ever returns dict literals, where k is not among the
    produced keys. Membership tests ("k" in y) are defensive and never
    flagged. Tracking is function-scope, single-assignment, invalidated on
    reassignment; aliasing and attribute stores are not followed.
    """
    producers = _producer_dict_keys(sources)
    if not producers:
        return {}
    stems = {p.rsplit("/", 1)[-1].rsplit(".", 1)[0] for p in sources}
    out: dict[str, list[str]] = {}

    def _imports(tree: Any) -> tuple[dict[str, str], dict[str, str]]:
        """name->module for `from mod import Name`; alias->module for `import mod`."""
        from_map: dict[str, str] = {}
        mod_map: dict[str, str] = {}
        for n in stdlib_ast.walk(tree):
            if isinstance(n, stdlib_ast.ImportFrom) and n.module:
                stem = n.module.split(".")[-1]
                if stem in stems:
                    for a in n.names:
                        from_map[a.asname or a.name] = stem
            elif isinstance(n, stdlib_ast.Import):
                for a in n.names:
                    stem = a.name.split(".")[-1]
                    if stem in stems:
                        mod_map[a.asname or a.name.split(".")[0]] = stem
        return from_map, mod_map

    def _scan_function(
        fn: Any,
        path: str,
        from_map: dict[str, str],
        mod_map: dict[str, str],
        violations: list[str],
    ) -> None:
        instances: dict[str, tuple[str, str]] = {}  # var -> (mod, Class)
        dicts: dict[str, tuple[str, str, set[str]]] = {}  # var -> (mod, qual, keys)

        # Track ONLY names assigned exactly once in this function: ast.walk
        # is breadth-first (not source order), so multi-assigned names could
        # otherwise be checked against stale tracking (a reassignment inside
        # a branch visits after a textually-later access). Single-assignment
        # names are order-immune; everything else is skipped (conservative).
        assign_counts: dict[str, int] = {}
        for n in stdlib_ast.walk(fn):
            if isinstance(n, stdlib_ast.Assign):
                for t in n.targets:
                    if isinstance(t, stdlib_ast.Name):
                        assign_counts[t.id] = assign_counts.get(t.id, 0) + 1
            elif isinstance(n, (stdlib_ast.AugAssign, stdlib_ast.AnnAssign)):
                if isinstance(n.target, stdlib_ast.Name):
                    assign_counts[n.target.id] = assign_counts.get(n.target.id, 0) + 1
            elif isinstance(n, stdlib_ast.For) and isinstance(
                n.target, stdlib_ast.Name
            ):
                assign_counts[n.target.id] = assign_counts.get(n.target.id, 0) + 2

        def _producer_of(call: Any) -> tuple[str, str] | None:
            """Resolve a Call node to an indexed (mod, qualname), if any."""
            f = call.func
            if isinstance(f, stdlib_ast.Name):
                # fn(...) or Class(...) via from-import
                mod = from_map.get(f.id)
                if mod and (mod, f.id) in producers:
                    return (mod, f.id)
                return None
            if isinstance(f, stdlib_ast.Attribute):
                base = f.value
                if isinstance(base, stdlib_ast.Name):
                    # mod.fn(...)
                    mod = mod_map.get(base.id)
                    if mod and (mod, f.attr) in producers:
                        return (mod, f.attr)
                    # instance.method(...)
                    inst = instances.get(base.id)
                    if inst and (inst[0], f"{inst[1]}.{f.attr}") in producers:
                        return (inst[0], f"{inst[1]}.{f.attr}")
            return None

        for node in stdlib_ast.walk(fn):
            if isinstance(node, stdlib_ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                if not isinstance(tgt, stdlib_ast.Name):
                    continue
                if assign_counts.get(tgt.id, 0) != 1:
                    continue  # multi-assigned → never tracked
                if isinstance(node.value, stdlib_ast.Call):
                    call = node.value
                    prod = _producer_of(call)
                    if prod is not None:
                        dicts[tgt.id] = (prod[0], prod[1], producers[prod])
                        continue
                    # instance construction: x = Name(...) via from-import
                    f = call.func
                    if isinstance(f, stdlib_ast.Name) and f.id in from_map:
                        instances[tgt.id] = (from_map[f.id], f.id)
                    elif (
                        isinstance(f, stdlib_ast.Attribute)
                        and isinstance(f.value, stdlib_ast.Name)
                        and f.value.id in mod_map
                    ):
                        instances[tgt.id] = (mod_map[f.value.id], f.attr)
            elif isinstance(node, stdlib_ast.Call):
                # y.get("k"[, default])
                f = node.func
                if (
                    isinstance(f, stdlib_ast.Attribute)
                    and f.attr == "get"
                    and isinstance(f.value, stdlib_ast.Name)
                    and f.value.id in dicts
                    and node.args
                    and isinstance(node.args[0], stdlib_ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    mod, qual, keys = dicts[f.value.id]
                    k = node.args[0].value
                    if k not in keys:
                        violations.append(
                            f"{path}: `{f.value.id}.get({k!r})` — "
                            f"{mod}.{qual}() only ever returns keys "
                            f"{{{', '.join(sorted(keys))}}}"
                        )
            elif isinstance(node, stdlib_ast.Subscript):
                # y["k"]
                if (
                    isinstance(node.value, stdlib_ast.Name)
                    and node.value.id in dicts
                    and isinstance(node.slice, stdlib_ast.Constant)
                    and isinstance(node.slice.value, str)
                ):
                    mod, qual, keys = dicts[node.value.id]
                    k = node.slice.value
                    if k not in keys:
                        violations.append(
                            f"{path}: `{node.value.id}[{k!r}]` — "
                            f"{mod}.{qual}() only ever returns keys "
                            f"{{{', '.join(sorted(keys))}}}"
                        )

    for path, src in sources.items():
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        from_map, mod_map = _imports(tree)
        if not from_map and not mod_map:
            continue
        violations: list[str] = []
        for node in stdlib_ast.walk(tree):
            if isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
                _scan_function(node, path, from_map, mod_map, violations)
        # dedup, preserve order
        seen: set[str] = set()
        uniq = [v for v in violations if not (v in seen or seen.add(v))]
        if uniq:
            out[path] = uniq
    return out


# ── reachability: which symbols can actually run ──────────────────────
#
# WHY (2026-07-30, from the eight seam-gate fail-open firings): five of them
# were one run re-reporting `GameEngine._handle_flee` — a real AttributeError
# sitting in `_handle_command`, a SECOND dispatcher the model wrote while
# migrating to helper style and then never wired up. `run()` still called the
# original. The seam could not execute, the fix loop spent its whole budget on
# it, and the phase exited with a WARNING claiming unresolved seams.
#
# A seam in code nothing reaches is not a defect in the artifact's behaviour.
# It is still cruft worth reporting, which is why this returns the dead
# duplicates and orphans separately rather than silently dropping them.

# Never called by name, so a name-reference test cannot see them.
_IMPLICITLY_LIVE = ("main", "setup", "teardown")


def _symbol_reachability(sources: dict[str, str]) -> dict[str, Any]:
    """Static reachability over the structural fileset.

    Returns a dict with:
      ``dead``          {qualname} — defined, never referenced anywhere else
      ``access_sites``  {attr_name: {qualname of each function accessing it}}
      ``orphans``       ["path::name"] — module-level ``def f(self, ...)``,
                        i.e. a method that fell out of its class on a splice
      ``dead_dupes``    [(dead_qualname, live_qualname)] — a dead symbol whose
                        underscore-normalized name matches a live one

    Deliberately conservative about what counts as dead: dunders, the
    ``_IMPLICITLY_LIVE`` names, anything decorated (a decorator may register
    it), and anything whose name appears in ANY string literal (getattr /
    dynamic dispatch) are all treated as live. A false "dead" here suppresses
    a real seam, so the bias is toward calling things live.
    """
    trees: dict[str, Any] = {}
    for path, text in sources.items():
        try:
            trees[path] = stdlib_ast.parse(text or "")
        except SyntaxError:
            continue

    defs: dict[str, Any] = {}  # qualname -> node
    orphans: list[str] = []
    # qualname -> the set of names referenced anywhere inside its own body
    own_refs: dict[str, set[str]] = {}
    access_sites: dict[str, set[str]] = {}
    all_refs: list[tuple[str, str]] = []  # (referenced_name, containing qualname)
    string_names: set[str] = set()

    def _fn_nodes(node: Any) -> bool:
        return isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef))

    for path, tree in trees.items():
        for s in stdlib_ast.walk(tree):
            if isinstance(s, stdlib_ast.Constant) and isinstance(s.value, str):
                string_names.add(s.value.strip())

        def _record(fn: Any, qual: str) -> None:
            defs[qual] = fn
            refs: set[str] = set()
            for n in stdlib_ast.walk(fn):
                if isinstance(n, stdlib_ast.Attribute):
                    refs.add(n.attr)
                    access_sites.setdefault(n.attr, set()).add(qual)
                elif isinstance(n, stdlib_ast.Name):
                    refs.add(n.id)
            own_refs[qual] = refs
            for r in refs:
                all_refs.append((r, qual))

        for node in tree.body:
            if _fn_nodes(node):
                _record(node, f"{path}::{node.name}")
                if any(a.arg == "self" for a in node.args.args):
                    orphans.append(f"{path}::{node.name}")
            elif isinstance(node, stdlib_ast.ClassDef):
                for item in node.body:
                    if _fn_nodes(item):
                        _record(item, f"{path}::{node.name}.{item.name}")

    def _short(qual: str) -> str:
        return qual.rsplit("::", 1)[-1].rsplit(".", 1)[-1]

    dead: set[str] = set()
    for qual, fn in defs.items():
        name = _short(qual)
        if name.startswith("__") and name.endswith("__"):
            continue
        if name in _IMPLICITLY_LIVE or name.startswith("test_"):
            continue
        if getattr(fn, "decorator_list", None):
            continue
        if name in string_names:
            continue  # getattr / dynamic dispatch — assume live
        # Referenced from anywhere that is not its own body (recursion alone
        # does not make a symbol reachable).
        if any(r == name and holder != qual for r, holder in all_refs):
            continue
        dead.add(qual)

    live_by_name: dict[str, str] = {}
    for qual in defs:
        if qual not in dead:
            live_by_name.setdefault(_short(qual).lstrip("_"), qual)
    dead_dupes = [
        (q, live_by_name[_short(q).lstrip("_")])
        for q in sorted(dead)
        if _short(q).lstrip("_") in live_by_name
    ]

    return {
        "dead": dead,
        "access_sites": access_sites,
        "orphans": sorted(orphans),
        "dead_dupes": dead_dupes,
        # Full symbol universe, so a caller can compute LIVE = defined - dead.
        # The §18 regression check needs the previous run's live set: a symbol
        # that WAS live and is now dead marks an edit that severed its last
        # caller — the arm13 failure shape (a lint fix deleted the only call
        # into the combat system and the artifact shipped unwinnable).
        "defined": set(defs),
    }


async def action_slice_batch_files(step_input: StepInput) -> StepOutput:
    """Slice a multi-file generation and write the declared files.

    Context required: inference_response, mission
    Context optional: inference_truncated
    Publishes: batch_manifest, files_changed, primary_code_file

    Manifest discipline mirrors the data-shape checker: the architecture
    is the exemplar, the generation is the data. Declared files are
    written; undeclared block paths are NOT written (logged as extra —
    an undeclared file the program needs would surface behaviorally and
    belongs in the architecture first). A block whose basename uniquely
    matches a missing declared file is accepted under the declared path
    (models occasionally prefix a spurious directory).
    """
    effects = step_input.effects
    ctx = step_input.context
    raw = ctx.get("inference_response", "") or ""
    mission = ctx.get("mission")
    truncated = bool(ctx.get("inference_truncated", False))

    declared = _declared_files(mission) if mission else []

    # DEGENERATE-ABORT SALVAGE (2026-08-02, the bartowski orbit). A server
    # degeneration abort returns NO text — but the partial generation sits
    # in the server's runaway-capture log, and the work is often complete:
    # laguna emitted all 8 declared files, properly marked, in the first
    # 42% of a 45k-token turn, then orbited on a readiness couplet; the
    # whole thing was discarded and rebuilt serially at far greater cost.
    # Fetch the capture and slice it like any other response. The tail is
    # severed by construction (the abort landed mid-loop) so the salvaged
    # text is treated as TRUNCATED — the fence-parity check drops an
    # unterminated last block, and every salvaged file still passes the
    # same write guard and per-file gates as a normal slice.
    salvaged = False
    if (
        not raw
        and declared
        and effects
        and ctx.get("inference_degenerate")
        and ctx.get("inference_request_id")
        and hasattr(effects, "fetch_runaway_capture")
    ):
        capture = await effects.fetch_runaway_capture(ctx["inference_request_id"])
        if capture and capture.get("text"):
            raw = capture["text"]
            truncated = True
            salvaged = True
            logger.warning(
                "🩹 batch salvage: recovered %d chars from the runaway capture "
                "of aborted generation %s (%s) — slicing for completed FILE "
                "blocks; %s elided bytes",
                len(raw),
                ctx["inference_request_id"],
                (capture.get("reason") or "")[:80],
                capture.get("elided_bytes", 0),
            )
    if not effects or not raw or not declared:
        # An empty response against a real blueprint is the CHEAPEST possible
        # failed attempt (zero coverage, near-zero tokens), so it is the
        # strongest resample candidate there is — route it through the same
        # ladder rather than dropping straight to serial. `not effects` and
        # `not declared` are unrecoverable here and fall through unchanged.
        if effects and declared:
            retry_empty, rung_empty = _batch_retry_verdict(
                resolved=0,
                declared=len(declared),
                missing=len(declared),
                tokens=_opt_int(ctx.get("inference_tokens_generated")),
                attempt=step_input.meta.attempt if step_input.meta else 1,
                truncated=truncated,
                salvaged=salvaged,
            )
            if retry_empty:
                logger.warning("🎲 batch resample: empty response — %s", rung_empty)
                return StepOutput(
                    result={
                        "files_written": 0,
                        "wrote_any": False,
                        "retry_batch": True,
                    },
                    observations=f"Batch attempt produced no usable text — {rung_empty}",
                    context_updates={},
                )
        return StepOutput(
            result={"files_written": 0, "wrote_any": False, "retry_batch": False},
            observations="No effects, response, or architecture manifest — nothing to write",
            context_updates={
                "batch_manifest": {
                    "written": [],
                    "missing": list(declared),
                    "extra": [],
                    "truncated": truncated,
                    "salvaged": False,
                    "deliberation_chars": 0,
                },
                "files_changed": [],
                "primary_code_file": "",
            },
        )

    declared_set = set(declared)
    blocks = parse_file_blocks(raw)

    # DELIBERATION MEASUREMENT (operator, 2026-08-03). Some models plan in
    # the open BETWEEN file blocks — the bartowski laguna capture showed 52k
    # tokens of interleaved design prose (win-path analysis, interface
    # tracing) that conditioned every later file, then vanished without a
    # log line. The prompt now expressly PERMITS this; the framework's end
    # of the bargain is to acknowledge it: count it, log it, and carry it
    # in the manifest so a trace can tell a deliberative batch from a
    # code-dense one. Text inside ANY fence is excluded (files, and the
    # 4-backtick .md fences).
    _defenced = re.sub(r"````.*?````", "", raw, flags=re.S)
    _defenced = re.sub(r"```.*?```", "", _defenced, flags=re.S)
    deliberation_chars = len(_defenced.strip())
    if deliberation_chars > 200:
        logger.info(
            "🗒 batch deliberation: %d chars outside file blocks "
            "(sanctioned; conditions later files in-context; not written)",
            deliberation_chars,
        )

    # A TRUNCATED response's last file is very likely severed mid-body, and
    # CommonMark closes an unterminated fence implicitly at end of input — so
    # markdown-it hands it back as a perfectly ordinary block and we would write
    # half a file over a good one. Drop it and let `missing` route it to the
    # serial create path, which regenerates it in full.
    #
    # Only when the fence really is unterminated: a truncation that happened to
    # land after a closing fence has a complete last file, and discarding it
    # would cost a regeneration for nothing. An odd count of fence lines means
    # the final one never closed.
    if truncated and blocks:
        fence_lines = sum(1 for ln in raw.splitlines() if ln.lstrip().startswith("```"))
        if fence_lines % 2 == 1:
            severed_path, _ = blocks[-1]
            blocks = blocks[:-1]
            logger.warning(
                "Batch slice: dropping trailing block %r — response was "
                "truncated with an unterminated fence, so its body is "
                "incomplete. It stays MISSING for the serial create path.",
                severed_path,
            )

    # RESOLUTION PASS — work out what this generation covers WITHOUT writing
    # anything. The resample rung has to be able to abandon an attempt
    # cleanly, and it can only do that if coverage is known before bytes
    # land on disk. Writing first and deleting on retry could destroy a
    # brownfield file the batch legitimately overwrote; writing first and
    # leaving them would splice two independent generations into one tree,
    # which is precisely the incoherence the batch path exists to prevent.
    resolved: list[tuple[str, str]] = []
    claimed: set[str] = set()
    extra: list[str] = []
    for path, content in blocks:
        norm = _normalize_path(path)
        target = norm if norm in declared_set else ""
        if not target:
            # Basename rescue: unique match against a still-unclaimed file.
            base = norm.rsplit("/", 1)[-1]
            candidates = [
                d for d in declared if d.rsplit("/", 1)[-1] == base and d not in claimed
            ]
            if len(candidates) == 1:
                target = candidates[0]
                logger.info(
                    "Batch slice: accepting %r under declared path %r", path, target
                )
        if not target or target in claimed:
            if norm not in declared_set:
                extra.append(norm)
            continue
        claimed.add(target)
        resolved.append((target, content))

    # The verdict is taken on what the generation COVERS, before any write.
    # A file the write guard later rejects (stub over an existing file, a
    # data file that does not parse) therefore lands in `missing` without
    # re-opening the resample question — deliberately. Re-deciding after
    # writes would mean deciding with files already on disk, which is the
    # merge this whole rung exists to avoid; guard rejections are content
    # faults and the serial repair path is the right owner for them.
    attempt = step_input.meta.attempt if step_input.meta else 1
    should_retry, rung = _batch_retry_verdict(
        resolved=len(resolved),
        declared=len(declared),
        missing=len(declared) - len(resolved),
        tokens=_opt_int(ctx.get("inference_tokens_generated")),
        attempt=attempt,
        truncated=truncated,
        salvaged=salvaged,
    )
    if should_retry:
        logger.warning("🎲 batch resample: %s", rung)
        return StepOutput(
            result={"files_written": 0, "wrote_any": False, "retry_batch": True},
            observations=f"Batch attempt discarded before writing — {rung}",
            # Deliberately publishes nothing: this attempt never existed as
            # far as the tree is concerned, and the resample's own slice
            # sets the manifest for real.
            context_updates={},
        )

    # WRITE PASS — this attempt is the one we are keeping.
    written: list[str] = []
    for target, content in resolved:
        written_ok, err = await guarded_write_file(effects, target, content)
        if written_ok:
            written.append(target)
        else:
            # Guard rejected (a stub would gut an existing file) — leave it
            # MISSING so the serial needs_create sweep regenerates it in full,
            # never a stub. No generated-file write bypasses the guard now.
            logger.warning("Batch slice: %s", err)

    # Preserve creation_order for downstream consumers.
    written = [f for f in declared if f in set(written)]
    missing = [f for f in declared if f not in set(written)]

    primary_code_file = next(
        (
            f
            for f in written
            if "." in f and not languages.is_data(f.rsplit(".", 1)[-1])
        ),
        "",
    )

    manifest = {
        "written": written,
        "missing": missing,
        "extra": extra,
        "truncated": truncated,
        "salvaged": salvaged,
        "deliberation_chars": deliberation_chars,
        # Which rung of the fallback ladder this batch landed on, and after
        # how many generations. Serial fallback is only interpretable as a
        # model signal if the record says the cheap retries were TRIED and
        # declined — without this, rung 3 and "never had a rung" look alike.
        "attempts": attempt,
        "fallback_rung": rung,
    }
    obs = (
        f"Batch slice: wrote {len(written)}/{len(declared)} declared files"
        + (f", {len(missing)} missing" if missing else "")
        + (f", {len(extra)} undeclared skipped" if extra else "")
        + (
            f", {deliberation_chars} chars of open deliberation"
            if deliberation_chars > 200
            else ""
        )
        + (
            " — SALVAGED from a degenerate-aborted generation's capture"
            if salvaged
            else (" — generation TRUNCATED" if truncated else "")
        )
    )
    logger.info(obs)
    if missing:
        # Say WHY serial was chosen, at the moment it is chosen. The whole
        # point of the ladder is that reaching serial is now informative.
        logger.warning("🪜 batch fallback rung: %s", rung)
    return StepOutput(
        result={
            "files_written": len(written),
            "wrote_any": bool(written),
            "retry_batch": False,
        },
        observations=obs + (f" — {rung}" if missing else ""),
        context_updates={
            "batch_manifest": manifest,
            "files_changed": written,
            "primary_code_file": primary_code_file,
        },
    )


async def action_run_batch_file_checks(step_input: StepInput) -> StepOutput:
    """Run per-file deterministic gates over the batch's written files.

    Context required: files_changed
    Publishes: batch_check_results, validation_results, validation_output

    Code files run the env-config tiers (syntax required, import, lint —
    same machinery as serial file_ops, grouped per extension); data
    files get the parse-validity check. The smoke-boot guard inside the
    env action is naturally inert here: environment_verified is false
    during the structural phase, exactly as in serial mode.
    """
    effects = step_input.effects
    files = step_input.context.get("files_changed") or []

    per_file: dict[str, dict[str, Any]] = {}
    flat_results: list[dict[str, Any]] = []
    output_lines: list[str] = []

    def _record(file_path: str, checks: list[dict], output: str) -> None:
        failed = [c["name"] for c in checks if not c.get("passed")]
        per_file[file_path] = {
            "passed": not any(
                not c.get("passed") and c.get("required") for c in checks
            ),
            "checks_failed": failed,
            "output": output,
        }
        flat_results.extend(checks)
        if output:
            output_lines.append(output)

    if not effects or not files:
        return StepOutput(
            result={"all_passed": True, "any_syntax_failed": False},
            observations="No files or effects — nothing to check",
            context_updates={
                "batch_check_results": {},
                "validation_results": [],
                "validation_output": "",
            },
        )

    env_config = await _load_env_config(effects)

    code_by_ext: dict[str, list[str]] = {}
    for f in files:
        ext = f.rsplit(".", 1)[-1].lower() if "." in f else ""
        if languages.is_data(ext):
            try:
                fc = await effects.read_file(f)
                content = (
                    getattr(fc, "content", "") if getattr(fc, "exists", False) else ""
                )
            except Exception as e:  # noqa: BLE001 - unreadable → treat as parse fail
                content = ""
                logger.warning("Batch check: could not read %s: %s", f, e)
            ok, detail = _parse_data_file(ext, content) if content else (True, "empty")
            check = {
                "name": f"syntax: {f}",
                "passed": ok,
                "tier": "syntax",
                "required": True,
                "stdout": "",
                "stderr": "" if ok else detail[:500],
            }
            line = f"[{'PASS' if ok else 'FAIL'}] syntax: {f}"
            if not ok:
                line += f"\n  stderr: {detail[:500]}"
            _record(f, [check], line)
        elif ext in env_config:
            code_by_ext.setdefault(ext, []).append(f)
        else:
            # Unknown extension with no env entry: nothing to run. The
            # lookup_env → set_env bootstrap in the flow handles the
            # primary language; a stray uncheckable file passes with a
            # note rather than blocking the whole batch.
            _record(
                f,
                [],
                f"[SKIP] {f} — {_UNCHECKED_NOTE}",
            )

    # Deterministic cross-file gate inputs, read ONCE for both gates:
    # data-boundary (code addresses declared data paths exactly) and
    # transfer-shape (producer→consumer dict-key agreement — the seam class
    # that decided the 2026-07-21 fair ablation). Boundary gate is inert
    # without mission/data_shapes; transfer gate is inert with <2 code files.
    declared_data = _declared_data_paths(step_input.context.get("mission"))
    code_sources: dict[str, str] = {}
    for group in code_by_ext.values():
        for f in group:
            if not f.endswith(".py"):
                continue
            try:
                fc = await effects.read_file(f)
                if getattr(fc, "exists", False):
                    code_sources[f] = getattr(fc, "content", "") or ""
            except Exception:  # noqa: BLE001 — unreadable → other gates report
                continue
    transfer_violations = (
        _transfer_shape_violations(code_sources) if len(code_sources) >= 2 else {}
    )

    for ext, group in code_by_ext.items():
        sub_input = StepInput(
            task=step_input.task,
            context={"validation_commands": env_config[ext]},
            config={},
            params={"target": group[0], "files": group},
            meta=FlowMeta(flow_name="build_structure", step_id="run_batch_checks"),
            effects=effects,
        )
        sub_out = await action_run_validation_checks_from_env(sub_input)
        results = sub_out.context_updates.get("validation_results", []) or []
        for f in group:
            mine = [c for c in results if c.get("name", "").endswith(f": {f}")]
            # Deterministic data-locus check: code must address declared
            # data files at EXACTLY their declared paths (the seam that
            # broke three 2026-07 structural runs — see
            # _data_boundary_violations).
            if declared_data and f in code_sources:
                for v in _data_boundary_violations(code_sources[f], declared_data):
                    mine.append(
                        {
                            "name": f"data_boundary: {f}",
                            "passed": False,
                            "tier": "data_boundary",
                            "required": True,
                            "stdout": "",
                            "stderr": v[:500],
                        }
                    )
            # Transfer-shape check: this file reads dict keys a cross-module
            # producer never returns (victory-never-registers class).
            for v in transfer_violations.get(f, []):
                mine.append(
                    {
                        "name": f"transfer_shape: {f}",
                        "passed": False,
                        "tier": "transfer_shape",
                        "required": True,
                        "stdout": "",
                        "stderr": v[:500],
                    }
                )
            file_output = "\n".join(
                line
                for c in mine
                for line in (
                    [f"[{'PASS' if c.get('passed') else 'FAIL'}] {c['name']}"]
                    + ([f"  stderr: {c['stderr']}"] if c.get("stderr") else [])
                )
            )
            _record(f, mine, file_output)

    all_passed = all(v["passed"] for v in per_file.values()) if per_file else True
    any_syntax_failed = any(
        c.get("tier") == "syntax" and not c.get("passed") for c in flat_results
    )
    passed_count = sum(1 for v in per_file.values() if v["passed"])
    return StepOutput(
        result={"all_passed": all_passed, "any_syntax_failed": any_syntax_failed},
        observations=(
            f"Batch checks: {passed_count}/{len(per_file)} files pass their gates"
        ),
        context_updates={
            "batch_check_results": per_file,
            "validation_results": flat_results,
            "validation_output": "\n".join(output_lines),
        },
    )


async def action_apply_batch_results(step_input: StepInput) -> StepOutput:
    """Translate batch outcomes into per-goal reports and completions.

    Context required: mission, batch_manifest, batch_check_results
    Context optional: inference_tokens_generated
    Publishes: directive_report, mission

    Each structural goal whose file was written gets a DirectiveReport
    (flow="build_structure"); goals passing structural_block_reason —
    the same gate serial auto-completion uses — complete immediately.
    Missing files get no report, leaving the sweep's needs_create path
    to build them serially. One summary NoteRecord records the batch
    economics (manifest counts, generation tokens, truncation).
    """
    from agent.actions.reporting_actions import structural_block_reason
    from agent.persistence.models import DirectiveReport, NoteRecord

    effects = step_input.effects
    ctx = step_input.context
    mission = ctx.get("mission")
    manifest = ctx.get("batch_manifest") or {}
    per_file = ctx.get("batch_check_results") or {}
    tokens = int(ctx.get("inference_tokens_generated") or 0)
    # Which batch flow is booking (contract_swarm reuses this action).
    flow_label = str(step_input.params.get("flow_label") or "build_structure")

    if not mission:
        return StepOutput(
            result={"all_passed": False, "wrote_any": False},
            observations="No mission in context — cannot apply batch results",
        )

    # Freshen from disk (lost-update guard — same doctrine as harvest):
    # a stale in-context mission would clobber state written mid-flow.
    if effects:
        try:
            fresh = await effects.load_mission()
            if fresh is not None:
                mission = fresh
        except Exception:  # noqa: BLE001 - keep context mission on read failure
            pass

    written = set(manifest.get("written") or [])
    missing = manifest.get("missing") or []
    extra = manifest.get("extra") or []
    truncated = bool(manifest.get("truncated"))

    completed = 0
    failed_files: list[str] = []
    for goal in mission.goals:
        if goal.type != "structural" or goal.status == "complete":
            continue
        file_path = next((f for f in (goal.associated_files or []) if f in written), "")
        if not file_path:
            continue
        checks = per_file.get(file_path) or {"passed": True, "checks_failed": []}
        passed = bool(checks.get("passed"))
        checks_failed = list(checks.get("checks_failed") or [])
        report = DirectiveReport(
            flow=flow_label,
            status="success" if passed else "failed",
            summary=(
                f"Created {file_path} in the batch generation; "
                + (
                    "all gates pass."
                    if passed
                    else "gate failures: " + ", ".join(checks_failed)
                )
            ),
            headline=f"Batch-created {file_path}"
            + ("" if passed else " (gate failed)"),
            files_affected=[file_path],
            checks_failed=checks_failed,
            terminal_output=_cap_diagnostic(checks.get("output") or "", 1000),
        )
        goal.reports.append(report)
        if passed and structural_block_reason(goal, checks_failed) is None:
            goal.status = "complete"
            completed += 1
        elif not passed:
            failed_files.append(file_path)

    attempts = int(manifest.get("attempts") or 1)
    summary = (
        f"Batch structural creation: {len(written)} files written, "
        f"{completed} goals completed, {len(failed_files)} failed gates"
        + (f", {len(missing)} missing (serial fallback)" if missing else "")
        + (f", {len(extra)} undeclared blocks skipped" if extra else "")
        # After a resample, "1 files written" alone would read as one bad
        # roll when it is actually the model's THIRD identical answer — the
        # difference between a fluke and a verdict about the model.
        + (f", after {attempts} batch attempts" if attempts > 1 else "")
        + (
            ", SALVAGED from aborted generation"
            if manifest.get("salvaged")
            else (", generation truncated" if truncated else "")
        )
        + (f". Generation cost: {tokens} tokens." if tokens else ".")
        # .get, not []: a manifest can arrive from a mission persisted before
        # the ladder existed, or from a caller that builds one by hand.
        + (
            f" Fallback rung: {manifest['fallback_rung']}."
            if missing and manifest.get("fallback_rung")
            else ""
        )
    )
    # LOG IT, not just note it. This summary is the only place that says whether
    # a batch DELIVERED, and until 2026-07-29 it existed solely in mission.json
    # notes — so a run being watched live was indistinguishable from a serial
    # one. Two check-ins on the glm-4.7-flash arm could not tell which path it
    # had taken; the answer ("1 files written ... 5 missing (serial fallback)")
    # was sitting in a note the whole time. A partial batch is the single most
    # important thing to know about a structural phase, because the fallback is
    # far more expensive per file.
    if missing or failed_files:
        logger.warning("🧱 %s", summary)
        if missing:
            logger.warning("🧱 serial fallback will build: %s", ", ".join(missing))
        if failed_files:
            logger.warning("🧱 failed gates: %s", ", ".join(failed_files))
    else:
        logger.info("🧱 %s", summary)

    mission.notes.append(
        NoteRecord(
            content=summary
            + (f" Failed: {', '.join(failed_files)}." if failed_files else "")
            + (f" Missing: {', '.join(missing)}." if missing else ""),
            category="codebase_observation",
            tags=["batch_structural"],
            source_flow=flow_label,
        )
    )
    if effects:
        await effects.save_mission(mission)

    directive_report = {
        "flow": flow_label,
        "status": "success" if written else "failed",
        "summary": summary,
        "headline": f"Batch built {len(written)} files, {completed} goals complete",
        "files_affected": sorted(written),
        "checks_failed": [
            name
            for f in failed_files
            for name in (per_file.get(f, {}).get("checks_failed") or [])
        ],
    }
    return StepOutput(
        result={
            "all_passed": not failed_files and not missing,
            "wrote_any": bool(written),
            "completed_count": completed,
            "failed_count": len(failed_files),
        },
        observations=summary,
        context_updates={"directive_report": directive_report, "mission": mission},
    )
