"""Session structural creation — one file per TURN, checked between turns.

WHY THIS MODE EXISTS. Seam bugs are the field's most prevalent decisive defect,
and their origin is batch time. On the gpt-oss-medium artifact the save/load
seam a blind judge called decisive had ZERO repair records — never diagnosed,
never targeted, written by the batch generation and never touched again. The
corpus says it out loud: "nine files written TOGETHER in one shared-context
generation disagree in FIVE places. Shared context is necessary for cross-file
coherence, not sufficient."

The swarm has an entity-id registry that would help, and it was never ported
here. Not because it is infeasible — it is one turn producing a pruned
defines/references map (``build_contracts.cue:78`` -> ``store_data_registry``),
and it could be pasted into a batch prompt tomorrow. It would not WORK there:
batch is one completion, so the contract is read at token 0 and nothing
re-asserts it at file five. A CONTRACT WITH NO CHECKPOINT IS A SUGGESTION. In
the swarm it binds because each isolated worker has nothing else.

This mode supplies the missing checkpoint. Files are generated one per turn
inside ONE inference session, so each file is written with its real siblings in
context (not a prediction of them), and between every pair of turns a
deterministic check runs against BOTH the architecture's contracts and the
vocabulary the earlier files actually declared.

Two checks here are new because two blind spots are proven:

  * ``_serialized_roundtrip_violations`` — ``_transfer_shape_violations``
    (batch_structural_actions.py) only indexes producers that return dict
    LITERALS, so a contract mediated by a FILE (``save_state`` dumps ->
    ``load_state`` returns ``json.load(f)``) has nothing to bind. That is
    exactly the seam that shipped.
  * ``_data_registry_violations`` — nothing anywhere re-reads a written data
    file to confirm its ids. The registry is enforced by prompt injection only.

Everything else is reuse: the blueprint renderer, the slice/write primitives,
the per-file and cross-module gates, and the goal bookkeeping are all the
batch path's, so the A/B measures the generation strategy and not a second
difference.
"""

from __future__ import annotations

import logging
from typing import Any

from agent import languages
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Repairs per file before the walk moves on. Bounded twice — here and as a
# `meta.attempt` stop in the CUE resolver — because this is the FIRST per-item
# retry in any list walk in the codebase (load_next_file, rewrite_symbol_turn
# and the quality-gate probe walk all record-and-skip), and an unbounded
# repair loop is the shape that produced a 12-round live-lock on 2026-08-10.
_SESSION_REPAIR_ATTEMPTS = 2

# How much observed vocabulary to carry into a turn. The session already holds
# every earlier file verbatim; this block is the DETERMINISTIC index of it, not
# a replacement for it, so it is a digest rather than a dump.
_VOCAB_CHAR_CAP = 4000


# ══════════════════════════════════════════════════════════════════════
# The two checks the batch path structurally cannot make
# ══════════════════════════════════════════════════════════════════════


def _dict_keys(node: Any) -> set[str] | None:
    """String keys of a dict literal, or None if it is not one / is dynamic."""
    import ast as stdlib_ast

    if not isinstance(node, stdlib_ast.Dict):
        return None
    keys: set[str] = set()
    for k in node.keys:
        if k is None:  # ** unpacking — the key set is not knowable
            return None
        if not (isinstance(k, stdlib_ast.Constant) and isinstance(k.value, str)):
            return None
        keys.add(k.value)
    return keys


