"""Flow context linter — static analysis of compiled CUE flow definitions.

Operates directly on flows/compiled.json and action source code.
No dependency on the old YAML loader or BlueprintIR.

Checks (new):
  1. Action contract: action code reads context/param keys not declared by step
  2. Cycle guards: step graph cycles without meta.attempt or action-level guards
  3. Publish/consume chains: required context keys with no upstream publisher
  4. Prompt/parser contracts: LLM response field names vs parsing code

Checks (ported from blueprint/lint.py):
  5. Unused optional inputs: flow accepts optional inputs no step references
  6. Prompt conventions: inference prompts missing ✅/❌ examples
  7. Resolver conventions: rule conditions using string-match anti-patterns

Checks (auditability of the flow layer — added 2026-08-05):
  8. Dead publishes: a context key published and consumed by NOTHING, once
     flow returns / prompt keys / resolver conditions / Python reads have
     been resolved away. This is the `design_gate_feedback` class, where
     the coherence gate named a defect that design_reconcile was never
     shown, so models DNF'd on what looked like their own incapacity.
  8b. context_key_python_only (INFO): the flow declares nothing about a key
     but Python reads it — it works, yet the wiring is invisible to anyone
     auditing the .cue, which is where the data contract should be legible.
  9. Prompt text in Python: prompt literals outside prompts/, which no
     reviewer can diff and no prompt/parser contract check can parse.

Usage:
    python -m agent.flow_lint [--verbose] [--compiled PATH]
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ── Result dataclass ─────────────────────────────────────────────────


@dataclass
class LintResult:
    level: str  # "ERROR" | "WARNING" | "INFO"
    flow: str
    step: str | None
    check: str
    message: str

    def __str__(self) -> str:
        loc = f"{self.flow}.{self.step}" if self.step else self.flow
        return f"  {self.level}: {loc} — {self.message}"


# ── Source introspection helpers ─────────────────────────────────────


def _find_enclosing_action(source: str, pos: int) -> str | None:
    """Find the action_* function that encloses a given source position."""
    best = None
    for fm in re.finditer(r"async def (action_\w+)", source[:pos]):
        best = fm.group(1)
    return best


def _extract_action_context_reads(action_dir: Path) -> dict[str, set[str]]:
    """Extract context keys each action function reads via step_input.context.get()."""
    reads: dict[str, set[str]] = {}
    for f in action_dir.glob("*.py"):
        if f.name in ("__init__.py", "registry.py"):
            continue
        source = f.read_text()
        for match in re.finditer(r"step_input\.context\.get\([\"'](\w+)[\"']", source):
            func = _find_enclosing_action(source, match.start())
            if func:
                reads.setdefault(func, set()).add(match.group(1))
    return reads


def _extract_action_param_reads(action_dir: Path) -> dict[str, set[str]]:
    """Extract param keys each action function reads via step_input.params.get()."""
    reads: dict[str, set[str]] = {}
    for f in action_dir.glob("*.py"):
        if f.name in ("__init__.py", "registry.py"):
            continue
        source = f.read_text()
        for match in re.finditer(r"step_input\.params\.get\([\"'](\w+)[\"']", source):
            func = _find_enclosing_action(source, match.start())
            if func:
                reads.setdefault(func, set()).add(match.group(1))
    return reads


def _extract_action_internal_guards(action_dir: Path) -> dict[str, list[str]]:
    """Extract context-based loop counters from action code.

    Recognizes patterns like:
        selection_turn = int(step_input.context.get("selection_turn", 0)) + 1
        if selection_turn > max_turns:

    Returns: func_name -> list of counter key names used as guards.
    """
    guards: dict[str, list[str]] = {}
    counter_re = re.compile(r'context\.get\(["\'](\w+)["\'].*\)\s*\+\s*1')
    for f in action_dir.glob("*.py"):
        if f.name in ("__init__.py", "registry.py"):
            continue
        source = f.read_text()
        for match in counter_re.finditer(source):
            func = _find_enclosing_action(source, match.start())
            if func:
                guards.setdefault(func, []).append(match.group(1))
    return guards


def _build_action_name_map(registry_path: Path) -> dict[str, str]:
    """Map registered action names to function names."""
    source = registry_path.read_text()
    mapping = {}
    for match in re.finditer(r'registry\.register\(["\'](\w+)["\'],\s*(\w+)', source):
        mapping[match.group(1)] = match.group(2)
    return mapping


# ── Check 1: Action contract violations ──────────────────────────────


def check_action_contracts(
    flows: dict,
    action_name_map: dict[str, str],
    context_reads: dict[str, set[str]],
    param_reads: dict[str, set[str]],
) -> list[LintResult]:
    """Flag steps where the action reads context/param keys the step doesn't declare.

    When _build_step_input filters the accumulator to declared keys only,
    undeclared reads silently get default values, causing incorrect behavior.
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            action = step_def.get("action", "")
            if action in ("inference", "flow", "noop"):
                continue

            func_name = action_name_map.get(action, "")
            if not func_name:
                continue

            # Check context reads
            declared_ctx = set(
                step_def.get("context", {}).get("required", [])
                + step_def.get("context", {}).get("optional", [])
            )
            action_ctx_reads = context_reads.get(func_name, set())
            undeclared_ctx = action_ctx_reads - declared_ctx
            if undeclared_ctx:
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=step_name,
                        check="action_reads_undeclared_context",
                        message=(
                            f"action '{action}' reads context keys "
                            f"{sorted(undeclared_ctx)} not in step's "
                            f"context declaration "
                            f"{sorted(declared_ctx) or '(empty)'}"
                        ),
                    )
                )

            # Check param reads (INFO — params usually have safe defaults)
            declared_params = set(step_def.get("params", {}).keys())
            action_param_reads = param_reads.get(func_name, set())
            undeclared_params = action_param_reads - declared_params
            if undeclared_params:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=step_name,
                        check="action_reads_undeclared_param",
                        message=(
                            f"action '{action}' reads param keys "
                            f"{sorted(undeclared_params)} not in step's "
                            f"params block "
                            f"{sorted(declared_params) or '(empty)'}"
                        ),
                    )
                )

    return results


# ── Check 2: Unguarded graph cycles ─────────────────────────────────