def _payload_methods(
    sources: dict[str, str],
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """(producer_keys, consumer_keys) keyed by METHOD NAME — the third hop.

    THE SHAPE THE FIRST TWO HOPS COULD NOT SEE, and the one this model
    actually writes. `_roundtrip_keys` follows a dict literal to a writer and
    a reader's return into a subscript, but the idiomatic Python round trip
    puts the keys in neither place:

        json.dump(state.to_dict(), f)      # producer is a METHOD
        return GameState.from_dict(data)   # consumer is a METHOD

    Measured against a real artifact both sides came back EMPTY, so the check
    reported "0 violations" over a save/load pair it had not read a single key
    of. A zero from zero checkable items is the vacuous pass, not a clean bill.

    A producer is a method returning a dict literal, or returning
    `asdict(self)` / `dataclasses.asdict(self)` — in which case the keys are
    the enclosing class's annotated fields. That case is worth its own hop:
    `asdict` picks up a newly added field automatically and a hand-written
    `from_dict` does not, which is precisely the value/key vocabulary seam.

    A consumer is a method that reads string keys off its payload parameter —
    the first parameter that is not self/cls.
    """
    import ast as stdlib_ast

    producers: dict[str, set[str]] = {}
    consumers: dict[str, set[str]] = {}
    _prod_sets: dict[str, list[set[str]]] = {}
    _cons_sets: dict[str, list[set[str]]] = {}

    for src in sources.values():
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        for cls in stdlib_ast.walk(tree):
            if not isinstance(cls, stdlib_ast.ClassDef):
                continue
            fields = {
                n.target.id
                for n in cls.body
                if isinstance(n, stdlib_ast.AnnAssign)
                and isinstance(n.target, stdlib_ast.Name)
            }
            for fn in cls.body:
                if not isinstance(
                    fn, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
                ):
                    continue

                # ── producer ──────────────────────────────────────────
                for n in stdlib_ast.walk(fn):
                    if not isinstance(n, stdlib_ast.Return) or n.value is None:
                        continue
                    lit = _dict_keys(n.value)
                    if lit:
                        producers.setdefault(fn.name, set()).update(lit)
                        _prod_sets.setdefault(fn.name, []).append(set(lit))
                        continue
                    # asdict(self) / dataclasses.asdict(self)
                    v = n.value
                    if isinstance(v, stdlib_ast.Call):
                        f = v.func
                        name = (
                            f.id
                            if isinstance(f, stdlib_ast.Name)
                            else (f.attr if isinstance(f, stdlib_ast.Attribute) else "")
                        )
                        if name == "asdict" and fields:
                            producers.setdefault(fn.name, set()).update(fields)
                            _prod_sets.setdefault(fn.name, []).append(set(fields))

                # ── consumer ──────────────────────────────────────────
                args = [a.arg for a in fn.args.args if a.arg not in ("self", "cls")]
                if not args:
                    continue
                payload = args[0]
                keys: set[str] = set()
                for n in stdlib_ast.walk(fn):
                    if (
                        isinstance(n, stdlib_ast.Subscript)
                        and isinstance(n.value, stdlib_ast.Name)
                        and n.value.id == payload
                        and isinstance(n.slice, stdlib_ast.Constant)
                        and isinstance(n.slice.value, str)
                    ):
                        keys.add(n.slice.value)
                    if (
                        isinstance(n, stdlib_ast.Call)
                        and isinstance(n.func, stdlib_ast.Attribute)
                        and n.func.attr == "get"
                        and isinstance(n.func.value, stdlib_ast.Name)
                        and n.func.value.id == payload
                        and n.args
                        and isinstance(n.args[0], stdlib_ast.Constant)
                        and isinstance(n.args[0].value, str)
                    ):
                        keys.add(n.args[0].value)
                if keys:
                    consumers.setdefault(fn.name, set()).update(keys)
                    _cons_sets.setdefault(fn.name, []).append(set(keys))

    # AMBIGUOUS NAMES ARE DROPPED, NOT UNIONED. Both maps are keyed by bare
    # method name because a call site like `self.to_dict()` names no class.
    # When two classes each define one — Game.to_dict returning
    # {"player", "rooms"} and Player.to_dict returning the player's own
    # fields — unioning them makes the INNER keys look written at the TOP
    # level, where nothing reads them. Measured on a frontier artifact that
    # does exactly this: 12 keys reported written-and-never-read against a
    # loader that reads every one of them through Player.from_dict.
    # A name that means two things cannot be resolved from the call site, so
    # the honest answer is silence.
    for table, seen in ((producers, _prod_sets), (consumers, _cons_sets)):
        for name, sets in seen.items():
            if len({frozenset(s) for s in sets}) > 1:
                table.pop(name, None)

    return producers, consumers


def _ambiguous_payload_names(sources: dict[str, str]) -> set[str]:
    """Method names `_payload_methods` had to drop as ambiguous.

    ASYMMETRIC DROPS PRODUCE FALSE SPECIFIC CLAIMS. When `to_dict` is defined
    once but `from_dict` is defined by two classes, only the consumer side is
    dropped: `written` stays populated, `read` goes empty, and the check
    reports named keys as "written and never read back" about a loader that
    calls the very consumer it discarded. Measured live on a finished
    artifact whose loader is `GameState.from_dict(json.load(f))`, where
    `Item.from_dict` also exists.

    A dropped name means the round trip is UNVERIFIABLE, not broken.
    """
    import ast as stdlib_ast

    per_name: dict[str, list[frozenset[str]]] = {}
    for src in sources.values():
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        for cls in stdlib_ast.walk(tree):
            if not isinstance(cls, stdlib_ast.ClassDef):
                continue
            for fn in cls.body:
                if isinstance(
                    fn, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
                ):
                    per_name.setdefault(fn.name, []).append(frozenset())
    return {n for n, seen in per_name.items() if len(seen) > 1}


def _serializer_functions(sources: dict[str, str]) -> tuple[set[str], set[str]]:
    """(writer_names, reader_names) — functions that json.dump / json.load.

    Bare names, because that is how they are imported and called
    (`from save_load import save_state, load_state`). A function is a writer
    if its body reaches `json.dump(s)`, a reader if it reaches `json.load(s)`.
    """
    import ast as stdlib_ast

    writers: set[str] = set()
    readers: set[str] = set()
    for src in sources.values():
        try:
            tree = stdlib_ast.parse(src)
        except SyntaxError:
            continue
        for fn in stdlib_ast.walk(tree):
            if not isinstance(
                fn, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)
            ):
                continue
            for n in stdlib_ast.walk(fn):
                f = getattr(n, "func", None)
                if (
                    isinstance(n, stdlib_ast.Call)
                    and isinstance(f, stdlib_ast.Attribute)
                    and isinstance(f.value, stdlib_ast.Name)
                    and f.value.id == "json"
                ):
                    if f.attr in ("dump", "dumps"):
                        writers.add(fn.name)
                    elif f.attr in ("load", "loads"):
                        readers.add(fn.name)
    return writers, readers