def check_unguarded_cycles(
    flows: dict,
    action_name_map: dict[str, str],
    internal_guards: dict[str, list[str]],
) -> list[LintResult]:
    """Detect cycles in step graphs that lack loop guards.

    A cycle is considered guarded if any of the following hold:
      - At least one step has a resolver condition referencing meta.attempt, OR
      - At least one step's action has an internal context-based counter guard
        (e.g., selection_turn in action_select_symbol_turn), OR
      - EVERY step in the cycle has at least one transition target OUTSIDE
        the cycle — meaning the cycle can always be exited from any member.
        This matches the common pattern where an LLM menu or rule-based
        resolver keeps looping until a "done"/"abandon" option is chosen.

    Unguarded cycles are WARNING; they may still be safe if bounded
    by LLM behavior, but deserve scrutiny.
    """
    results = []

    # Build set of function names that have internal guards
    guarded_funcs: set[str] = {
        func for func, counters in internal_guards.items() if counters
    }

    for flow_name, flow_def in _iter_flows(flows):
        steps = flow_def["steps"]
        adj = _build_adjacency(steps)
        cycles = _find_cycles(adj)

        for cycle in cycles:
            cycle_set = set(cycle)
            guarded = False

            for step_name in cycle:
                step_def = steps.get(step_name, {})

                # Check meta.attempt in resolver conditions
                for rule in step_def.get("resolver", {}).get("rules", []):
                    if "meta.attempt" in rule.get("condition", ""):
                        guarded = True
                        break

                # Check action-level internal guards
                if not guarded:
                    action = step_def.get("action", "")
                    func = action_name_map.get(action, "")
                    if func in guarded_funcs:
                        guarded = True

                if guarded:
                    break

            # Non-cyclic-exit check: if every cycle member can eventually
            # reach a step outside the cycle (via any path), the loop is
            # bounded — the flow can always escape. A cycle is unguarded
            # only when it's a terminal strongly-connected component with
            # no exit path at all.
            if not guarded:

                def _reaches_outside(start: str, cycle_set: set[str]) -> bool:
                    """True if any path from start leaves cycle_set."""
                    seen: set[str] = set()
                    stack = [start]
                    while stack:
                        node = stack.pop()
                        if node in seen:
                            continue
                        seen.add(node)
                        for t in adj.get(node, []):
                            if t not in cycle_set:
                                return True
                            stack.append(t)
                    return False

                if all(_reaches_outside(m, cycle_set) for m in cycle):
                    guarded = True

            if not guarded:
                cycle_str = " \u2192 ".join(cycle + [cycle[0]])
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=cycle[0],
                        check="unguarded_cycle",
                        message=(
                            f"cycle {cycle_str} has no meta.attempt or "
                            f"action-level guard \u2014 potential infinite loop"
                        ),
                    )
                )

    return results


def _build_adjacency(steps: dict) -> dict[str, list[str]]:
    """Build step adjacency list from resolver transitions."""
    adj: dict[str, list[str]] = {}
    for step_name, step_def in steps.items():
        targets: list[str] = []
        resolver = step_def.get("resolver", {})
        for rule in resolver.get("rules", []):
            t = rule.get("transition", "")
            if t:
                targets.append(t)
        dt = resolver.get("default_transition", "")
        if dt:
            targets.append(dt)
        if resolver.get("options"):
            for opt_name, opt_def in resolver["options"].items():
                if isinstance(opt_def, dict):
                    targets.append(opt_def.get("target", opt_name))
                else:
                    targets.append(opt_name)
        adj[step_name] = targets
    return adj


def _find_cycles(adj: dict[str, list[str]]) -> list[list[str]]:
    """Find all simple cycles in a directed graph. Returns deduplicated cycles."""
    cycles: list[list[str]] = []
    visited: set[str] = set()

    def dfs(node: str, path: list[str], on_stack: set[str]) -> None:
        visited.add(node)
        on_stack.add(node)
        path.append(node)

        for neighbor in adj.get(node, []):
            if neighbor not in visited:
                dfs(neighbor, path, on_stack)
            elif neighbor in on_stack:
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:])

        path.pop()
        on_stack.discard(node)

    for node in adj:
        if node not in visited:
            dfs(node, [], set())

    # Deduplicate: normalize each cycle to start with its smallest element
    seen: set[tuple[str, ...]] = set()
    unique: list[list[str]] = []
    for cycle in cycles:
        min_idx = cycle.index(min(cycle))
        normalized = tuple(cycle[min_idx:] + cycle[:min_idx])
        if normalized not in seen:
            seen.add(normalized)
            unique.append(list(normalized))

    return unique


# ── Check 3: Publish/consume chain validation ────────────────────────


# Published by runtime machinery after every inference/turn step and read
# back by that same machinery, never by a flow declaration.
_RUNTIME_KEYS = frozenset({"inference_response", "inference_error", "events"})

# `context.get("x")` / `context["x"]` anywhere under agent/. Deliberately
# broader than _extract_action_context_reads (which requires the
# `step_input.` prefix and lives per-action): for the dead-publish question
# we only need to know SOMETHING reads the key, not which action does.
_PY_CONTEXT_READ_RE = re.compile(
    r"""context\.get\(\s*["'](\w+)["']|context\[\s*["'](\w+)["']"""
)


def _python_context_reads(agent_dir: Path) -> dict[str, set[str]]:
    """key -> set of agent/** files that read it out of a context dict."""
    reads: dict[str, set[str]] = {}
    if not agent_dir.exists():
        return reads
    for f in sorted(agent_dir.rglob("*.py")):
        if "__pycache__" in str(f):
            continue
        try:
            source = f.read_text()
        except OSError:
            continue
        for m in _PY_CONTEXT_READ_RE.finditer(source):
            key = m.group(1) or m.group(2)
            reads.setdefault(key, set()).add(f.as_posix())
    return reads


def _declared_consumers(flow_def: dict) -> set[str]:
    """Every context key this flow consumes through a DECLARED path.

    Declared means auditable from the flow definition alone: step context
    blocks, $refs, prompt template keys, resolver rule conditions, and the
    flow's own `returns` (which its caller consumes). This is the set a
    reader of the .cue can see; anything outside it is invisible to the
    flow layer, which is the whole point of the check.
    """
    consumed: set[str] = set()

    for spec in (flow_def.get("returns") or {}).values():
        src = spec.get("from", "") if isinstance(spec, dict) else ""
        if src.startswith("context."):
            consumed.add(src.split(".", 1)[1])

    for step_def in (flow_def.get("steps") or {}).values():
        if not isinstance(step_def, dict):
            continue
        ctx = step_def.get("context") or {}
        consumed.update(ctx.get("required") or [])
        consumed.update(ctx.get("optional") or [])
        consumed.update(_extract_context_refs(step_def))

        tpl = step_def.get("prompt_template") or {}
        consumed.update(tpl.get("context_keys") or [])
        consumed.update(tpl.get("input_keys") or [])

        for rule in (step_def.get("resolver") or {}).get("rules") or []:
            consumed.update(
                re.findall(r"context\.(\w+)", str(rule.get("condition", "")))
            )

    return consumed