def _roundtrip_keys(
    src: str,
    writers: set[str],
    readers: set[str],
    producers: dict[str, set[str]] | None = None,
    consumers: dict[str, set[str]] | None = None,
) -> tuple[set[str], set[str]]:
    """(written, read) payload keys in ONE module, following both hops.

    Hop one: `state = {...}` then `save_state(state)` — the payload is built
    here and serialized elsewhere. Hop two: `state = load_state()` then
    `state["k"]` — the payload is deserialized elsewhere and consumed here.
    Also covers the direct shape, where the same function both builds the dict
    and calls json.dump on it.
    """
    import ast as stdlib_ast

    producers = producers or {}
    consumers = consumers or {}

    def _produced(node: Any) -> set[str]:
        """Keys of `x.to_dict()` / `to_dict()` used as a payload argument."""
        if not isinstance(node, stdlib_ast.Call):
            return set()
        f = node.func
        name = (
            f.attr
            if isinstance(f, stdlib_ast.Attribute)
            else (f.id if isinstance(f, stdlib_ast.Name) else "")
        )
        return set(producers.get(name) or ())

    try:
        tree = stdlib_ast.parse(src)
    except SyntaxError:
        return set(), set()

    written: set[str] = set()
    read: set[str] = set()

    for fn in stdlib_ast.walk(tree):
        if not isinstance(fn, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            continue
        dict_vars: dict[str, set[str]] = {}
        payload_vars: set[str] = set()
        # A function whose own body calls json.load: the payload it hands to a
        # consumer method never passes through a reader-function variable, so
        # the payload_vars path below cannot see it. load_game is exactly this.
        direct_read = fn.name in readers
        direct_write = any(
            isinstance(n, stdlib_ast.Call)
            and isinstance(n.func, stdlib_ast.Attribute)
            and isinstance(n.func.value, stdlib_ast.Name)
            and n.func.value.id == "json"
            and n.func.attr in ("dump", "dumps")
            for n in stdlib_ast.walk(fn)
        )

        # TUPLE UNPACKING. `player, save_data = load_save(path, world)` is
        # the shape a loader that returns BOTH a reconstructed object and the
        # raw payload takes, and it is common precisely because the caller
        # needs the top-level keys the object does not carry. A Name-target
        # walk never registers save_data, so every `save_data.get("k")` in
        # the caller went uncounted and the keys read as never-read.
        for n in stdlib_ast.walk(fn):
            if not (isinstance(n, stdlib_ast.Assign) and len(n.targets) == 1):
                continue
            tgt = n.targets[0]
            if not isinstance(tgt, (stdlib_ast.Tuple, stdlib_ast.List)):
                continue
            v = n.value
            is_payload_src = isinstance(v, stdlib_ast.Call) and (
                (isinstance(v.func, stdlib_ast.Name) and v.func.id in readers)
                or (isinstance(v.func, stdlib_ast.Attribute) and v.func.attr in readers)
            )
            if is_payload_src:
                for el in tgt.elts:
                    if isinstance(el, stdlib_ast.Name):
                        payload_vars.add(el.id)

        for n in stdlib_ast.walk(fn):
            # AnnAssign as well as Assign. `save_data: dict = json.load(f)` is
            # an AnnAssign, so an Assign-only walk cannot see the payload at
            # all — and annotating it is the IDIOMATIC style, which made this
            # blind spot fire on typed loaders specifically. Measured against
            # 64 finished artifacts it produced a confident "player is written
            # and never read back" about a loader whose next line was
            # `player_dict = save_data["player"]`.
            if isinstance(n, (stdlib_ast.Assign, stdlib_ast.AnnAssign)):
                if isinstance(n, stdlib_ast.AnnAssign):
                    tgt = n.target
                    if n.value is None:
                        continue
                    n = stdlib_ast.Assign(targets=[tgt], value=n.value)
                elif len(n.targets) == 1:
                    tgt = n.targets[0]
                else:
                    continue
                if not isinstance(tgt, stdlib_ast.Name):
                    continue
                keys = _dict_keys(n.value)
                if keys is not None:
                    dict_vars[tgt.id] = keys
                elif (
                    isinstance(n.value, stdlib_ast.Call)
                    and isinstance(n.value.func, stdlib_ast.Name)
                    and n.value.func.id in readers
                ):
                    payload_vars.add(tgt.id)
                # THE MIRROR OF direct_write, and the shape that produced a
                # LIVE FALSE POSITIVE: `data = json.load(f)` in the same
                # method that then subscripts it. The assignment is an
                # Attribute call, not a bare reader Name, so the branch above
                # never fired and every `data["k"]` went uncounted — the
                # commonest loader in Python read as "no reader consumes the
                # loaded payload at all". It cost three repair turns against
                # a save/load pair that was already symmetric.
                elif (
                    isinstance(n.value, stdlib_ast.Call)
                    and isinstance(n.value.func, stdlib_ast.Attribute)
                    and isinstance(n.value.func.value, stdlib_ast.Name)
                    and n.value.func.value.id == "json"
                    and n.value.func.attr in ("load", "loads")
                ):
                    payload_vars.add(tgt.id)

        # Two passes to a fixed point. `p = data["player"]` makes p a payload
        # too — the writer unions every dict it built (direct_write), so the
        # INNER keys of a nested payload are all in `written`, and the reader
        # reaches them one subscript deeper. Without this the commonest
        # grouped-save shape reports its whole player block unread. Source
        # order is not walk order, so `data` may be registered after `p` is
        # examined; iterate until stable rather than assuming.
        for _pass in range(3):
            before = len(payload_vars)
            for n in stdlib_ast.walk(fn):
                tgt_v = None
                if isinstance(n, stdlib_ast.Assign) and len(n.targets) == 1:
                    tgt_v, val = n.targets[0], n.value
                elif isinstance(n, stdlib_ast.AnnAssign) and n.value is not None:
                    tgt_v, val = n.target, n.value
                if (
                    isinstance(tgt_v, stdlib_ast.Name)
                    and isinstance(val, stdlib_ast.Subscript)
                    and isinstance(val.value, stdlib_ast.Name)
                    and val.value.id in payload_vars
                ):
                    payload_vars.add(tgt_v.id)
            if len(payload_vars) == before:
                break

        for n in stdlib_ast.walk(fn):
            # writer_fn(payload) — the cross-module hop
            if (
                isinstance(n, stdlib_ast.Call)
                and isinstance(n.func, stdlib_ast.Name)
                and n.func.id in writers
                and n.args
            ):
                a = n.args[0]
                if isinstance(a, stdlib_ast.Name) and a.id in dict_vars:
                    written |= dict_vars[a.id]
                else:
                    lit = _dict_keys(a)
                    if lit:
                        written |= lit
                    else:
                        written |= _produced(a)
            # json.dump(state.to_dict(), f) — producer straight into the dump
            if (
                isinstance(n, stdlib_ast.Call)
                and isinstance(n.func, stdlib_ast.Attribute)
                and isinstance(n.func.value, stdlib_ast.Name)
                and n.func.value.id == "json"
                and n.func.attr in ("dump", "dumps")
                and n.args
            ):
                written |= _produced(n.args[0])
            # GameState.from_dict(data) — consumer method inside the reader
            if isinstance(n, stdlib_ast.Call) and n.args:
                f = n.func
                cname = (
                    f.attr
                    if isinstance(f, stdlib_ast.Attribute)
                    else (f.id if isinstance(f, stdlib_ast.Name) else "")
                )
                if cname in consumers:
                    arg0 = n.args[0]
                    if direct_read or (
                        isinstance(arg0, stdlib_ast.Name) and arg0.id in payload_vars
                    ):
                        read |= consumers[cname]
            # payload["k"] / payload.get("k") — the consumer hop
            if isinstance(n, stdlib_ast.Subscript) and isinstance(
                n.value, stdlib_ast.Name
            ):
                s = n.slice
                if (
                    n.value.id in payload_vars
                    and isinstance(s, stdlib_ast.Constant)
                    and isinstance(s.value, str)
                ):
                    read.add(s.value)
            if (
                isinstance(n, stdlib_ast.Call)
                and isinstance(n.func, stdlib_ast.Attribute)
                and n.func.attr == "get"
                and isinstance(n.func.value, stdlib_ast.Name)
                and n.func.value.id in payload_vars
                and n.args
                and isinstance(n.args[0], stdlib_ast.Constant)
                and isinstance(n.args[0].value, str)
            ):
                read.add(n.args[0].value)
            # `if "player" not in save_data: raise` — a guard IS a read, and
            # it is often the only mention before the value is handed to a
            # from_dict. Missing it made a validated loader look negligent.
            if isinstance(n, stdlib_ast.Compare) and isinstance(
                n.left, stdlib_ast.Constant
            ):
                if isinstance(n.left.value, str) and any(
                    isinstance(op, (stdlib_ast.In, stdlib_ast.NotIn)) for op in n.ops
                ):
                    for comp in n.comparators:
                        if (
                            isinstance(comp, stdlib_ast.Name)
                            and comp.id in payload_vars
                        ):
                            read.add(n.left.value)

        if direct_write:
            for keys in dict_vars.values():
                written |= keys

    return written, read


def _implicated_file(message: str, written: list[str]) -> str:
    """The written file a cross-file violation actually names, if any.

    Violation text carries its own provenance — "(game.py, save_load.py)" —
    so the owner is recoverable without restructuring the checks' return
    type.

    THE READER OWNS A ROUND-TRIP DEFECT. When keys are written and never
    read, the writer is doing its job and the loader is dropping state —
    "a save whose loader ignores what the writer stored", as the message
    says. So the `never read back (...)` clause wins when present, and only
    then does the leftmost-mentioned file apply. Picking by string length
    got this right once by accident and would have got it wrong the moment
    the writer had the longer path.
    """
    import re as stdlib_re

    clause = stdlib_re.search(r"never read back \(([^)]*)\)", message)
    if clause:
        named = clause.group(1)
        for path in sorted(written, key=len, reverse=True):
            if path and path in named:
                return path

    best, best_pos = "", len(message) + 1
    for path in written:
        if not path:
            continue
        i = message.find(path)
        if i >= 0 and i < best_pos:
            best, best_pos = path, i
    return best


def _serialized_roundtrip_violations(sources: dict[str, str]) -> list[str]:
    """Keys written to a serialized payload that nothing ever reads back.

    THE CHECK THAT WOULD HAVE CAUGHT THE SHIPPED SEAM. `handle_save`
    serialized four world tables; `handle_load` read none of them. Both sides
    were valid Python, both agreed on every call shape, and `save_load.py`
    typed the payload `Dict[str, Any]` — the seam lived entirely inside the
    `Any`, so every existing gate passed it. The save FILE was correct; the
    reader ignored it.

    IT MUST FOLLOW TWO HOPS. The first version of this check looked for the
    dict literals in the same module as the `json` call and found nothing on
    the very artifact it was written for — because `game.py` builds the
    payload and `save_load.py` does the I/O. That indirection is precisely
    what defeats `_transfer_shape_violations`, so reproducing it would have
    shipped a check that passes its own motivating case.

    IT MUST FOLLOW THREE. The two-hop version then reported "0 violations" on
    a real artifact whose save/load pair it had read ZERO keys from, because
    that pair used the idiomatic `json.dump(state.to_dict(), f)` /
    `GameState.from_dict(json.load(f))` — keys in neither a literal nor a
    subscript. See `_payload_methods`. A check whose clean verdict and whose
    blind verdict are the same string is not a check, so the vacuous case is
    now reported instead of silently passing.

    Reported one-way only (written-never-read). The reverse is a legitimate
    shape for optional keys with defaults.
    """
    py = {p: s for p, s in sources.items() if p.endswith(".py")}
    if len(py) < 2:
        return []
    writers, readers = _serializer_functions(py)
    if not writers or not readers:
        return []
    producers, consumers = _payload_methods(py)

    written: set[str] = set()
    read: set[str] = set()
    writer_files: list[str] = []
    reader_files: list[str] = []
    for path, src in py.items():
        w, r = _roundtrip_keys(src, writers, readers, producers, consumers)
        if w:
            written |= w
            writer_files.append(path)
        if r:
            read |= r
            reader_files.append(path)

    # An ambiguous name dropped on EITHER side makes the comparison unsound:
    # written stays populated while read goes empty, and the check would name
    # keys as unread about a loader that calls the consumer it discarded.
    ambiguous = _ambiguous_payload_names(py)
    pair_unsound = bool(
        ambiguous & {"to_dict", "from_dict", "serialize", "deserialize"}
    )

    # THE VACUOUS PASS IS A REPORTABLE STATE. A serializer pair exists — the
    # project saves and loads — yet no key was recoverable from either side,
    # so this check has no opinion and must not be counted as a clean one.
    # Returning [] here is what let a two-hop check certify an artifact it had
    # read nothing of. Surfaced as a violation, an unparseable round trip gets
    # looked at; surfaced as [], it reads as proof.
    if not written and not read:
        return [
            "serialized round trip: this project saves and loads "
            f"({', '.join(sorted(writers)[:3])} / {', '.join(sorted(readers)[:3])}) "
            "but the payload keys could not be read from either side, so the "
            "round trip is UNVERIFIED — not confirmed symmetric. Build the "
            "payload where it is serialized, or through a to_dict/from_dict "
            "pair, so the two halves can be compared."
        ]

    # Audit fields are written for humans and future migrations, not read
    # back by the loader. Flagging them is technically true and practically
    # noise — and because this check DRIVES REPAIRS, noise costs rewrites of
    # correct code. Two of ten findings across 64 artifacts were this class.
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
    if pair_unsound and (written - read):
        return [
            "serialized round trip: UNVERIFIED — this project saves and loads, "
            f"but a payload method name ({', '.join(sorted(ambiguous & {'to_dict', 'from_dict', 'serialize', 'deserialize'}))}) "
            "is defined by more than one class, so which keys belong to which "
            "half cannot be resolved from the call sites. Not confirmed "
            "symmetric and NOT confirmed broken."
        ]

    orphaned = sorted(
        k
        for k in written - read
        if not k.startswith("_") and k.lower() not in _AUDIT_FIELDS
    )
    if not orphaned or not writer_files:
        return []
    return [
        f"serialized round trip: {', '.join(orphaned[:12])} "
        f"{'is' if len(orphaned) == 1 else 'are'} written into the saved "
        f"payload ({', '.join(sorted(writer_files)[:3])}) and never read back "
        f"({', '.join(sorted(reader_files)[:3]) or 'no reader consumes the '
          'loaded payload at all'}). A save whose loader ignores what the "
        f"writer stored restores an incomplete world."
    ]