def check_publish_consume_chains(flows: dict) -> list[LintResult]:
    """Verify required context keys have an upstream publisher.

    Also flags keys published but never consumed (INFO only \u2014 sub-flow
    exports are expected to be consumed by parent flows).
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        steps = flow_def["steps"]
        flow_inputs = _flow_input_keys(flow_def)

        all_published: dict[str, list[str]] = {}
        all_consumed: set[str] = set()

        for step_name, step_def in steps.items():
            for key in step_def.get("publishes", []):
                all_published.setdefault(key, []).append(step_name)

            # Runtime machinery publishes these after EVERY inference/turn
            # step regardless of the publishes declaration (runtime.py
            # context_updates) — without modelling this, any consumer of the
            # raw turn text lints as an unpublished-key error.
            if step_def.get("action") == "inference":
                for key in ("inference_response", "inference_error"):
                    all_published.setdefault(key, []).append(step_name)

            # Turn-based menu steps use publish_selection as their
            # authoritative publisher for the chosen option key.
            turn = step_def.get("turn")
            if turn:
                sel = (turn.get("response") or {}).get("publish_selection")
                if sel:
                    all_published.setdefault(sel, []).append(step_name)

            ctx = step_def.get("context", {})
            all_consumed.update(ctx.get("required", []))
            all_consumed.update(ctx.get("optional", []))

            # $ref to context.* in input_map and pre_compute
            all_consumed.update(_extract_context_refs(step_def))

        available = set(all_published.keys()) | flow_inputs

        # Required keys with no publisher
        for step_name, step_def in steps.items():
            for key in step_def.get("context", {}).get("required", []):
                if key not in available:
                    results.append(
                        LintResult(
                            level="ERROR",
                            flow=flow_name,
                            step=step_name,
                            check="required_key_no_publisher",
                            message=(
                                f"requires context key '{key}' but no step "
                                f"or input in this flow publishes it"
                            ),
                        )
                    )

    return results


# ── Check 3c: Dead publishes, with every legitimate path resolved ────
#
# This supersedes the INFO-level half of check_publish_consume_chains,
# which hedged every finding with "may be consumed by parent flow" and so
# emitted 108 unactionable notes that the CLI dropped below its
# ERROR/WARNING threshold. The signal was real and 100% invisible: it
# included `design_gate_feedback`, where the coherence gate named a
# specific defect and design_reconcile was never shown it, so every
# reconcile attempt ran blind and models DNF'd at the design gate on what
# looked like their own incapacity (fixed 2026-08-05).
#
# The fix is to resolve the paths that made it hedge — flow `returns`,
# prompt template keys, resolver conditions, runtime machinery, and reads
# from Python — so that what remains is genuinely unconsumed and can be
# stated plainly.


def check_dead_publishes(flows: dict, agent_dir: Path) -> list[LintResult]:
    """Published context keys that nothing consumes.

    Two verdicts, because they call for different responses:

      dead_publish (WARNING)
          No consumer anywhere — not the flow layer, not Python. Either
          the key is vestigial and should go, or a consumer was meant to
          exist and does not. The second case is a silent data loss.

      context_key_python_only (INFO)
          The flow layer declares nothing, but Python reads it. It works,
          yet the wiring is invisible to anyone auditing the .cue, which
          is where the flow's data contract is supposed to be legible.
    """
    results: list[LintResult] = []
    py_reads = _python_context_reads(agent_dir)

    for flow_name, flow_def in _iter_flows(flows):
        consumed = _declared_consumers(flow_def)
        flow_inputs = _flow_input_keys(flow_def)

        published: dict[str, list[str]] = {}
        for step_name, step_def in (flow_def.get("steps") or {}).items():
            if not isinstance(step_def, dict):
                continue
            for key in step_def.get("publishes") or []:
                published.setdefault(key, []).append(step_name)
            turn = step_def.get("turn") or {}
            selection = (turn.get("response") or {}).get("publish_selection")
            if selection:
                published.setdefault(selection, []).append(step_name)

        for key, publishers in sorted(published.items()):
            if key in consumed or key in flow_inputs or key in _RUNTIME_KEYS:
                continue
            readers = py_reads.get(key)
            if readers:
                where = ", ".join(sorted(readers)[:2])
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=publishers[0],
                        check="context_key_python_only",
                        message=(
                            f"publishes '{key}'; no declaration in this flow "
                            f"consumes it, only Python does ({where}) — the "
                            f"wiring is invisible to a reader of the .cue"
                        ),
                    )
                )
            else:
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=publishers[0],
                        check="dead_publish",
                        message=(
                            f"publishes '{key}' and NOTHING consumes it — no "
                            f"step context, $ref, prompt key, resolver "
                            f"condition, flow return, or Python read. Either "
                            f"drop it or wire the consumer that was intended"
                        ),
                    )
                )

    return results


# ── Check 3a: Pre-compute context refs must be declared ──────────────


def check_precompute_context_declared(flows: dict) -> list[LintResult]:
    """Flag pre_compute $refs to context.<key> the step doesn't declare.

    _build_step_input filters the accumulator to each step's declared context
    (required + optional) before pre_compute runs. A pre_compute that references
    context.<key> without declaring it therefore sees None — the formatter
    renders empty and the step runs on missing data, silently, with no error.

    Regression guard: ops_task.run_checks referenced
    context.mission.task_definition.completion_criteria in a pre_compute but
    declared only validation_strategy, so it rendered an empty
    definition-of-done and bypassed the completion gate entirely.
    """
    ambient_keys = frozenset({"session_injections", "inference_session_id"})
    try:
        from agent.runtime import _AMBIENT_CONTEXT_KEYS as _runtime_ambient

        ambient_keys = frozenset(_runtime_ambient)
    except Exception:
        pass

    results = []
    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            ctx = step_def.get("context", {})
            declared = set(ctx.get("required", [])) | set(ctx.get("optional", []))
            declared |= ambient_keys
            # Pre-compute params only (input_map refs resolve against the
            # tail-call/sub-flow namespace, not this step's filtered context).
            for pc in step_def.get("pre_compute", []):
                for v in (pc.get("params", {}) or {}).values():
                    if not (isinstance(v, dict) and "$ref" in v):
                        continue
                    ref = v["$ref"]
                    if not (isinstance(ref, str) and ref.startswith("context.")):
                        continue
                    key = ref.split(".")[1]
                    if key not in declared:
                        results.append(
                            LintResult(
                                level="ERROR",
                                flow=flow_name,
                                step=step_name,
                                check="precompute_context_undeclared",
                                message=(
                                    f"pre_compute references 'context.{key}' but "
                                    f"the step declares only "
                                    f"{sorted(declared - ambient_keys) or '(none)'}; "
                                    f"the context filter hides it and the $ref "
                                    f"resolves to None"
                                ),
                            )
                        )
    return results


# ── Check 3b: Path-reachability of required context ──────────────────


# Short-circuit evaluation states for resolver rule conditions.
# TRUE: this rule fires on the current path — subsequent rules don't.
# FALSE: this rule can't fire — skip, continue to next rule.
# UNKNOWN: could fire or not — the rule's target is possible, and so
#   are subsequent rules (because if UNKNOWN doesn't fire at runtime,
#   the next rule gets evaluated).
_COND_TRUE = "TRUE"
_COND_FALSE = "FALSE"
_COND_UNKNOWN = "UNKNOWN"

# Condition shapes we can evaluate statically. All others are UNKNOWN.
_COND_CONTEXT_GET = re.compile(r"""^\s*context\.get\(\s*['"]([^'"]+)['"]\s*\)\s*$""")
_COND_NOT_CONTEXT_GET = re.compile(
    r"""^\s*not\s+context\.get\(\s*['"]([^'"]+)['"]\s*\)\s*$"""
)


def _evaluate_condition(cond: str, published: frozenset[str]) -> str:
    """Evaluate a resolver rule condition against the known-published set.

    Returns one of ``_COND_TRUE`` / ``_COND_FALSE`` / ``_COND_UNKNOWN``.

    Supported shapes:
      * ``true`` — always fires.
      * ``context.get('X')`` — fires iff X is in ``published``. When X
        is not published along the path being walked, the branch is
        statically unreachable and the walker can skip it.
      * ``not context.get('X')`` — inverse.

    Any other shape (conditions involving step result fields,
    comparisons, boolean composition) is reported as UNKNOWN. The
    walker treats UNKNOWN as "might fire" — the target is explored,
    and subsequent rules are also explored (since at runtime the
    condition may evaluate false and the next rule gets its chance).
    This preserves soundness: real paths are never pruned, only
    provably-unreachable ones are.
    """
    cond = (cond or "").strip()
    if cond == "true":
        return _COND_TRUE

    m = _COND_CONTEXT_GET.match(cond)
    if m:
        key = m.group(1)
        return _COND_TRUE if key in published else _COND_FALSE

    m = _COND_NOT_CONTEXT_GET.match(cond)
    if m:
        key = m.group(1)
        return _COND_FALSE if key in published else _COND_TRUE

    return _COND_UNKNOWN


def _resolver_possible_targets(resolver: dict, published: frozenset[str]) -> list[str]:
    """Return transition targets reachable from a rule resolver given
    the currently published context, honoring rule order and short-
    circuit semantics (first TRUE rule wins, subsequent rules skipped).
    """
    targets: list[str] = []
    for rule in (resolver or {}).get("rules", []) or []:
        cond = rule.get("condition", "")
        t = rule.get("transition")
        if not t:
            continue
        state = _evaluate_condition(cond, published)
        if state == _COND_TRUE:
            targets.append(t)
            break  # short-circuit: subsequent rules don't run
        elif state == _COND_FALSE:
            continue
        else:
            # UNKNOWN: might fire at runtime, or might fall through.
            # Explore this target AND keep walking the remaining rules
            # because they're both possible.
            targets.append(t)
    return targets


def check_path_reachability(flows: dict) -> list[LintResult]:
    """Verify that on *every* execution path reaching step X, all of
    X's required context keys are produced by some earlier step.

    This is stronger than check_publish_consume_chains, which only
    verifies "some step in the flow publishes this key." That's
    insufficient: the post-a85 diagnose_issue trace showed pick_action
    routing __conclude__ → end_session → compile_diagnosis, where
    compile_diagnosis needs `hypotheses`, which only force_conclude
    publishes — but force_conclude wasn't on that path. The flow
    compiled clean, per-step existence passed, but the execution
    crashed at runtime with MissingContextError.

    Algorithm: DFS from flow.entry, tracking the accumulated set of
    keys published by steps visited so far. When we arrive at a step,
    any of its required keys not in the accumulated set is a gap.
    The (step, frozen-published-set) tuple is memoized to keep the
    walk tractable on flows with cycles — once we've certified a
    state, we don't re-explore it.

    Flow inputs are seeded into the initial published set, since
    they're available throughout the flow. Ambient context keys
    (session_injections, inference_session_id) are also seeded
    because the runtime treats them as flow-wide regardless of
    whether any step publishes them.

    Rule-resolver branches are evaluated via _evaluate_condition so
    branches guarded on ``context.get('X')`` are only considered
    reachable when X has actually been published along the current
    path. This is the difference between "publisher exists somewhere
    in the flow" and "publisher has run by the time we get here."
    """
    # Keep in sync with _AMBIENT_CONTEXT_KEYS in agent/runtime.py.
    # Adding a key in one place and forgetting the other would
    # re-introduce the a85-class bug, so we pin this set here with
    # an explicit import fallback.
    ambient_keys = frozenset({"session_injections", "inference_session_id"})
    try:
        from agent.runtime import _AMBIENT_CONTEXT_KEYS as _runtime_ambient

        ambient_keys = frozenset(_runtime_ambient)
    except Exception:
        pass

    results: list[LintResult] = []

    for flow_name, flow_def in _iter_flows(flows):
        steps = flow_def["steps"]
        entry = flow_def.get("entry")
        if not entry or entry not in steps:
            continue

        seed = set(_flow_input_keys(flow_def)) | set(ambient_keys)

        def publishes_of(step_name: str) -> set[str]:
            step = steps.get(step_name, {})
            out = set(step.get("publishes", []) or [])
            # Implicit runtime publications after any inference/turn step
            # (see check_publish_consume_chains).
            if step.get("action") == "inference":
                out |= {"inference_response", "inference_error"}
            turn = step.get("turn")
            if turn:
                sel = (turn.get("response") or {}).get("publish_selection")
                if sel:
                    out.add(sel)
                    # menu_compound publishes {sel}_arg for the chosen
                    # option's argument — match runtime _extract_menu_arg.
                    if turn.get("response_shape") == "menu_compound":
                        out.add(f"{sel}_arg")
            return out

        def required_of(step_name: str) -> set[str]:
            step = steps.get(step_name, {})
            return set((step.get("context") or {}).get("required", []) or [])

        def successors_of(step_name: str, published: frozenset[str]) -> list[str]:
            step = steps.get(step_name, {})
            targets: list[str] = []
            resolver = step.get("resolver") or {}
            if resolver.get("type") == "rule" or resolver.get("rules"):
                targets.extend(_resolver_possible_targets(resolver, published))
            turn = step.get("turn") or {}
            transitions = turn.get("transitions") or {}
            if transitions.get("default"):
                targets.append(transitions["default"])
            if transitions.get("no_answer"):
                targets.append(transitions["no_answer"])
            for tgt in (transitions.get("options") or {}).values():
                if tgt:
                    targets.append(tgt)
            return targets

        # Per-flow gap set: (step, missing_keys_tuple) → sample_path.
        # Deduplicated so a cycle doesn't produce thousands of identical
        # findings.
        gaps: dict[tuple[str, tuple[str, ...]], tuple[str, ...]] = {}
        visited: set[tuple[str, frozenset]] = set()

        def walk(step_name: str, published: frozenset, path: tuple[str, ...]) -> None:
            key = (step_name, published)
            if key in visited:
                return
            visited.add(key)

            if step_name not in steps:
                # Transition target to a step that doesn't exist — a
                # separate check flags this; skip to avoid noise here.
                return

            req = required_of(step_name)
            missing = tuple(sorted(req - published))
            if missing:
                gap_key = (step_name, missing)
                if gap_key not in gaps:
                    gaps[gap_key] = path + (step_name,)

            new_published = published | publishes_of(step_name)
            for succ in successors_of(step_name, new_published):
                walk(succ, new_published, path + (step_name,))

        walk(entry, frozenset(seed), ())

        for (step_name, missing), sample_path in gaps.items():
            results.append(
                LintResult(
                    level="ERROR",
                    flow=flow_name,
                    step=step_name,
                    check="required_key_not_reachable",
                    message=(
                        f"required context {list(missing)} not published on "
                        f"every path reaching this step. Example gap path: "
                        f"{' -> '.join(sample_path)}"
                    ),
                )
            )

    return results


# ── Check 4: Prompt/parser contract mismatches ───────────────────────


def check_prompt_parser_contracts(
    prompts_dir: Path, action_dir: Path
) -> list[LintResult]:
    """Check that JSON field names in prompt examples match parsing code.

    Scans prompt templates for JSON examples (in \u2705 CORRECT blocks),
    extracts the top-level keys, then checks if the corresponding
    action's parsing code reads those keys or different ones.
    """
    results = []

    # Map: (prompt_template_path, action_source_file, parser_function)
    # Manually maintained \u2014 these are flows where the LLM returns
    # structured JSON that gets parsed by a specific action.
    prompt_action_pairs = [
        (
            "quality_gate/summarize",
            "refinement_actions.py",
            "_parse_quality_summary",
        ),
        (
            "set_env/detect_tooling",
            "pipeline_actions.py",
            "action_persist_validation_env",
        ),
    ]

    for prompt_path, action_file, parser_func in prompt_action_pairs:
        prompt_file = prompts_dir / f"{prompt_path}.yaml"
        if not prompt_file.exists():
            continue

        prompt_text = prompt_file.read_text()
        prompt_keys = _extract_json_keys_from_prompt(prompt_text)
        if not prompt_keys:
            continue

        action_source_path = action_dir / action_file
        if not action_source_path.exists():
            continue

        action_source = action_source_path.read_text()
        parser_keys = _extract_parser_reads(action_source, parser_func)
        if not parser_keys:
            continue

        prompt_only = prompt_keys - parser_keys
        parser_only = parser_keys - prompt_keys

        # Prompt-side orphans (WARNING): model generates fields nothing reads.
        # This is wasted generation — a real quality issue.
        if prompt_only:
            results.append(
                LintResult(
                    level="WARNING",
                    flow=prompt_path.split("/")[0],
                    step=None,
                    check="prompt_parser_mismatch",
                    message=(
                        f"{prompt_path}: prompt defines {sorted(prompt_only)} "
                        f"but parser ignores them"
                    ),
                )
            )
        # Parser-side orphans (INFO): parser reads keys not in the prompt
        # example. Often legitimate — normalized/synthesized keys (e.g.,
        # verdict → all_passing). Worth surfacing but not a quality bug.
        if parser_only:
            results.append(
                LintResult(
                    level="INFO",
                    flow=prompt_path.split("/")[0],
                    step=None,
                    check="prompt_parser_mismatch",
                    message=(
                        f"{prompt_path}: parser reads {sorted(parser_only)} "
                        f"but prompt doesn't define them (likely normalized)"
                    ),
                )
            )

    return results


def _extract_json_keys_from_prompt(prompt_text: str) -> set[str]:
    """Extract top-level JSON keys from \u2705 CORRECT example blocks."""
    keys: set[str] = set()
    correct_pos = prompt_text.find("CORRECT")
    if correct_pos < 0:
        return keys

    rest = prompt_text[correct_pos:]
    json_match = re.search(r"\{[\s\S]*?\}", rest)
    if json_match:
        try:
            obj = json.loads(json_match.group())
            if isinstance(obj, dict):
                keys = set(obj.keys())
        except json.JSONDecodeError:
            pass

    return keys


def _extract_parser_reads(source: str, func_name: str) -> set[str]:
    """Extract dict keys read by .get("key"), parsed["key"], or 'key' in parsed
    inside a function body. Also scans the immediate caller(s) of this function
    for reads on the returned dict — a common pattern is a parser that returns
    a normalized dict and a caller that consumes the normalized fields.

    Correctly bounds the function body by finding the next def/async def
    at the same or lesser indentation level.
    """
    keys: set[str] = set()
    func_start = source.find(f"def {func_name}")
    if func_start < 0:
        func_start = source.find(func_name)
        if func_start < 0:
            return keys

    # Determine indentation of this function
    line_start = source.rfind("\n", 0, func_start) + 1
    indent = func_start - line_start

    # Find the next def/async def at same or lesser indentation
    pattern = re.compile(r"\n( *)(?:async )?def \w+")
    func_end = len(source)
    for m in pattern.finditer(source, func_start + 10):
        if len(m.group(1)) <= indent:
            func_end = m.start()
            break

    func_body = source[func_start:func_end]

    # .get("key") / .get('key')
    for match in re.finditer(r'\.get\(["\'](\w+)["\']', func_body):
        keys.add(match.group(1))
    # parsed["key"] / parsed['key'] — any name followed by subscript access
    for match in re.finditer(r'\w+\[["\'](\w+)["\']\]', func_body):
        keys.add(match.group(1))
    # "key" in parsed / 'key' in parsed — membership tests
    for match in re.finditer(r'["\'](\w+)["\']\s+in\s+\w+', func_body):
        keys.add(match.group(1))

    # Caller chain: find functions that call this parser and bind its return
    # to a name, then scan those functions' bodies for reads of that name.
    # Pattern: "<varname> = <parser_func>(...)" — the varname holds the
    # normalized dict; any .get/subscript/in on varname counts as a read.
    call_pattern = re.compile(rf"(\w+)\s*=\s*{re.escape(func_name)}\s*\(")
    for m in call_pattern.finditer(source):
        varname = m.group(1)
        # Locate the calling function's body (same bounding logic)
        call_pos = m.start()
        # Find the enclosing function definition
        caller_start = -1
        for dm in re.finditer(r"(?:async )?def \w+", source[:call_pos]):
            caller_start = dm.start()
        if caller_start < 0:
            continue
        caller_line_start = source.rfind("\n", 0, caller_start) + 1
        caller_indent = caller_start - caller_line_start
        caller_end = len(source)
        for dm in pattern.finditer(source, caller_start + 10):
            if len(dm.group(1)) <= caller_indent:
                caller_end = dm.start()
                break
        caller_body = source[caller_start:caller_end]

        # Read patterns on the bound varname specifically.
        for match in re.finditer(
            rf'\b{re.escape(varname)}\.get\(["\'](\w+)["\']', caller_body
        ):
            keys.add(match.group(1))
        for match in re.finditer(
            rf'\b{re.escape(varname)}\[["\'](\w+)["\']\]', caller_body
        ):
            keys.add(match.group(1))
        for match in re.finditer(
            rf'["\'](\w+)["\']\s+in\s+{re.escape(varname)}\b', caller_body
        ):
            keys.add(match.group(1))

    return keys


# ── Check 5: Unused optional inputs (ported from blueprint/lint.py) ──


def check_unused_optional_inputs(flows: dict) -> list[LintResult]:
    """Warn about flow optional inputs that no step references.

    An optional input that nothing reads is dead weight in the flow
    definition and a signal that something was renamed or removed
    without updating the input declaration.
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        optional_inputs = set(flow_def.get("input", {}).get("optional", []))
        if not optional_inputs:
            continue

        referenced: set[str] = set()
        for step_name, step_def in flow_def["steps"].items():
            referenced.update(_extract_input_refs(step_def))

        unused = optional_inputs - referenced
        for inp in sorted(unused):
            results.append(
                LintResult(
                    level="INFO",
                    flow=flow_name,
                    step=None,
                    check="unused_optional_input",
                    message=(
                        f"accepts optional input '{inp}' " f"but no step references it"
                    ),
                )
            )

    return results