def _data_registry_violations(
    data_sources: dict[str, str], registry: list[dict] | None
) -> list[str]:
    """Re-read written data files and hold them to the id registry.

    The registry is authored once, before generation, and broadcast into
    prompts. NOTHING has ever read a file back to confirm the ids landed —
    which makes it advice, not a contract. This is the verification half.
    """
    if not registry or not data_sources:
        return []
    from agent.data_ops import Document, DataOpsError, detect_fmt

    present: dict[str, set[str]] = {}
    for path, content in data_sources.items():
        try:
            doc = Document.read(content, detect_fmt(path, content))
        except DataOpsError:
            continue  # unparseable is the syntax gate's business, not ours
        ids: set[str] = set()
        for node in doc.walk():
            ptr = node.pointer
            if not ptr:
                continue
            tail = ptr.rsplit("/", 1)[-1]
            if tail and not tail.isdigit():
                ids.add(tail)
            if isinstance(node.value, dict) and isinstance(node.value.get("id"), str):
                ids.add(node.value["id"])
        present[path] = ids

    out: list[str] = []
    for entry in registry:
        path = str(entry.get("file") or "")
        if path not in present:
            continue  # not written yet — the walk has not reached it
        declared = {str(i) for i in (entry.get("defines") or [])}
        missing = sorted(declared - present[path])
        if missing:
            out.append(
                f"entity registry: {path} was to define "
                f"{', '.join(missing[:10])} — not present in the file as "
                f"written. Siblings are being told to reference these ids."
            )
        for sibling, ids in (entry.get("references") or {}).items():
            if sibling not in present:
                continue
            dangling = sorted({str(i) for i in ids} - present[sibling])
            if dangling:
                out.append(
                    f"entity registry: {path} references "
                    f"{', '.join(dangling[:10])} in {sibling}, which does not "
                    f"define them."
                )
    return out


# ══════════════════════════════════════════════════════════════════════
# The binding vocabulary — contracts AND what is actually on disk
# ══════════════════════════════════════════════════════════════════════


def _observed_symbols(sources: dict[str, str]) -> str:
    """A signature index of what the already-written files declare.

    NOT `contract_swarm._project_digest` — that renders a `.pyi` from a
    CONTRACT SET (`stub_text` per module), which is the swarm's pre-generation
    artefact and does not exist here. The whole point of this mode is that the
    vocabulary comes off disk, so it is extracted from the written bytes with
    the same tree-sitter reader the repomap uses.
    """
    if not sources:
        return ""
    from agent.repomap import extract_file_symbols

    _KINDS = {"class", "function", "method"}
    lines: list[str] = []
    for path in sorted(sources):
        try:
            defs, _refs = extract_file_symbols(path, sources[path])
        except Exception:  # noqa: BLE001 — a digest miss weakens the block, not the run
            logger.debug("session vocabulary: %s not indexable", path, exc_info=True)
            continue
        sigs: list[str] = []
        for d in defs:
            if getattr(d, "kind", "") not in _KINDS:
                continue
            sig = (getattr(d, "signature", "") or "").strip()
            parent = getattr(d, "parent", None)
            name = getattr(d, "name", "")
            label = f"{parent}.{name}" if parent else name
            sigs.append(sig if sig else label)
        if sigs:
            lines.append(f"### {path}\n" + "\n".join(f"  {s}" for s in sigs[:40]))
    return "\n".join(lines)