# ── Check 6: Prompt conventions (ported from blueprint/lint.py) ──────


def check_prompt_conventions(flows: dict, prompts_dir: Path) -> list[LintResult]:
    """Warn about inference prompts missing \u2705/\u274c output examples.

    Prompts that return structured JSON should include a \u2705 CORRECT
    example and a \u274c WRONG example to guide the LLM. This is a
    convention from PROMPTING_CONVENTIONS.md.
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            if step_def.get("action") != "inference":
                continue

            pt = step_def.get("prompt_template", {})
            template_path = pt.get("template", "")
            if not template_path:
                continue

            prompt_file = prompts_dir / f"{template_path}.yaml"
            if not prompt_file.exists():
                results.append(
                    LintResult(
                        level="WARNING",
                        flow=flow_name,
                        step=step_name,
                        check="prompt_template_missing",
                        message=(
                            f"references prompt template "
                            f"'{template_path}' but file not found"
                        ),
                    )
                )
                continue

            prompt_text = prompt_file.read_text()

            has_correct = "\u2705" in prompt_text or "CORRECT" in prompt_text
            has_wrong = "\u274c" in prompt_text or "WRONG" in prompt_text

            if not has_correct:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=step_name,
                        check="prompt_missing_correct_example",
                        message=(
                            f"prompt '{template_path}' missing "
                            f"\u2705 CORRECT output example"
                        ),
                    )
                )

            if not has_wrong:
                results.append(
                    LintResult(
                        level="INFO",
                        flow=flow_name,
                        step=step_name,
                        check="prompt_missing_wrong_example",
                        message=(
                            f"prompt '{template_path}' missing "
                            f"\u274c WRONG output example"
                        ),
                    )
                )

    return results


# ── Check 7: Resolver conventions (ported from blueprint/lint.py) ────


def check_resolver_conventions(flows: dict) -> list[LintResult]:
    """Warn about resolver anti-patterns.

    - Rule conditions doing string-match on result.text (should use llm_menu)
    - Fallthrough catch-all rule that isn't last
    - Conditions referencing result keys from a noop action
    """
    results = []

    for flow_name, flow_def in _iter_flows(flows):
        for step_name, step_def in flow_def["steps"].items():
            resolver = step_def.get("resolver", {})
            rules = resolver.get("rules", [])
            if not rules:
                continue

            action = step_def.get("action", "")

            for i, rule in enumerate(rules):
                cond = rule.get("condition", "")

                # Anti-pattern: string match on result.text
                if "result.text" in cond:
                    results.append(
                        LintResult(
                            level="WARNING",
                            flow=flow_name,
                            step=step_name,
                            check="string_match_in_result_text",
                            message=(
                                f"rule condition uses string match on "
                                f"result.text ({cond[:60]}) \u2014 "
                                f"consider llm_menu resolver instead"
                            ),
                        )
                    )

                # Anti-pattern: catch-all "true" not in last position
                if cond == "true" and i < len(rules) - 1:
                    results.append(
                        LintResult(
                            level="WARNING",
                            flow=flow_name,
                            step=step_name,
                            check="catch_all_not_last",
                            message=(
                                f"catch-all condition 'true' is rule "
                                f"{i + 1}/{len(rules)} \u2014 rules after "
                                f"it are unreachable"
                            ),
                        )
                    )

                # Anti-pattern: noop action with result.X conditions
                if action == "noop" and "result." in cond:
                    results.append(
                        LintResult(
                            level="WARNING",
                            flow=flow_name,
                            step=step_name,
                            check="noop_result_condition",
                            message=(
                                f"noop action but condition references "
                                f"result ({cond[:60]}) \u2014 noop result "
                                f"is always empty"
                            ),
                        )
                    )

    return results


# ── Shared helpers ───────────────────────────────────────────────────


def _iter_flows(flows: dict):
    """Yield (flow_name, flow_def) for valid flow definitions."""
    for flow_name, flow_def in sorted(flows.items()):
        if isinstance(flow_def, dict) and "steps" in flow_def:
            yield flow_name, flow_def


def _flow_input_keys(flow_def: dict) -> set[str]:
    """Get all input key names (required + optional) for a flow."""
    inp = flow_def.get("input", {})
    return set(inp.get("required", []) + inp.get("optional", []))


def _extract_context_refs(step_def: dict) -> set[str]:
    """Extract context.* references from input_map and pre_compute $refs."""
    refs: set[str] = set()
    for v in step_def.get("input_map", {}).values():
        if isinstance(v, dict) and "$ref" in v:
            ref = v["$ref"]
            if ref.startswith("context."):
                refs.add(ref.split(".")[1])
    for pc in step_def.get("pre_compute", []):
        for v in pc.get("params", {}).values():
            if isinstance(v, dict) and "$ref" in v:
                ref = v["$ref"]
                if ref.startswith("context."):
                    refs.add(ref.split(".")[1])
    return refs


def _extract_input_refs(step_def: dict) -> set[str]:
    """Extract input.* references from params, input_map, pre_compute, prompt_template."""
    refs: set[str] = set()

    # $ref in params
    for v in step_def.get("params", {}).values():
        if isinstance(v, dict) and "$ref" in v:
            ref = v["$ref"]
            if ref.startswith("input."):
                refs.add(ref.split(".")[1])

    # $ref in input_map
    for v in step_def.get("input_map", {}).values():
        if isinstance(v, dict) and "$ref" in v:
            ref = v["$ref"]
            if ref.startswith("input."):
                refs.add(ref.split(".")[1])

    # $ref in pre_compute params
    for pc in step_def.get("pre_compute", []):
        for v in pc.get("params", {}).values():
            if isinstance(v, dict) and "$ref" in v:
                ref = v["$ref"]
                if ref.startswith("input."):
                    refs.add(ref.split(".")[1])

    # prompt_template input_keys
    pt = step_def.get("prompt_template", {})
    for key in pt.get("input_keys", []):
        refs.add(key)

    return refs


# ── Strategy 8: Pydantic model drift ────────────────────────────────


def check_pydantic_model_drift(flows: dict) -> list[LintResult]:
    """Check that all fields in compiled.json are accepted by Pydantic models.

    Compares the keys present in compiled flow JSON against the fields
    declared on the corresponding Pydantic models (FlowDefinition,
    StepDefinition, ResolverDefinition). Any key present in the JSON
    but missing from the Pydantic model would be silently dropped at
    load time, which can cause subtle bugs.
    """
    # Ensure agent package is importable (linter may run from dev/ or project root)

    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        from agent.models import FlowDefinition, StepDefinition, ResolverDefinition
    except ImportError as e:
        return [
            LintResult(
                level="WARNING",
                flow="(global)",
                step=None,
                check="pydantic_drift",
                message=f"Could not import agent.models — skipping drift check: {e}",
            )
        ]

    flow_fields = set(FlowDefinition.model_fields.keys())
    step_fields = set(StepDefinition.model_fields.keys())
    resolver_fields = set(ResolverDefinition.model_fields.keys())

    results: list[LintResult] = []

    for flow_name, flow_def in flows.items():
        if not isinstance(flow_def, dict) or "steps" not in flow_def:
            continue

        # Check flow-level keys
        for key in flow_def:
            if key == "steps":
                continue
            if key not in flow_fields:
                results.append(
                    LintResult(
                        level="ERROR",
                        flow=flow_name,
                        step=None,
                        check="pydantic_drift",
                        message=f"CUE field '{key}' not in FlowDefinition model — will be silently dropped",
                    )
                )

        # Check step-level keys
        for step_name, step_def in flow_def.get("steps", {}).items():
            if not isinstance(step_def, dict):
                continue
            for key in step_def:
                if key not in step_fields:
                    results.append(
                        LintResult(
                            level="ERROR",
                            flow=flow_name,
                            step=step_name,
                            check="pydantic_drift",
                            message=f"CUE field '{key}' not in StepDefinition model — will be silently dropped",
                        )
                    )

            # Check resolver keys
            resolver = step_def.get("resolver")
            if isinstance(resolver, dict):
                for key in resolver:
                    if key not in resolver_fields:
                        results.append(
                            LintResult(
                                level="ERROR",
                                flow=flow_name,
                                step=step_name,
                                check="pydantic_drift",
                                message=f"CUE resolver field '{key}' not in ResolverDefinition model — will be silently dropped",
                            )
                        )

    return results


# ── Main ─────────────────────────────────────────────────────────────


# ── Check 9: Prompt text living in Python instead of the store ──────
#
# prompts/ is the auditable surface: a reviewer can diff it, a lint check
# can parse it, and check_prompt_parser_contracts can verify the JSON keys
# a prompt asks for against the keys its parser reads. A prompt built from
# a string literal inside an action gets none of that, and the drift is
# gradual — each individual literal looks like a reasonable local choice.
#
# Two signals, both narrow enough to keep false positives near zero:
#   A. a literal bound to a prompt-ish NAME (*_prompt, *_instruction, ...)
#   B. a literal using the prompt-store interpolation syntax ({context.x})
# Docstrings are excluded structurally via AST rather than by heuristic.

_PROMPT_NAME_HINTS = (
    "prompt",
    "instruction",
    "template",
    "directive",
    "rubric",
    "brief",
)
_PROMPT_MIN_CHARS = 200
_INTERPOLATION_MIN_CHARS = 60


def _docstring_node_ids(tree: ast.AST) -> set[int]:
    """ids of Constant nodes that are docstrings, so they can be skipped."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def check_prompt_text_in_python(agent_dir: Path) -> list[LintResult]:
    """Flag prompt text embedded in Python rather than the prompts/ store."""
    results: list[LintResult] = []
    if not agent_dir.exists():
        return results

    for path in sorted(agent_dir.rglob("*.py")):
        # This module is tooling, not agent behaviour, and it necessarily
        # quotes the interpolation syntax it detects — it would flag itself.
        if "__pycache__" in str(path) or path.name == "flow_lint.py":
            continue
        try:
            source = path.read_text()
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        docstrings = _docstring_node_ids(tree)
        flagged: set[int] = set()
        rel = path.as_posix()

        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if not any(h in n.lower() for n in names for h in _PROMPT_NAME_HINTS):
                continue
            text = node.value.value
            if len(text) < _PROMPT_MIN_CHARS:
                continue
            flagged.add(id(node.value))
            results.append(
                LintResult(
                    level="WARNING",
                    flow=rel,
                    step=f"line {node.lineno}",
                    check="prompt_text_in_python",
                    message=(
                        f"`{names[0]}` is {len(text)} chars of prompt text in "
                        f"Python — prompts/ is the auditable store, and text "
                        f"here is invisible to prompt/parser contract checks"
                    ),
                )
            )

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in docstrings or id(node) in flagged:
                continue
            text = node.value
            if len(text) < _INTERPOLATION_MIN_CHARS:
                continue
            if "{context." not in text and "{input." not in text:
                continue
            results.append(
                LintResult(
                    level="WARNING",
                    flow=rel,
                    step=f"line {getattr(node, 'lineno', 0)}",
                    check="prompt_text_in_python",
                    message=(
                        "string literal uses prompt-store interpolation "
                        "syntax ({context.*}/{input.*}) outside prompts/ — "
                        "this is a prompt fragment in Python"
                    ),
                )
            )

    return results


def lint(
    compiled_path: str = "flows/compiled.json",
    action_dir: str = "agent/actions",
    prompts_dir: str = "prompts",
    verbose: bool = False,
) -> list[LintResult]:
    """Run all lint checks and return findings."""
    compiled = Path(compiled_path)
    actions = Path(action_dir)
    prompts = Path(prompts_dir)

    if not compiled.exists():
        print(f"ERROR: {compiled} not found")
        sys.exit(1)

    with open(compiled) as f:
        flows = json.load(f)

    # Build introspection data
    action_name_map = _build_action_name_map(actions / "registry.py")
    context_reads = _extract_action_context_reads(actions)
    param_reads = _extract_action_param_reads(actions)
    internal_guards = _extract_action_internal_guards(actions)

    results: list[LintResult] = []

    # Strategy 1: Action contract violations
    results.extend(
        check_action_contracts(flows, action_name_map, context_reads, param_reads)
    )

    # Strategy 2: Unguarded cycles
    results.extend(check_unguarded_cycles(flows, action_name_map, internal_guards))

    # Strategy 3: Publish/consume chains
    results.extend(check_publish_consume_chains(flows))
    results.extend(check_precompute_context_declared(flows))

    # Strategy 3c: Dead publishes (the design_gate_feedback class), and
    # keys the flow layer declares nothing about but Python reads.
    results.extend(check_dead_publishes(flows, actions.parent))

    # Strategy 3b: Path reachability of required context (stronger —
    # catches gaps where the publisher exists but isn't on the taken path)
    results.extend(check_path_reachability(flows))

    # Strategy 4: Prompt/parser contracts
    results.extend(check_prompt_parser_contracts(prompts, actions))

    # Ported 5: Unused optional inputs
    results.extend(check_unused_optional_inputs(flows))

    # Ported 6: Prompt conventions
    results.extend(check_prompt_conventions(flows, prompts))

    # Ported 7: Resolver conventions
    results.extend(check_resolver_conventions(flows))

    # Strategy 8: Pydantic model drift (CUE fields vs Pydantic fields)
    results.extend(check_pydantic_model_drift(flows))

    # Strategy 9: Prompt text that has drifted out of the prompts/ store
    results.extend(check_prompt_text_in_python(actions.parent))

    if not verbose:
        results = [r for r in results if r.level != "INFO"]

    # Sort: ERROR first, then WARNING, then INFO; then by flow/step
    severity_order = {"ERROR": 0, "WARNING": 1, "INFO": 2}
    results.sort(
        key=lambda r: (
            severity_order.get(r.level, 3),
            r.flow,
            r.step or "",
        )
    )

    return results


def main() -> None:
    verbose = "--verbose" in sys.argv
    compiled = "flows/compiled.json"
    for i, arg in enumerate(sys.argv):
        if arg == "--compiled" and i + 1 < len(sys.argv):
            compiled = sys.argv[i + 1]

    results = lint(compiled_path=compiled, verbose=verbose)

    errors = [r for r in results if r.level == "ERROR"]
    warnings = [r for r in results if r.level == "WARNING"]
    infos = [r for r in results if r.level == "INFO"]

    for r in results:
        print(r)

    print()
    print(f"{'=' * 60}")
    print(
        f"Lint: {len(errors)} errors, {len(warnings)} warnings"
        + (f", {len(infos)} info" if verbose else "")
    )

    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