def _observed_ids(data_sources: dict[str, str]) -> str:
    """The literal id strings present in the data files already written.

    `extract_data_skeleton` deliberately renders ids as `<id>` placeholders,
    so it answers "what keys" and not "which ids" — and the id vocabulary is
    precisely where shadow_lord/shadow_lich lives. Document.walk is the only
    helper that surfaces the actual strings.
    """
    if not data_sources:
        return ""
    from agent.data_ops import Document, DataOpsError, detect_fmt

    lines: list[str] = []
    for path in sorted(data_sources):
        try:
            doc = Document.read(
                data_sources[path], detect_fmt(path, data_sources[path])
            )
        except DataOpsError:
            continue
        ids: list[str] = []
        for node in doc.walk():
            if isinstance(node.value, dict) and isinstance(node.value.get("id"), str):
                ids.append(node.value["id"])
            elif node.pointer.count("/") == 2:
                tail = node.pointer.rsplit("/", 1)[-1]
                if tail and not tail.isdigit():
                    ids.append(tail)
        seen: list[str] = []
        for i in ids:
            if i not in seen:
                seen.append(i)
        if seen:
            lines.append(f"{path} defines: {', '.join(seen[:40])}")
    return "\n".join(lines)


def _binding_vocabulary(
    mission: Any, written: dict[str, str], registry: list[dict] | None
) -> str:
    """Contracts + observed, as one prompt block.

    DISAGREEMENT RULE, and it is the whole point of deriving from disk: where
    the contract and the written files disagree, IDS take the written value
    (the file that exists is the fact; the contract was a prediction made
    before anything was written) and SIGNATURES take the contract (a caller
    not yet written must bind to the declared shape, not to whatever the first
    implementer happened to emit). The block states this so the model does not
    have to infer it.
    """
    from agent.actions.contract_swarm_actions import (
        _data_contracts,
        _data_digest,
        _registry_digest,
        _state_contracts,
        _state_digest,
    )

    code = {p: s for p, s in written.items() if not languages.is_data(_ext(p))}
    data = {p: s for p, s in written.items() if languages.is_data(_ext(p))}

    parts: list[str] = []

    contract_bits = [
        _data_digest(_data_contracts(mission)),
        _state_digest(_state_contracts(mission)),
        _registry_digest(registry),
    ]
    contract_text = "\n\n".join(b for b in contract_bits if b)
    if contract_text:
        parts.append(
            "## Declared contracts (authoritative for SIGNATURES)\n" + contract_text
        )

    sym = _observed_symbols(code)
    ids = _observed_ids(data)
    if sym or ids:
        observed = "\n\n".join(b for b in (sym, ids) if b)
        parts.append(
            "## Already written, and therefore BINDING (authoritative for IDS "
            "and NAMES)\n"
            "These files exist on disk now. Call them exactly as they are — do "
            "not invent a variant spelling, and do not assume a symbol you "
            "cannot see here.\n\n" + observed
        )

    if not parts:
        return ""
    block = "\n\n".join(parts)
    if len(block) > _VOCAB_CHAR_CAP:
        block = block[:_VOCAB_CHAR_CAP].rstrip() + "\n… (truncated)"
    return block


def _ext(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


# ══════════════════════════════════════════════════════════════════════
# Actions
# ══════════════════════════════════════════════════════════════════════


async def action_open_structural_session(step_input: StepInput) -> StepOutput:
    """Open the ONE session the whole file walk runs inside.

    There is no generic opener in the codebase — every family has its own that
    also builds and queues the seed (start_diagnosis_session,
    open_search_session, open_escalation_session). This follows that contract:

      * the SAME id is published under two keys. `inference_session_id` is
        ambient (`runtime._AMBIENT_CONTEXT_KEYS`) so turn steps route into the
        session without declaring it — the filter that once made diagnose
        turns silently stateless. `structural_session_id` is the flow-scoped
        alias that steps DO declare, which is what keeps the dependency
        lint-visible; ambient status deliberately does not satisfy a
        `required`.
      * the seed rides `session_injections`, consumed once before the first
        turn's retry loop rather than re-sent per turn.

    The seed is the SAME blueprint the batch prompt gets
    (`render_batch_blueprint`), so the two modes start from identical
    information and the A/B measures the walk, not the briefing.
    """
    from agent.renderers import render_batch_blueprint
    from agent.session_injections import queue as queue_injection

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot open a structural session",
        )

    arch = getattr(mission, "architecture", None)
    try:
        blueprint = render_batch_blueprint({"source": arch}, {}) or ""
    except Exception:  # noqa: BLE001 — an unrenderable blueprint is still workable
        logger.debug("session open: blueprint render failed", exc_info=True)
        blueprint = ""

    try:
        session_id = await effects.start_inference_session({"ttl_seconds": 1800})
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to open structural session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to open structural session: {e}",
        )

    seed = (
        "You are about to write a project one file at a time, in dependency "
        "order. Every file you write stays in front of you, so each later file "
        "must bind to the names the earlier ones actually declared — not to a "
        "plausible variant of them.\n\n" + blueprint
    )
    context_updates: dict[str, Any] = {
        "structural_session_id": session_id,
        "inference_session_id": session_id,
        "session_files_written": [],
        "files_changed": [],
    }
    queue_injection(context_updates, step_input.context, seed)
    logger.info(
        "structural session opened: %s (blueprint seed %d chars)",
        session_id,
        len(blueprint),
    )
    return StepOutput(
        result={"session_started": True},
        observations=f"Structural session opened: {session_id}",
        context_updates=context_updates,
    )


async def _read_written(effects, paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        try:
            fc = await effects.read_file(p)
            if getattr(fc, "exists", False):
                out[p] = getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001 — an unreadable file simply is not indexed
            continue
    return out


async def action_session_next_file(step_input: StepInput) -> StepOutput:
    """Pop the next file off the walk and assemble its binding vocabulary.

    Cursor lives in CONTEXT (`pending_files`), never a mission field and never
    an integer index — the idiom `patch.cue`'s cross-file walk uses. Seeded on
    first call from the architecture's ordered file list, so the walk follows
    `creation_order`: dependencies before dependents, which is what makes
    "bind to what is already written" mean anything.

    Publishes: pending_files, session_files_written, current_file,
    binding_vocabulary. Result: has_next.
    """
    effects = step_input.effects
    ctx = step_input.context
    mission = ctx.get("mission")

    pending = ctx.get("pending_files")
    written_paths = list(ctx.get("session_files_written") or [])

    if pending is None:
        from agent.actions.mission_actions import _get_sweep_files

        arch = getattr(mission, "architecture", None)
        pending = list(_get_sweep_files(arch)) if arch is not None else []
        logger.info("session walk: %d file(s) in creation order", len(pending))
    pending = list(pending)

    if not pending:
        # THE MANIFEST IS THE BOOKKEEPING CONTRACT, not telemetry.
        # apply_batch_results reads `batch_manifest["written"]` to decide which
        # structural goal each file belongs to; with no manifest it skips every
        # goal, returns wrote_any=False, and the flow reports FAILURE over a
        # complete artifact. That is exactly what happened on this mode's first
        # live run: nine files on disk, nine goals still incomplete, zero
        # reports booked. Reusing batch's bookkeeping means supplying the
        # contract batch supplies, not merely calling the same action.
        from agent.actions.mission_actions import _get_sweep_files

        arch = getattr(mission, "architecture", None)
        declared = list(_get_sweep_files(arch)) if arch is not None else []
        manifest = {
            "written": list(written_paths),
            "missing": [f for f in declared if f not in written_paths],
            "extra": [],
            "truncated": False,
            "salvaged": False,
            "deliberation_chars": 0,
            "attempts": 1,
            "fallback_rung": "session walk",
        }
        logger.info(
            "session walk: complete — %d/%d file(s) written%s",
            len(written_paths),
            len(declared) or len(written_paths),
            (
                f", missing {', '.join(manifest['missing'][:6])}"
                if manifest["missing"]
                else ""
            ),
        )
        return StepOutput(
            result={"has_next": False},
            observations=(
                f"session walk: complete — {len(written_paths)} file(s) written"
            ),
            context_updates={
                "pending_files": [],
                "session_files_written": written_paths,
                "batch_manifest": manifest,
            },
        )

    current = pending.pop(0)
    written = await _read_written(effects, written_paths) if effects else {}
    registry = ctx.get("data_registry") or []
    vocabulary = _binding_vocabulary(mission, written, registry)

    logger.info(
        "session walk: file %d/%d — %s (%d sibling(s) binding)",
        len(written_paths) + 1,
        len(written_paths) + 1 + len(pending),
        current,
        len(written),
    )
    return StepOutput(
        result={"has_next": True},
        observations=(
            f"session walk: generating {current} "
            f"({len(pending)} remaining, {len(written)} sibling(s) binding)"
        ),
        context_updates={
            "pending_files": pending,
            "session_files_written": written_paths,
            "current_file": current,
            "binding_vocabulary": vocabulary,
            # A fresh file starts its own repair budget.
            "session_repairs": 0,
        },
    )


async def action_write_session_file(step_input: StepInput) -> StepOutput:
    """Write the one file this turn produced.

    Same primitives as the batch path — `parse_file_blocks` then
    `guarded_write_file` (anti-gut retention floor + the scaffold parse floor)
    — so a session-written file is held to exactly the batch standard.
    Anything the turn emits under a path other than `current_file` is dropped:
    the walk owns which file is being written, not the model.

    Publishes: files_changed, session_files_written, file_written.
    Result: write_success.
    """
    from agent.actions.file_ops_actions import guarded_write_file
    from agent.markdown_fence import parse_file_blocks

    effects = step_input.effects
    ctx = step_input.context
    current = str(ctx.get("current_file") or "")
    raw = str(ctx.get("inference_response") or "")
    written_paths = list(ctx.get("session_files_written") or [])
    changed = list(ctx.get("files_changed") or [])

    if not current or not effects:
        return StepOutput(
            result={"write_success": False},
            observations="session write: no target file or effects",
        )

    blocks = parse_file_blocks(raw)
    body = ""
    extra: list[str] = []
    for path, content in blocks:
        norm = str(path or "").lstrip("./")
        if norm == current.lstrip("./") and not body:
            body = content
        else:
            extra.append(norm)
    # A single unlabelled fence is the common shape when only one file was
    # asked for — take it rather than lose the turn to a missing marker.
    if not body and len(blocks) == 1:
        body = blocks[0][1]
    if not body:
        return StepOutput(
            result={"write_success": False},
            observations=(
                f"session write: the turn produced no block for {current}"
                + (f" (saw {', '.join(extra[:4])})" if extra else "")
            ),
        )

    ok, err = await guarded_write_file(effects, current, body)
    if not ok:
        return StepOutput(
            result={"write_success": False},
            observations=f"session write: {current} refused — {err or 'guard'}",
        )
    if extra:
        logger.info(
            "session write: dropped %d off-target block(s) from the %s turn: %s",
            len(extra),
            current,
            ", ".join(extra[:4]),
        )
    if current not in written_paths:
        written_paths.append(current)
    if current not in changed:
        changed.append(current)
    return StepOutput(
        result={"write_success": True},
        observations=f"session write: {current}",
        context_updates={
            "files_changed": changed,
            "session_files_written": written_paths,
            "file_written": current,
        },
    )


async def action_check_session_file(step_input: StepInput) -> StepOutput:
    """Gate the file just written, and the fileset it now belongs to.

    THE CHECKPOINT. Reuses the batch path's own gates so the standard is
    identical — `run_batch_file_checks` (syntax/lint/env per file, plus the
    data-boundary and in-process transfer-shape checks) and
    `run_contract_typecheck` (call shape across modules) — then adds the two
    checks no existing gate can make: the file-mediated round trip and the
    id registry read back off disk.

    Publishes: batch_check_results, violations. Result: file_ok, repairs_left.
    """
    from agent.actions.batch_structural_actions import action_run_batch_file_checks
    from agent.actions.contract_swarm_actions import action_run_contract_typecheck

    effects = step_input.effects
    ctx = step_input.context
    current = str(ctx.get("current_file") or "")
    written_paths = list(ctx.get("session_files_written") or [])
    repairs = int(ctx.get("session_repairs") or 0)

    if not current or not effects:
        return StepOutput(
            result={"file_ok": True, "repairs_left": 0},
            observations="session check: nothing to check",
        )

    # Run the shared gates over EVERY file written so far, not just this one:
    # a new file is exactly what can break an earlier file's assumption, and
    # the cross-module checks are only meaningful over the whole set.
    shared = StepInput(
        context={"files_changed": written_paths, "mission": ctx.get("mission")},
        params={},
        meta=step_input.meta,
        effects=effects,
    )
    per_file_out = await action_run_batch_file_checks(shared)
    results = dict(per_file_out.context_updates.get("batch_check_results") or {})

    typed = StepInput(
        context={"files_changed": written_paths, "batch_check_results": results},
        params={},
        meta=step_input.meta,
        effects=effects,
    )
    typed_out = await action_run_contract_typecheck(typed)
    results = dict(typed_out.context_updates.get("batch_check_results") or results)

    sources = await _read_written(effects, written_paths)
    code = {p: s for p, s in sources.items() if p.endswith(".py")}
    data = {p: s for p, s in sources.items() if languages.is_data(_ext(p))}

    # ── FILESET CHECKS RUN ONCE, WHEN THE FILESET EXISTS ──────────────
    # A whole-project check applied after every file asks a question the
    # walk cannot yet answer. Save/load wiring lives in the entry point,
    # which creation_order writes LAST, so "nothing reads this payload" is
    # true of every intermediate state and means nothing until the final
    # file lands. Measured live: the round trip failed at file 5 of 7 while
    # the consumer was still unwritten, and the model burned repair turns on
    # a condition it had no way to satisfy.
    pending = list(ctx.get("pending_files") or [])
    cross: list[str] = []
    if not pending:
        cross.extend(_serialized_roundtrip_violations(code))
        cross.extend(_data_registry_violations(data, ctx.get("data_registry") or []))

    entry = results.setdefault(
        current, {"passed": True, "checks_failed": [], "output": ""}
    )
    own = list(entry.get("checks_failed") or [])
    violations = [f"{current}: {c}" for c in own] + cross

    # ── ATTRIBUTE TO THE FILE THAT OWNS THE DEFECT ────────────────────
    # Booking a cross-file violation against whatever file happened to be
    # current is how a defect in game.py's save payload became a diagnosis
    # aimed at data/world.yaml, which was then patched to satisfy it. The
    # message names its writer files; book it there.
    current_implicated = False
    for msg in cross:
        target = _implicated_file(msg, written_paths) or current
        if target == current:
            current_implicated = True
        tgt_entry = results.setdefault(
            target, {"passed": True, "checks_failed": [], "output": ""}
        )
        tgt_entry["passed"] = False
        tgt_entry["checks_failed"] = list(tgt_entry.get("checks_failed") or []) + [
            "cross_file"
        ]
        tgt_entry["output"] = (tgt_entry.get("output") or "") + "\n" + msg

    # Repair only what THIS turn can fix. A violation owned by another file
    # is recorded against that file and left to the sweep, which will
    # diagnose it with the right target instead of rewriting a bystander.
    file_ok = bool(entry.get("passed", True)) and not current_implicated
    repairs_left = max(0, _SESSION_REPAIR_ATTEMPTS - repairs)
    if not file_ok:
        logger.info(
            "session check: %s FAILED (%d violation(s), %d repair(s) left)",
            current,
            len(violations),
            repairs_left,
        )
    return StepOutput(
        result={"file_ok": file_ok, "repairs_left": repairs_left},
        observations=(
            f"session check: {current} {'ok' if file_ok else 'failed'}"
            + (f" — {len(violations)} violation(s)" if violations else "")
        ),
        context_updates={
            "batch_check_results": results,
            "violations": violations,
            "session_repairs": repairs + (0 if file_ok else 1),
        },
    )
