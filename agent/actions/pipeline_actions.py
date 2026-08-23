"""New actions for the CUE flow pipeline.

These actions support the file_ops lifecycle (validation, retry budget)
and prepare_context (git summary). They're registered in the action registry
alongside existing actions.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import sys

from agent import languages
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)


# ── Repair test loop (Phase B.5) ──────────────────────────────────────
# For a repair-profile brownfield goal, the repo's OWN failing tests are the
# ground truth: the verification, the interface spec (how the code is called),
# and the feedback loop. The retest proved every failing run had the right
# file/symbol but no working test-in-the-loop; langcodes won because 7 of 30
# turns were real pytest runs iterated to green. These helpers select the
# relevant test files from goal terms (via the unused search_files effect) and
# capture a baseline so downstream can (a) dispatch a deterministic pytest
# verification and (b) seed diagnose with the failing test's call signature.

_STOPWORDS = frozenset(
    "the a an and or but for with when then this that from into via must should "
    "make sure ensure fix add remove change update value values same produce "
    "produces produced correctly correct properly using use uses given return "
    "returns object objects method function class test tests case cases "
    # low-signal bug-report prose — present in the description, absent in code
    "missing broken incorrect wrong fails failing failed raise raises raised "
    "error errors bug issue does not doesnt implement implemented support "
    "supported handle handled expected actual instead currently".split()
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_BACKTICK_RE = re.compile(r"`([^`]+)`")
_TEST_PATH_RE = re.compile(r"(^|/)(test_|tests?/|conftest)")


def extract_repair_terms_tiered(description: str) -> tuple[list[str], list[str]]:
    """(strong, weak) terms from a goal description.

    strong — quoted `literals` and CamelCase/snake_case/mixed-case identifiers
    (the names a test file would actually reference). weak — remaining
    lowercase prose words. The tiers matter: the corrected-retest miss came
    from weak words ("file", "open", "lines") polluting the grep alternation —
    big generic test files out-hit the right one on prose, so fsspec selected
    test_local/test_cached over test_dirfs and astropy selected
    test_c_reader/test_table over test_qdp. Search STRONG terms alone whenever
    any exist; weak is a fallback for identifier-free descriptions only.
    """
    strong: list[str] = []
    weak: list[str] = []
    seen: set[str] = set()

    def _add(t: str, bucket: list[str]) -> None:
        t = t.strip().strip(".")
        if not t or t.lower() in _STOPWORDS or len(t) < 3:
            return
        if t not in seen:
            seen.add(t)
            bucket.append(t)

    # Backticked literals first — highest signal (dotted paths, call exprs).
    for m in _BACKTICK_RE.finditer(description):
        for tok in _IDENT_RE.findall(m.group(1)):
            _add(tok, strong)
    # CamelCase / snake_case / has_underscore / mixed-case identifiers.
    for tok in _IDENT_RE.findall(description):
        if "_" in tok or not tok.islower() or not tok.isalpha():
            _add(tok, strong)
    # Lowercase content words (bounded) — weak tier.
    for tok in _IDENT_RE.findall(description):
        if len(weak) >= 8:
            break
        _add(tok, weak)
    return strong[:12], weak


def extract_repair_terms(description: str) -> list[str]:
    """Flat view of the tiered extraction (strong first)."""
    strong, weak = extract_repair_terms_tiered(description)
    return (strong + weak)[:12]


def _is_test_path(path: str) -> bool:
    return bool(_TEST_PATH_RE.search(path)) and path.endswith(".py")


def _parse_pytest_output(out: str) -> tuple[list[str], bool]:
    """Return (failing_node_ids, collect_ok). collect_ok is False when the run
    died at collection/import (an INTERNALERROR / import error / errors during
    collection) — the baseline stand-down signal: a suite that can't collect at
    baseline can never indict an edit."""
    nodes = re.findall(r"(?m)^FAILED (\S+)", out)
    nodes += [n for n in re.findall(r"(?m)^ERROR (\S+::\S+)", out) if n not in nodes]
    collect_broken = bool(
        re.search(
            r"errors during collection|ImportError while importing|INTERNALERROR|"
            r"ERROR collecting|ModuleNotFoundError",
            out,
        )
    )
    return nodes, not collect_broken


def _module_stems(terms: list[str]) -> set[str]:
    """Module-name stems (lowercased) worth matching a test file against: the
    identifier parts of the terms (e.g. a fix naming `sympy/geometry/point.py`
    or `Point.distance` → {'point','distance',...}). Case-insensitive — `Point`
    must match `test_point.py`. Extra stems are harmless: they only help when a
    test BASENAME equals one."""
    stems: set[str] = set()
    for t in terms:
        base = t.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # path/ext tail
        for part in re.split(r"[^A-Za-z0-9]+", base):
            if len(part) >= 3 and part.isalpha():
                stems.add(part.lower())
    return stems


async def _grep_test_files(effects, terms: list[str]) -> list[str]:
    """Rank candidate test files. Primary key: MODULE-NAME MATCH — a test whose
    basename is test_<module> / <module>_test for a module named in the terms
    (sympy pilot: point.py's real regression lives in test_point.py, but plain
    term-hit ranking picked test_args/test_line and verified green against the
    wrong suite). Secondary key: term-hit count. Both glob patterns queried
    (LocalEffects expands non-recursively; container grep + mock fnmatch
    recurse; a double-hit counts consistently)."""
    if not terms:
        return []
    content_pattern = "|".join(re.escape(t) for t in terms)
    stems = _module_stems(terms)
    hits: dict[str, int] = {}
    for pattern in ("*.py", "**/*.py"):
        try:
            res = await effects.search_files(pattern, content_pattern=content_pattern)
        except Exception as e:  # noqa: BLE001
            logger.warning("repair-tests: search_files(%s) failed (%s)", pattern, e)
            continue
        for m in getattr(res, "matches", []) or []:
            fp = getattr(m, "file_path", "")
            if _is_test_path(fp):
                hits[fp] = hits.get(fp, 0) + 1

    def _module_match(fp: str) -> int:
        stem = os.path.basename(fp)[:-3]  # strip .py
        base = (
            stem[5:]
            if stem.startswith("test_")
            else (stem[:-5] if stem.endswith("_test") else stem)
        )
        return 1 if base in stems else 0

    # module-match first (desc), then hit count (desc), then path (stable).
    return [
        fp
        for fp, _ in sorted(
            hits.items(), key=lambda kv: (-_module_match(kv[0]), -kv[1], kv[0])
        )
    ]


async def _baseline(
    effects, test_files: list[str]
) -> tuple[str, list[str], bool, bool]:
    """Run the candidate suite once. Returns (command, failing_nodes,
    collect_ok, witnessed) — witnessed = the suite FAILS at baseline, i.e. it
    actually exhibits the defect this goal exists to fix."""
    command = "python -m pytest -q -x --no-header " + " ".join(test_files)
    failing_nodes: list[str] = []
    collect_ok = True
    try:
        # 90s: must fit inside the TB adapter's wall-clock park margin
        # (0.1 x deadline = ~100s) — a longer exec straddling the deadline gets
        # its container torn down mid-flight (the b5c 404 teardown race).
        base = await effects.run_command(["/bin/sh", "-c", command], timeout=90)
        out = (getattr(base, "stdout", "") or "") + (getattr(base, "stderr", "") or "")
        failing_nodes, collect_ok = _parse_pytest_output(out)
    except Exception as e:  # noqa: BLE001 — baseline is best-effort
        logger.warning("repair-tests: baseline run failed (%s)", e)
        return command, [], True, True  # infra miss — don't reject the pick
    # A WITNESS is a named failing/errored test node or a broken collection —
    # NOT a bare nonzero exit code. `_parse_pytest_output` already captures
    # FAILED, node-level ERROR, and collection breaks, so a nonzero rc with
    # none of those is noise (warnings-as-errors, session teardown, plugin exit
    # codes). Treating that as a witness dispatched a deterministic verify
    # against an effectively-green baseline — which, in SWE-bench (the failing
    # regression test is HELD OUT, so no in-repo test indicts THIS bug),
    # trivially "passed" and completed the goal with zero edits (pilot-3: 4
    # empty patches from django-10554/pylint-4551/4604/sympy). Without a real
    # witness the caller returns {} and the goal routes diagnose-first from the
    # problem statement instead.
    witnessed = bool(failing_nodes) or not collect_ok
    return command, failing_nodes, collect_ok, witnessed


async def derive_repair_tests(effects, goal_description: str) -> dict:
    """Select ≤2 relevant test files for a repair goal and baseline them.

    Returns {command, test_files, failing_nodes, collect_ok, derived: True},
    or {} when no candidate WITNESSES the defect (→ caller falls back to the
    LLM evaluator).

    THE WITNESS RULE: on a repair mission, a suite that is fully green at
    baseline cannot be the ground truth — the bug is unfixed, so the right
    tests must be failing. The corrected retest proved both halves live:
    fsspec/astropy selected green suites (weak-term pollution) and completed
    confidently wrong; langcodes selected a baseline-failing suite and
    RESOLVED. Strong terms are searched alone when any exist; a green first
    pick retries the next-ranked candidates once before falling back.
    """
    if effects is None:
        return {}
    strong, weak = extract_repair_terms_tiered(goal_description)
    ranked = await _grep_test_files(effects, strong if strong else weak)
    if not ranked and strong:
        ranked = await _grep_test_files(effects, weak)  # identifier miss — try prose
    if not ranked:
        return {}

    # Try up to two candidate pairs, requiring a baseline witness.
    for start in (0, 2):
        test_files = ranked[start : start + 2]
        if not test_files:
            break
        command, failing_nodes, collect_ok, witnessed = await _baseline(
            effects, test_files
        )
        if witnessed:
            return {
                "command": command,
                "test_files": test_files,
                "failing_nodes": failing_nodes,
                "collect_ok": collect_ok,
                "derived": True,
            }
        logger.info(
            "repair-tests: %s green at baseline — not a witness, trying next",
            test_files,
        )
    return {}


def is_repair_profile(mission) -> bool:
    return getattr(getattr(mission, "config", None), "task_profile", "") == "repair"


async def action_derive_repair_tests(step_input: StepInput) -> StepOutput:
    """Standalone action: derive + persist a repair goal's test loop onto the
    goal (repair_tests + a tighten-only acceptance check). Idempotent via
    repair_tests.derived. Reused by the functional sweep and the test gate.

    Context: mission (required). Inputs: goal_id. Publishes: mission.
    """
    effects = step_input.effects
    mission = step_input.context.get("mission")
    goal_id = str((step_input.inputs or {}).get("goal_id", "") or "")
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None or not is_repair_profile(mission):
        return StepOutput(result={"derived": False}, observations="not a repair goal")
    if (getattr(goal, "repair_tests", None) or {}).get("derived"):
        return StepOutput(
            result={"derived": True, "cached": True},
            observations="repair tests already derived",
        )
    rt = await derive_repair_tests(effects, goal.description)
    goal.repair_tests = rt
    if rt.get("command"):
        # Tighten-only acceptance check: the pytest command runs each
        # verification pass and vetoes a credulous goal_met (C4 field).
        cmds = {c.get("command") for c in (goal.acceptance_checks or [])}
        if rt["command"] not in cmds:
            goal.acceptance_checks = list(goal.acceptance_checks or []) + [
                {"command": rt["command"], "name": "repair suite", "required": True}
            ]
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"derived": bool(rt), "test_files": rt.get("test_files", [])},
        observations=(
            f"repair tests: {rt.get('test_files')} "
            f"(collect_ok={rt.get('collect_ok')}, "
            f"{len(rt.get('failing_nodes', []))} failing)"
            if rt
            else "repair tests: no matching suite — LLM evaluator fallback"
        ),
        context_updates={"mission": mission},
    )


def _cap_diagnostic(text: str, limit: int = 1200) -> str:
    """Cap check/smoke output for storage, preserving a Python traceback's TAIL.

    Tracebacks print "most recent call last" — the exception line and the
    deepest frame (the actual fault site) are at the END. A plain head-cap
    (``text[:500]``) therefore drops exactly the part the diagnose needs to
    pick a fix target, leaving only the entry frames (e.g. ``main.py``), which
    sends repair off chasing the wrong file. When a traceback is present, keep
    the head (the marker + entry frames) AND the tail (deepest frames +
    exception); otherwise fall back to a plain head-cap.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    if "Traceback (most recent call last):" in text:
        head = limit // 3
        return text[:head] + "\n…[frames truncated]…\n" + text[-(limit - head) :]
    return text[:limit]


# Extensions that skip validation (non-code files)
_SKIP_EXTENSIONS = {
    "md",
    "txt",
    "csv",
    "cfg",
    "ini",
    "env",
    "gitkeep",
    "gitignore",
    "lock",
    "svg",
    "png",
    "jpg",
    "jpeg",
    "gif",
}

# Structured data files the program loads at runtime (world.yaml, save.json,
# pyproject.toml, …). These were previously skipped, so a malformed data file
# sailed through the structural gate and only broke at runtime — an expensive,
# often mis-diagnosed failure (e.g. Step-3.5/Qwen3-Next YAML breakages). We now
# give them a PARSE-VALIDITY check (the data analog of the Python syntax gate)
# via action_check_data_file. Parse-only — NOT style/lint — so a real
# malformation blocks the structural goal while formatting nits don't.
# The data-extension set now lives in agent/languages.py (languages.is_data).


async def _load_env_config(effects) -> dict:
    """Load ``.agent/env.json`` for the mission, returning {} on any failure.

    Read through the effects layer so the path resolves against the mission's
    working_directory — NOT the agent process cwd. (Reading it via a bare
    relative ``Path`` wrote/read the repo's own ``.agent/`` and cross-contaminated
    missions; effects.read_file is working_dir-scoped and traversal-safe.)"""
    if effects is None:
        return {}
    try:
        fc = await effects.read_file(".agent/env.json")
    except Exception:  # noqa: BLE001 - missing/unreadable env config → no config
        return {}
    if not getattr(fc, "exists", False):
        return {}
    try:
        return json.loads(getattr(fc, "content", "") or "") or {}
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Failed to parse .agent/env.json: %s", e)
        return {}


async def action_lookup_validation_env(step_input: StepInput) -> StepOutput:
    """Look up validation commands for a file extension from .agent/env.json.

    Returns env_found=true with validation_commands if the extension is known.
    Returns skip_validation=true for non-code files.
    Returns env_found=false if the extension is unknown (triggers set_env).
    """
    target = step_input.params.get("target", "")
    if not target:
        return StepOutput(
            result={"skip_validation": True},
            observations="No target file specified — skipping validation",
        )

    ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""

    if ext in _SKIP_EXTENSIONS:
        return StepOutput(
            result={"skip_validation": True},
            observations=f"Non-code file ({ext}) — skipping validation",
        )

    # Structured data files get a built-in parse-validity check (not env
    # commands, not set_env) — see action_check_data_file. Routed here before
    # the env lookup so it's consistent regardless of project tooling.
    if languages.is_data(ext):
        return StepOutput(
            result={"is_data_file": True},
            observations=f"Data file ({ext}) — parse-validity check",
        )

    env_config = await _load_env_config(step_input.effects)
    if ext in env_config:
        commands = env_config[ext]
        return StepOutput(
            result={"env_found": True},
            observations=f"Found validation config for .{ext}",
            context_updates={"validation_commands": commands},
        )

    return StepOutput(
        result={"env_found": False},
        observations=f"No validation config for .{ext} — set_env needed",
    )


# ── Post-install verification ─────────────────────────────────────────

# "PyYAML>=6.0", "requests[socks] ; python_version>'3.8'" -> the bare name.
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _declared_distributions(pyproject: str, requirements: str) -> list[str]:
    """Distribution names the project declares, from pyproject and/or
    requirements.txt.

    Distribution names are used deliberately rather than module names: the two
    routinely differ (PyYAML->yaml, beautifulsoup4->bs4), and a manifest
    declares the former. importlib.metadata resolves exactly that, so no
    name-mapping guesswork is needed.
    """
    names: list[str] = []
    if pyproject:
        try:
            import tomllib

            data = tomllib.loads(pyproject)
        except Exception:  # noqa: BLE001 — a malformed manifest is not our error
            data = {}
        deps = (data.get("project") or {}).get("dependencies") or []
        if isinstance(deps, list):
            names.extend(str(d) for d in deps)
    for line in (requirements or "").splitlines():
        line = line.strip()
        # Skip comments, blanks, and pip flags (-r, -e, --index-url, …).
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        names.append(line)
    out: list[str] = []
    for raw in names:
        m = _REQ_NAME_RE.match(raw)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


async def action_verify_project_env(step_input: StepInput) -> StepOutput:
    """Confirm the DECLARED dependencies are actually present.

    project_ops used to report success on "the install commands exited 0",
    which is not the same claim. The empty-venv run reported success on every
    cycle while nothing was installed, and that false success propagated into
    the workspace ledger ("[provision] … — success") where the next diagnosis
    read it as settled (dev/POOLSIDE_TRAP_ROOTCAUSE.md).

    Runs one probe in the interpreter the project will actually use — so it
    also catches an install that landed in a DIFFERENT interpreter than the one
    the program runs under, which is the failure this whole subsystem exists to
    prevent.

    Result:
        env_verified: bool — nothing declared, or everything declared is present
        missing: list[str] — declared distributions the interpreter cannot find
    """
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"env_verified": True, "missing": []},
            observations="No effects interface — skipping env verification",
        )

    async def _maybe_read(path: str) -> str:
        try:
            if not await effects.file_exists(path):
                return ""
            fc = await effects.read_file(path)
            return getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001 — absence is the common case
            return ""

    declared = _declared_distributions(
        await _maybe_read("pyproject.toml"), await _maybe_read("requirements.txt")
    )
    if not declared:
        return StepOutput(
            result={"env_verified": True, "missing": []},
            observations="No declared dependencies to verify",
        )

    probe = (
        "import importlib.metadata as md, sys\n"
        "missing = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        md.distribution(name)\n"
        "    except Exception:\n"
        "        missing.append(name)\n"
        "print(','.join(missing))\n"
    )
    res = await effects.run_command(["python", "-c", probe, *declared], timeout=60)

    if res.return_code != 0:
        # The probe itself could not run — report unverified rather than
        # inventing a pass. A broken interpreter is exactly what we are looking
        # for here.
        logger.warning(
            "env verification probe failed (rc=%s): %s",
            res.return_code,
            (res.stderr or "")[:200],
        )
        return StepOutput(
            result={"env_verified": False, "missing": declared},
            observations=f"Could not verify {len(declared)} declared dependencies",
        )

    missing = [n for n in (res.stdout or "").strip().split(",") if n]
    if missing:
        logger.warning(
            "env verification: %d declared dependencies are NOT installed in the "
            "interpreter the project runs under: %s",
            len(missing),
            ", ".join(missing),
        )
    return StepOutput(
        result={"env_verified": not missing, "missing": missing},
        observations=(
            f"All {len(declared)} declared dependencies present"
            if not missing
            else f"Missing declared dependencies: {', '.join(missing)}"
        ),
    )


async def action_collect_env_field(step_input: StepInput) -> StepOutput:
    """Collect a named field from all language sections in .agent/env.json.

    Iterates every language section (e.g., "py", "js") in the env config,
    extracts the named field from each, and publishes a flat list of
    shell command strings. Used by project_ops to collect install_command
    from all languages for sequential execution via run_commands.

    Params:
        field: The key to extract from each lang section (e.g., "install_command")
        output_key: Context key to publish the collected list under
                    (default: "collected_commands")

    Result:
        commands_found: bool — whether any commands were collected
    Publishes:
        <output_key>: list of shell command strings
    """
    field = step_input.params.get("field", "install_command")
    output_key = step_input.params.get("output_key", "collected_commands")

    env_config = await _load_env_config(step_input.effects)
    if not env_config:
        return StepOutput(
            result={"commands_found": False},
            observations="No env config found",
            context_updates={output_key: []},
        )

    commands = []
    for lang, section in env_config.items():
        if isinstance(section, dict) and field in section:
            cmd = section[field]
            if cmd:
                # Convert command arrays to shell strings
                if isinstance(cmd, list):
                    cmd = " ".join(str(part) for part in cmd)
                commands.append(str(cmd))
                logger.info("Collected %s from %s: %s", field, lang, cmd)

    # Route Python dependency installs through uv so each project gets a clean,
    # isolated venv and the install lands in the SAME interpreter the framework
    # runs the program under. Bare `pip` is ambient and was silently installing
    # into the wrong (sometimes broken) interpreter while the program ran under
    # another — every interactive goal then failed regardless of code quality.
    if field == "install_command":
        commands = _uvize_install_commands(commands, env_config)

    return StepOutput(
        result={"commands_found": len(commands) > 0},
        observations=f"Collected {len(commands)} {field} command(s) from env config",
        context_updates={output_key: commands},
    )


def _uvize_install_commands(commands: list[str], env_config: dict) -> list[str]:
    """Rewrite Python installs to use uv and a per-project venv.

    - ``pip``/``pip3`` → ``uv pip`` (installs into the per-project venv).
    - ``pip install -e .`` → ``uv pip install -r pyproject.toml``. Editable
      installs BUILD the project, which fails for the common flat-layout (several
      top-level modules — setuptools refuses auto-discovery). The framework runs
      programs via ``python main.py`` from the project root, so only the declared
      DEPENDENCIES are needed; installing them from pyproject sidesteps the build.
    - Prepend ``uv venv --allow-existing`` for Python **only when a Python
      install will actually run into it**.
    Non-Python install commands (npm, cargo, …) pass through unchanged.

    THE VENV IS GATED ON A REAL INSTALL, NOT ON A ``py`` SECTION. It used to be
    seeded from ``isinstance(env_config.get("py"), dict)``, and ``syntax`` is a
    required field for every detected extension — so every Python project always
    has a ``py`` section. A model that omitted ``install_command`` (its own
    prompt calls that "optional") therefore produced NO commands, and this
    function still returned ``["uv venv --allow-existing --python 3.x"]``: venv
    creation with nothing after it. The caller computes ``commands_found`` AFTER
    this rewrite, so that lone line made the step report success while creating
    an EMPTY venv — which then shadowed a working system interpreter for every
    command and PTY for the rest of the mission (``local.py`` activates any
    ``.venv`` with a ``bin/python``). One 2h run lost ~50 minutes to the
    resulting blind diagnose loop; see dev/POOLSIDE_TRAP_ROOTCAUSE.md.

    An empty venv is strictly worse than no venv: the arm in that comparison
    that created none ran fine on system Python."""
    out: list[str] = []
    python_install = False
    for cmd in commands:
        toks = cmd.split()
        # Normalize bare pip → uv pip (the LLM may emit either, or `uv pip`).
        if toks and toks[0] in ("pip", "pip3"):
            toks = ["uv", "pip", *toks[1:]]
        # `python -m pip install …` is the third form models reach for.
        elif toks[:3] in (["python", "-m", "pip"], ["python3", "-m", "pip"]):
            toks = ["uv", "pip", *toks[3:]]
        # An editable project install (`... install -e .`) BUILDS the project,
        # which fails for the common flat-layout (several top-level modules —
        # setuptools refuses auto-discovery). The framework runs programs via
        # `python main.py` from the root, so install the declared DEPENDENCIES
        # from pyproject instead. Gated on a uv pip install so non-pip commands
        # that merely contain "-e" can't be clobbered.
        if toks[:3] == ["uv", "pip", "install"]:
            python_install = True
            if "-e" in toks[3:] or "--editable" in toks[3:]:
                toks = ["uv", "pip", "install", "-r", "pyproject.toml"]
        out.append(" ".join(toks))

    # A Python project that declared NO install command is the trap condition.
    # Say so — the step used to report success and move on, leaving the failure
    # to surface an hour later as a ModuleNotFoundError with no trace back here.
    if isinstance(env_config.get("py"), dict) and not python_install:
        logger.warning(
            "env detection produced a `py` section but NO Python install "
            "command; skipping venv creation so the system interpreter stays "
            "usable. Declared dependencies will NOT be installed — if the "
            "project imports third-party packages it will fail at startup."
        )

    if python_install:
        # Pin the venv to the interpreter the framework itself runs on.
        # Unpinned, uv discovers whatever PATH offers — live failure: the
        # macOS system Python 3.9.6, where the 3.10+ union/generic syntax
        # models routinely write parses (every syntax gate passes) but
        # raises TypeError at import. The smoke gate then fails forever
        # while diagnose flounders, because the code is CORRECT for any
        # modern interpreter (284 repair dispatches on one goal, live).
        pinned = f"{sys.version_info.major}.{sys.version_info.minor}"
        out.insert(0, f"uv venv --allow-existing --python {pinned}")
    return out


def _substitute_command(template, file_path: str, module_name: str) -> list | None:
    """Fill {file}/{module} placeholders in an env command template."""
    if isinstance(template, list):
        return [
            part.replace("{file}", file_path).replace("{module}", module_name)
            for part in template
        ]
    if isinstance(template, str):
        return (
            template.replace("{file}", file_path)
            .replace("{module}", module_name)
            .split()
        )
    return None


async def action_run_validation_checks_from_env(
    step_input: StepInput,
) -> StepOutput:
    """Execute validation commands from the env config.

    Runs formatter first (if configured), then syntax (required), import,
    and lint checks deterministically, for EVERY file the operation
    changed (params.files, falling back to the single dispatch target).
    Cross-file batches are Python-only today, so one env command set
    (selected by the target's extension) covers all files.
    No LLM involvement — commands come from .agent/env.json.
    """
    effects = step_input.effects
    commands = step_input.context.get("validation_commands", {})
    target = step_input.params.get("target", "")
    files_param = step_input.params.get("files", []) or []
    if isinstance(files_param, str):
        files_param = [files_param] if files_param else []
    files = [str(f) for f in files_param if str(f)] or ([target] if target else [])

    if not effects or not commands or not files:
        return StepOutput(
            result={"all_passing": True},
            observations="No commands, files, or effects — skipping",
            context_updates={"validation_results": []},
        )

    results = []
    syntax_failed = False
    has_issues = False

    for file_path in files:
        module_name = ""
        if file_path.endswith(".py"):
            module_name = file_path.replace("/", ".").replace(".py", "")
            if module_name.startswith("."):
                module_name = module_name[1:]

        # ── Run formatter before validation (non-fatal) ──────────
        # If a formatter command is configured, run it to normalize
        # indentation, whitespace, and style before checks.
        fmt_cmd = _substitute_command(commands.get("formatter"), file_path, module_name)
        if fmt_cmd:
            try:
                await effects.run_command(fmt_cmd, timeout=30)
                logger.info("Formatter ran: %s", " ".join(fmt_cmd))
            except Exception as e:
                logger.warning("Formatter failed (non-fatal): %s — %s", fmt_cmd, e)

        for tier in ("syntax", "import", "lint"):
            cmd = _substitute_command(commands.get(tier), file_path, module_name)
            if not cmd:
                continue

            try:
                result = await effects.run_command(cmd, timeout=30)
                passed = result.return_code == 0
            except Exception as e:
                logger.warning("Validation command failed: %s — %s", cmd, e)
                passed = False
                result = type(
                    "R", (), {"stdout": "", "stderr": str(e), "return_code": 1}
                )()

            check = {
                "name": f"{tier}: {file_path}",
                "passed": passed,
                "tier": tier,
                "required": tier == "syntax",
                "stdout": (
                    _cap_diagnostic(result.stdout) if hasattr(result, "stdout") else ""
                ),
                "stderr": (
                    _cap_diagnostic(result.stderr) if hasattr(result, "stderr") else ""
                ),
            }
            results.append(check)

            if not passed:
                if tier == "syntax":
                    syntax_failed = True
                else:
                    has_issues = True

    # Build a human-readable formatted output string from the check
    # results so downstream consumers (e.g. the diagnose_issue flow
    # seeding prompt) can interpolate it directly as terminal output.
    # validation_results remains available as structured data for
    # consumers that need to reason over pass/fail tiers.
    output_lines: list[str] = []
    for r in results:
        tier_name = r.get("name", "?")
        status = "PASS" if r.get("passed") else "FAIL"
        output_lines.append(f"[{status}] {tier_name}")
        stdout = r.get("stdout", "")
        stderr = r.get("stderr", "")
        if stdout:
            output_lines.append(f"  stdout: {stdout}")
        if stderr:
            output_lines.append(f"  stderr: {stderr}")
    # ── Smoke-boot check: does the program still start? ───────────
    # A syntactically valid edit can still break startup (the gate later
    # re-reports it and reopens goals — observed live: one bad edit
    # cascaded into 9 reopens). Once the program is KNOWN bootable
    # (environment_verified — during the structural phase it legitimately
    # isn't yet), every write re-runs the architecture's smoke command;
    # a failure routes into the same-dispatch self-correct loop instead
    # of surfacing N cycles later as behavioral symptoms.
    smoke_failed = False
    if not syntax_failed:
        smoke_cmd = ""
        smoke_baseline_ok = None
        try:
            mission = await effects.load_mission()
            if mission is not None and getattr(mission, "environment_verified", False):
                arch = getattr(mission, "architecture", None)
                smoke_cmd = (getattr(arch, "effective_smoke_command", "") or "").strip()
                smoke_baseline_ok = getattr(mission, "smoke_baseline_ok", None)
        except Exception:
            smoke_cmd = ""
        # BASELINE STAND-DOWN: the smoke command already failed on the
        # untouched repo (unbuilt brownfield checkout — swe-bench-astropy's
        # `import astropy` fails regardless of the edit). A check that failed
        # at baseline can never indict THIS edit; running it would route a
        # correct patch into the self-correct rewrite loop chasing an
        # unappeasable environmental failure (~290s of whole-file rewrites
        # observed). Skip it entirely and say so.
        if smoke_cmd and smoke_baseline_ok is False:
            output_lines.append(
                f"[SKIP] smoke_boot: {smoke_cmd} — failing at BASELINE "
                "(pre-existing, cannot indict this edit)"
            )
            smoke_cmd = ""
        if smoke_cmd:
            try:
                smoke = await effects.run_command(
                    ["/bin/sh", "-c", smoke_cmd], timeout=20
                )
                passed = smoke.return_code == 0 and not smoke.timed_out
            except Exception as e:
                smoke = type(
                    "R", (), {"stdout": "", "stderr": str(e), "return_code": 1}
                )()
                passed = False
            results.append(
                {
                    "name": f"smoke_boot: {smoke_cmd}",
                    "passed": passed,
                    "tier": "smoke",
                    "required": True,
                    "stdout": _cap_diagnostic(getattr(smoke, "stdout", "")),
                    "stderr": _cap_diagnostic(getattr(smoke, "stderr", "")),
                }
            )
            if not passed:
                smoke_failed = True
                output_lines.append(f"[FAIL] smoke_boot: {smoke_cmd}")
                if getattr(smoke, "stderr", ""):
                    output_lines.append(f"  stderr: {_cap_diagnostic(smoke.stderr)}")
                output_lines.append(
                    "  The program no longer starts after this edit — the edit "
                    "must be corrected."
                )
            else:
                output_lines.append(f"[PASS] smoke_boot: {smoke_cmd}")

    validation_output = "\n".join(output_lines)

    # Oversized-symbol-fix signal (M4 defense-in-depth): a large target file
    # with a known symbol should re-diagnose a fresh symbol-scoped patch on a
    # check failure, NOT whole-file self-correct (swe-bench-astropy regenerated
    # a 19KB qdp.py twice, ~102s each, and that regeneration is where an
    # invalid `str | None` py3.9 annotation crept in). The whole-file rewrite
    # path is fine for small files; it's the escalation on big ones that hurts.
    oversized_symbol_fix = False
    tgt_symbol = str(step_input.params.get("target_symbol", "") or "").strip()
    if tgt_symbol and target:
        try:
            fc = await effects.read_file(target)
            if getattr(fc, "exists", False):
                if len((fc.content or "").splitlines()) > 300:
                    oversized_symbol_fix = True
        except Exception:
            pass

    # THE SHARED DECISION. `has_issues` is true for EVERY non-required finding,
    # so a resolver routing on it cannot tell a lint failure from anything else
    # — which is exactly how the serial path came to report a lint failure as a
    # success while the batch path blocked on it for one pass. Publishing the
    # reason lets file_ops route on the same precedence mission_control uses,
    # from the same function. See block_reason_from_checks for the incident.
    #
    # Review flags default False here (this action sees files and commands, not
    # goals). That is the right default: "not yet reviewed" is what makes the
    # one-pass contract fire, and the bound is enforced by the retry budget.
    from agent.actions.reporting_actions import block_reason_from_checks

    failed_names = [r.get("name", "") for r in results if not r.get("passed")]
    block_reason = block_reason_from_checks(failed_names)

    return StepOutput(
        result={
            "all_passing": not syntax_failed and not has_issues and not smoke_failed,
            "syntax_failed": syntax_failed,
            "smoke_failed": smoke_failed,
            "has_issues": has_issues,
            "oversized_symbol_fix": oversized_symbol_fix,
            # "syntax" | "import" | "lint" | None — None means nothing blocking.
            "block_reason": block_reason or "",
        },
        observations=f"Validation: {sum(1 for r in results if r['passed'])}/{len(results)} checks passed",
        context_updates={
            "validation_results": results,
            "validation_output": validation_output,
        },
    )


def _parse_data_file(ext: str, content: str) -> tuple[bool, str]:
    """Return (parses_ok, detail) — parse-only validity for one data file.

    JSON/TOML use the stdlib; YAML needs PyYAML, and if it's not importable we
    degrade gracefully (pass with a note) rather than false-fail a project that
    doesn't ship a YAML parser.
    """
    try:
        if ext == "json":
            json.loads(content)
            return True, ""
        if ext == "toml":
            try:
                import tomllib
            except ModuleNotFoundError:  # <3.11 — can't validate; don't false-fail
                return True, "tomllib unavailable — parse check skipped"
            tomllib.loads(content)
            return True, ""
        if ext in ("yaml", "yml"):
            try:
                import yaml
            except ImportError:  # PyYAML not installed — degrade gracefully
                return True, "PyYAML unavailable — parse check skipped"
            yaml.safe_load(content)
            return True, ""
    except Exception as e:  # noqa: BLE001 - any parse error → malformed file
        return False, f"{type(e).__name__}: {e}"
    return True, f"unrecognized data ext '{ext}' — skipped"


async def action_check_data_file(step_input: StepInput) -> StepOutput:
    """Parse-validity check for a structured data file (.yaml/.yml/.json/.toml).

    The data analog of the Python syntax gate: parse the file and report a
    single required ``syntax: <file>`` check, so the structural gate blocks a
    malformed data file exactly as it blocks a syntax error — instead of letting
    it pass and break (and get mis-diagnosed) at runtime. Parse-only, not
    style/lint. Output shape mirrors ``action_run_validation_checks_from_env``
    so the file_ops routing + ``structural_block_reason`` logic is unchanged.
    """
    effects = step_input.effects
    target = step_input.params.get("target", "")
    ext = target.rsplit(".", 1)[-1].lower() if "." in target else ""

    if not effects or not target:
        ok, detail = True, "no target/effects — skipped"
    else:
        try:
            fc = await effects.read_file(target)
            content = getattr(fc, "content", "") if getattr(fc, "exists", False) else ""
        except Exception as e:  # noqa: BLE001
            content, detail = "", f"could not read file: {e}"
        if not content:
            # Empty/unreadable: empty parses as valid for all three formats —
            # nothing structural to flag.
            ok, detail = True, locals().get("detail", "") or "empty file"
        else:
            ok, detail = _parse_data_file(ext, content)

    check = {
        "name": f"syntax: {target}",
        "passed": ok,
        "tier": "syntax",
        "required": True,
        "stdout": "",
        "stderr": "" if ok else detail[:500],
    }
    status = "PASS" if ok else "FAIL"
    validation_output = f"[{status}] {check['name']}"
    if not ok and detail:
        validation_output += f"\n  stderr: {detail}"
    return StepOutput(
        result={
            "all_passing": ok,
            "syntax_failed": not ok,
            "has_issues": False,
        },
        observations=f"Data-file parse check ({ext or '?'}): {status}"
        + (f" — {detail}" if detail else ""),
        context_updates={
            "validation_results": [check],
            "validation_output": validation_output,
        },
    )


def _sanitize_env_commands(env_config: dict) -> list:
    """Resolve interpreters and verify tools in model-proposed check commands.

    Mutates ``env_config`` in place; returns human-readable adjustment notes.
    Walks every language block's command lists (syntax/import/lint/formatter/
    install_command — anything shaped like [argv0, ...]):

    - argv0 ``python``/``python3`` not on PATH → rewritten to
      ``sys.executable`` (the interpreter the agent itself runs under —
      guaranteed present, has py_compile, and ``-c`` imports resolve
      against the workspace cwd exactly as before).
    - any other argv0 not on PATH → the entry is REMOVED, but ONLY for
      gate-check keys (syntax/import/lint/formatter/typecheck): the gate
      skips a check it could never run instead of failing every file with
      FileNotFoundError. Non-check commands (install_command etc.) are
      left verbatim — installs have their own normalization machinery and
      may become runnable after provisioning.

    ``{file}``-style placeholders and non-command values are untouched.
    """
    import shutil as _shutil
    import sys as _sys

    check_keys = {"syntax", "import", "lint", "formatter", "typecheck"}
    notes: list = []

    def _fix_cmd(key, cmd):
        """Returns (new_cmd_or_None, note_or_None); None cmd = drop entry."""
        if not (isinstance(cmd, list) and cmd and isinstance(cmd[0], str)):
            return cmd, None
        tok0 = cmd[0]
        if _shutil.which(tok0) is not None:
            return cmd, None
        if tok0 in ("python", "python3"):
            return [_sys.executable] + cmd[1:], (
                f"interpreter {tok0!r} not on PATH — rewrote to sys.executable"
            )
        if key in check_keys:
            return None, (
                f"tool {tok0!r} not on PATH — dropped the check entry "
                f"(gate will skip it)"
            )
        return cmd, None

    for lang, block in list(env_config.items()):
        if not isinstance(block, dict):
            continue
        for key, val in list(block.items()):
            new_cmd, note = _fix_cmd(key, val)
            if note:
                notes.append(f"[{lang}.{key}] {note}")
            if new_cmd is None:
                del block[key]
            elif new_cmd is not val:
                block[key] = new_cmd
    return notes


async def action_persist_validation_env(step_input: StepInput) -> StepOutput:
    """Parse LLM-generated validation config and save to .agent/env.json.

    The inference response should be a JSON object mapping extensions
    to validation commands (syntax, import, lint). Commands are sanitized
    before persisting — see _sanitize_env_commands.
    """
    raw = step_input.context.get("inference_response", "")

    # Parse env config from inference response
    from agent.llm_json import parse_llm_json

    env_config = None
    if isinstance(raw, dict):
        env_config = raw
    elif isinstance(raw, str):
        env_config = parse_llm_json(raw)

    if not env_config or not isinstance(env_config, dict):
        return StepOutput(
            result={"env_saved": False},
            observations="Could not parse validation config",
        )

    # SANITIZE BEFORE PERSISTING (2026-08-03, the title-match root cause).
    # These commands are model-proposed and were persisted verbatim; a
    # config that named bare `python` on a python3-only macOS made every
    # syntax/import gate fail with "No such file or directory: 'python'" —
    # ten cycles of misdiagnosis, then an environment-assert reified into
    # the module frame. Resolve the interpreter and verify every tool at
    # WRITE time, so the gate never records a command this machine cannot
    # run: a missing python/python3 argv0 is rewritten to sys.executable;
    # any other missing tool drops that check entry loudly (the gate skips
    # what it cannot run — degraded validation beats false failure).
    notes = _sanitize_env_commands(env_config)
    for note in notes:
        logger.warning("env sanitize: %s", note)

    # Persist through the effects layer so .agent/env.json lands in the mission
    # working_directory (NOT the agent process cwd, which leaked a stray .agent/
    # into the repo and shared one env.json across all missions).
    effects = step_input.effects
    if effects is None:
        return StepOutput(
            result={"env_saved": False},
            observations="No effects available to persist env config",
        )

    # Merge with existing config if present (working-dir scoped read).
    existing = await _load_env_config(effects)
    if not isinstance(existing, dict):
        existing = {}
    existing.update(env_config)

    write = await effects.write_file(".agent/env.json", json.dumps(existing, indent=2))
    if not getattr(write, "success", False):
        err = getattr(write, "error", "") or "unknown"
        logger.error("Failed to write .agent/env.json: %s", err)
        return StepOutput(
            result={"env_saved": False},
            observations=f"Failed to write env config: {err}",
        )

    return StepOutput(
        result={"env_saved": True},
        observations=f"Saved validation config for: {', '.join(env_config.keys())}"
        + (f" ({len(notes)} command(s) sanitized)" if notes else ""),
        context_updates={"env_config": existing},
    )


async def action_log_validation_notes(step_input: StepInput) -> StepOutput:
    """Save non-blocking validation issues as mission notes.

    Reads validation_results from context, filters for non-passing
    non-required checks, and saves them as mission notes.
    """
    effects = step_input.effects
    results = step_input.context.get("validation_results", [])

    issues = [
        r
        for r in results
        if isinstance(r, dict)
        and not r.get("passed", True)
        and not r.get("required", False)
    ]

    if not issues or not effects:
        return StepOutput(
            result={"notes_logged": 0},
            observations="No non-blocking issues to log",
        )

    # Format issues into a note
    lines = ["Validation issues (non-blocking):"]
    for issue in issues:
        lines.append(f"  - {issue.get('name', '?')}: {issue.get('stderr', '')[:100]}")

    note_content = "\n".join(lines)

    # Tag the target file so _filter_notes_for_file surfaces these next time
    # the file is touched — the channel was WRITE-ONLY until 2026-08-02
    # (notes written, persisted, never selected into any prompt).
    target = str(step_input.context.get("target_file_path") or "").strip()
    try:
        await effects.push_note(
            content=note_content,
            category="lint_warning",
            tags=["lint", "non_blocking"] + ([target] if target else []),
        )
    except Exception as e:
        logger.warning("Failed to save validation notes: %s", e)

    return StepOutput(
        result={"notes_logged": len(issues)},
        observations=f"Logged {len(issues)} non-blocking validation issues as notes",
    )


# ── Dependency coverage check ────────────────────────────────────────

# Well-known dependency manifest filenames, in priority order.
# Language-agnostic: the LLM interprets contents, we just locate and read.
_DEP_MANIFEST_NAMES = [
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "Pipfile",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "Gemfile",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "composer.json",
    "pubspec.yaml",
    "mix.exs",
    "Package.swift",
    "deno.json",
    "deno.jsonc",
]

# Source extensions worth scanning for imports now live in agent/languages.py
# (languages.is_source).


def _extract_import_lines(filepath: str, content: str) -> list[str]:
    """Extract import/require/use lines from source code.

    Language-agnostic grep — pulls lines that look like dependency
    declarations. The LLM handles the actual interpretation.
    Language-specific patterns (Rust 'use', Ruby 'require') are gated
    on file extension to avoid false positives from content text.
    """
    lines = []
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("//"):
            continue
        # Python: import X, from X import Y
        if stripped.startswith(("import ", "from ")) and ext in ("py", "pyx", ""):
            lines.append(stripped)
        # JS/TS: import ... from '...', require('...')
        elif ext in ("js", "ts", "jsx", "tsx", "mjs", "cjs", "") and (
            "require(" in stripped
            or (stripped.startswith("import ") and "from" in stripped)
        ):
            lines.append(stripped)
        # Rust: use X, extern crate X (only for .rs files)
        elif ext == "rs" and stripped.startswith(("use ", "extern crate ")):
            lines.append(stripped)
        # Go: import "X"
        elif ext == "go" and stripped.startswith("import "):
            lines.append(stripped)
        # Ruby: require 'X', require_relative 'X', gem 'X' (only for .rb files)
        elif ext in ("rb", "gemspec") and stripped.startswith(
            ("require ", "require_relative ", "gem ")
        ):
            lines.append(stripped)
    return lines


async def action_check_dependency_coverage(step_input: StepInput) -> StepOutput:
    """Check that imports in source files are covered by the dependency manifest.

    Language-agnostic: extracts import lines from all source files, reads
    dependency manifest file(s), and publishes both into context for an
    inference step to analyze. Does NOT do the analysis itself.

    Reads:
        context.project_manifest — {filepath: signature} from scan_project
        params.working_directory or input.working_directory

    Publishes:
        dep_check_imports     — deduplicated import lines grouped by file
        dep_check_manifest    — full text of the dependency manifest file(s)
        dep_check_skipped     — true if no manifest or no source files found
    """
    effects = step_input.effects
    project_manifest = step_input.context.get("project_manifest", {})

    if not effects or not project_manifest:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="No effects or project manifest — skipping dep check",
            context_updates={"dep_check_skipped": True},
        )

    # ── Find dependency manifest files ────────────────────────────
    manifest_files = []
    project_files = set(project_manifest.keys())

    for name in _DEP_MANIFEST_NAMES:
        # Check both root and common subdirectory patterns
        for candidate in project_files:
            basename = os.path.basename(candidate)
            if basename == name:
                manifest_files.append(candidate)

    if not manifest_files:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="No dependency manifest found — skipping dep check",
            context_updates={"dep_check_skipped": True},
        )

    # ── Read manifest file contents ───────────────────────────────
    manifest_contents: dict[str, str] = {}
    for mf in manifest_files:
        try:
            fc = await effects.read_file(mf)
            if fc.exists:
                manifest_contents[mf] = fc.content
        except Exception as e:
            logger.warning("Could not read manifest %s: %s", mf, e)

    if not manifest_contents:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="Could not read any manifest files",
            context_updates={"dep_check_skipped": True},
        )

    # ── Extract import lines from source files ────────────────────
    import_map: dict[str, list[str]] = {}
    for filepath in sorted(project_files):
        ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
        if not languages.is_source(ext):
            continue
        try:
            fc = await effects.read_file(filepath)
            if fc.exists and fc.content:
                imports = _extract_import_lines(filepath, fc.content)
                if imports:
                    import_map[filepath] = imports
        except Exception as e:
            logger.debug("Could not read %s for import scan: %s", filepath, e)

    if not import_map:
        return StepOutput(
            result={"dep_check_skipped": True},
            observations="No source files with imports found",
            context_updates={"dep_check_skipped": True},
        )

    # ── Format for prompt injection ───────────────────────────────
    import_lines = []
    for filepath, imports in import_map.items():
        # Test-tree files get a visible label so the coverage checker can
        # apply the dev-dependency rule (see quality_gate/check_deps):
        # a package imported ONLY by tests belongs in a dev dependency
        # group, never in the runtime `dependencies` list. Without the
        # label, an authored test's `import pytest` reads as a project
        # import and the "fix" poisons the manifest (deepseek 2026-08-22:
        # a text-adventure game shipped requiring pytest at install time).
        _parts = filepath.replace("\\", "/").split("/")
        _is_test = "tests" in _parts[:-1] or _parts[-1].startswith("test_")
        _label = " (TEST FILE — dev dependency scope)" if _is_test else ""
        import_lines.append(f"--- {filepath}{_label} ---")
        for imp in imports:
            import_lines.append(f"  {imp}")
    imports_text = "\n".join(import_lines)

    manifest_text_parts = []
    for mf, content in manifest_contents.items():
        manifest_text_parts.append(f"--- {mf} ---")
        manifest_text_parts.append(content)
    manifest_text = "\n".join(manifest_text_parts)

    # IS THE MANIFEST A DECLARATION SET AT ALL? Asked deterministically, here,
    # because the rung below asks an LLM to read the manifest TEXT and answer
    # "is pytest covered?" — and a manifest can answer yes to that while being
    # unusable. `pytest = "^7.4"` folded into [project.optional-dependencies]
    # contains the substring the LLM is looking for, parses as TOML, satisfies
    # the artifact's own `tomllib.load` syntax check, and stops `uv` dead.
    #
    # This is the write floor's blind spot by design: scaffold_parse_error
    # stands down when the file on disk is ALREADY incoherent, so that a broken
    # manifest never becomes unfixable. Standing down must not also mean
    # ceasing to notice — otherwise a repair that adds the right line without
    # removing the wrong one passes every check and the mission completes over
    # a manifest no toolchain can install.
    #
    # Costs no inference. Bounded like any gate failure: it files ONE goal by
    # signature, and the reopen/attempt ceilings end it.
    from agent.actions.file_ops_actions import _pyproject_defects

    # ALL of them, not the first. Reporting one defect at a time costs a repair
    # round each and invites a fix for one defect — live, the artifact carried a
    # non-PEP-508 extras entry AND a [tool.poetry] table simultaneously, against
    # a reopen ceiling of 3. No path prefix either: the message already names the
    # file, and adding one shipped "pyproject.toml: pyproject.toml
    # optional-dependencies…" to a model as its brief.
    manifest_defects = [
        err
        for mf, content in manifest_contents.items()
        for err in _pyproject_defects(mf, content)
    ]

    return StepOutput(
        result={"dep_check_skipped": False, "files_scanned": len(import_map)},
        observations=f"Extracted imports from {len(import_map)} files, "
        f"found {len(manifest_contents)} manifest(s)"
        + (f"; {len(manifest_defects)} incoherent" if manifest_defects else ""),
        context_updates={
            "dep_check_imports": imports_text,
            "dep_check_manifest": manifest_text,
            "dep_manifest_defects": manifest_defects,
            "dep_check_skipped": False,
        },
    )


# ── the dependency CLAIM, checked deterministically ───────────────────
#
# WHY THIS IS SEPARATE FROM check_dependency_coverage ABOVE: that one gathers
# evidence and hands it to an LLM to interpret, and it runs in quality_gate —
# i.e. AFTER the code is written, and only as a gate. This one asks the much
# narrower question the step that AUTHORS the manifest should be able to answer
# about its own output: does anything the code imports fail to appear in the
# manifest at all?
#
# The failure it exists for: a plan_setup run emitted `requires = []` alongside
# the claim "PyYAML is stdlib". Both halves were rendered as FILE CONTENT, so
# nothing could validate either — the claim was smuggled inside a config file.
# `yaml` is not in sys.stdlib_module_names, and this is a fact, not an opinion,
# so no inference call is needed to catch it.
#
# ADVISORY by design. Import-name to distribution-name is genuinely ambiguous
# (`import yaml` <- PyYAML, `import bs4` <- beautifulsoup4), so this normalizes
# both sides and accepts a substring match in either direction. That resolves
# PyYAML/yaml and python-dateutil/dateutil, and still misses bs4/beautifulsoup4.
# A miss here is a false alarm on a note, never a blocked phase.

_LOCAL_IMPORT_HINTS = ("src", "lib", "app", "tests", "test")


def _declared_names(manifest_text: str) -> set[str]:
    """Every identifier-ish token in the dependency manifest, normalized."""
    return {
        re.sub(r"[^a-z0-9]", "", tok.lower())
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9._-]{1,}", manifest_text or "")
    } - {""}


def _undeclared_imports(
    sources: dict[str, str], manifest_text: str, local_stems: set[str]
) -> list[str]:
    """Third-party module names imported by the code but absent from the manifest.

    Python only — ``sys.stdlib_module_names`` is what makes the check exact,
    and there is no equivalent for the other languages, so callers skip them
    rather than guess.
    """
    declared = _declared_names(manifest_text)
    stdlib = set(sys.stdlib_module_names)
    missing: set[str] = set()
    for path, text in sources.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(text or "")
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 is an explicit relative import — always local.
                if node.level or not node.module:
                    continue
                roots = [node.module.split(".")[0]]
            else:
                continue
            for root in roots:
                norm = re.sub(r"[^a-z0-9]", "", root.lower())
                if not norm or root in stdlib or root in local_stems:
                    continue
                if root in _LOCAL_IMPORT_HINTS:
                    continue
                if any(norm in d or d in norm for d in declared):
                    continue
                missing.add(root)
    return sorted(missing)


# Import name -> distribution name where the two differ. Only the cases that
# actually recur; everything else falls back to the import name, which is right
# far more often than not. A wrong guess is corrected by the install step
# failing with the name in its message.
_IMPORT_TO_DISTRIBUTION = {
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "serial": "pyserial",
    "docx": "python-docx",
    "fitz": "PyMuPDF",
    "OpenSSL": "pyOpenSSL",
    "attr": "attrs",
    "jwt": "PyJWT",
}


async def _declare_dependencies(effects, paths: list, missing: list) -> list:
    """Add ``missing`` to a pyproject.toml [project] dependencies list.

    Returns the distribution names written, or [] when there is nothing this
    can safely edit — no pyproject, no [project] table, or a dependencies key
    already present (which means the manifest has an opinion and this must not
    overwrite it).
    """
    target = next((p for p in paths if os.path.basename(p) == "pyproject.toml"), None)
    if not target or effects is None:
        return []
    try:
        fc = await effects.read_file(target)
        text = getattr(fc, "content", "") or ""
    except Exception:  # noqa: BLE001
        return []
    if not text.strip() or "[project]" not in text:
        return []
    # An existing dependencies key is a STATEMENT. Appending to it needs a TOML
    # parse this action deliberately does not do, and silently rewriting a
    # populated list is how a correct manifest gets clobbered.
    for line in text.splitlines():
        if line.strip().startswith("dependencies"):
            return []
    dists = [_IMPORT_TO_DISTRIBUTION.get(m, m) for m in missing]
    body = "".join(f'    "{d}",\n' for d in dists)
    block = f"dependencies = [\n{body}]\n"
    out, inserted = [], False
    for line in text.splitlines(keepends=True):
        out.append(line)
        if not inserted and line.strip() == "[project]":
            out.append(block)
            inserted = True
    if not inserted:
        return []
    try:
        await effects.write_file(target, "".join(out))
    except Exception:  # noqa: BLE001 — a failed repair stays advisory
        return []
    logger.warning("📦 declared %s in %s", ", ".join(dists), target)
    return dists


async def action_check_declared_dependencies(step_input: StepInput) -> StepOutput:
    """Advisory: report imports the dependency manifest does not account for.

    Reads context.project_manifest for the file list; skips cleanly when there
    is no manifest, no python sources, or nothing to say. Publishes
    ``undeclared_dependencies`` and pushes at most ONE note per mission, so a
    re-run of project_ops cannot spam the record.
    """
    effects = step_input.effects
    project_manifest = step_input.context.get("project_manifest") or {}
    if not effects or not project_manifest:
        return StepOutput(
            result={"dep_claim_skipped": True},
            observations="No effects or project manifest — dependency claim not checked",
            context_updates={"undeclared_dependencies": []},
        )

    paths = list(project_manifest.keys())
    manifest_text = ""
    for p in paths:
        if os.path.basename(p) in _DEP_MANIFEST_NAMES:
            try:
                fc = await effects.read_file(p)
                if getattr(fc, "exists", False):
                    manifest_text += (getattr(fc, "content", "") or "") + "\n"
            except Exception:  # noqa: BLE001 — unreadable manifest = nothing to check
                continue
    if not manifest_text.strip():
        return StepOutput(
            result={"dep_claim_skipped": True},
            observations="No dependency manifest — nothing to cross-check",
            context_updates={"undeclared_dependencies": []},
        )

    sources: dict[str, str] = {}
    for p in paths:
        if not p.endswith(".py"):
            continue
        try:
            fc = await effects.read_file(p)
            if getattr(fc, "exists", False):
                sources[p] = getattr(fc, "content", "") or ""
        except Exception:  # noqa: BLE001
            continue
    if not sources:
        return StepOutput(
            result={"dep_claim_skipped": True},
            observations="No python sources — dependency claim not checked",
            context_updates={"undeclared_dependencies": []},
        )

    local_stems = {os.path.basename(p)[:-3] for p in sources}
    local_stems |= {p.split("/")[0] for p in paths if "/" in p}
    missing = _undeclared_imports(sources, manifest_text, local_stems)
    if not missing:
        return StepOutput(
            result={"dep_claim_skipped": False, "undeclared_count": 0},
            observations=f"Dependency claim consistent across {len(sources)} file(s)",
            context_updates={"undeclared_dependencies": []},
        )

    # DECLARE IT, do not merely mention it. This was advisory because
    # import-name -> distribution-name is genuinely ambiguous, and that
    # reasoning was sound about the MAPPING and wrong about the COST. Measured
    # 2026-08-14: an arm imported `yaml` with no dependencies block, this check
    # fired correctly, the note said exactly what was wrong — and nothing read
    # it. `undeclared_dependencies` had no consumer anywhere in agent/ or
    # flows/, and the install step downstream derives its command FROM the
    # manifest, so it faithfully installed nothing. The model then diagnosed
    # the true root cause twice ("Declare PyYAML as a project dependency in
    # pyproject.toml"), was unheard, and degraded into moving the import
    # between files for nine cycles until the 2h wall.
    #
    # The ambiguity is real but its failure mode is BENIGN and LOUD: a wrong
    # distribution name fails at `uv pip install`, in the step immediately
    # after this one, with the name in the error. Silence fails quietly and
    # forever. So: write the declaration, prefer the known alias, and let the
    # installer be the judge.
    written = await _declare_dependencies(effects, paths, missing)

    summary = (
        ("dependency claim REPAIRED: declared " + ", ".join(written) + " — ")
        if written
        else "dependency claim unverified: "
    ) + (
        "the code imports "
        + ", ".join(missing)
        + " but the dependency manifest did not mention "
        + ("it" if len(missing) == 1 else "them")
        + (
            ". The declaration was added; the install step will report a wrong "
            "distribution name by failing on it."
            if written
            else ". Either add the distribution(s) or confirm the import is "
            "provided another way — 'it is in the standard library' is "
            "checkable and these are not in it."
        )
    )
    logger.warning("📦 %s", summary)
    mission = step_input.context.get("mission")
    if mission is None and effects is not None:
        try:
            mission = await effects.load_mission()
        except Exception:  # noqa: BLE001 — advisory only
            mission = None
    if mission is not None:
        from agent.persistence.models import NoteRecord

        marker = "dependency claim unverified"
        already = any(
            marker in (getattr(n, "content", "") or "")
            for n in (getattr(mission, "notes", []) or [])
        )
        if not already:
            mission.notes.append(
                NoteRecord(
                    content=summary[:600],
                    category="failure_analysis",
                    tags=["dependency_claim"],
                    source_flow="project_ops",
                )
            )
            try:
                await effects.save_mission(mission)
            except Exception:  # noqa: BLE001
                pass
    return StepOutput(
        result={"dep_claim_skipped": False, "undeclared_count": len(missing)},
        observations=summary,
        context_updates={"undeclared_dependencies": missing},
    )


async def action_parse_dep_check_result(step_input: StepInput) -> StepOutput:
    """Parse the LLM's dependency coverage analysis.

    Reads context.inference_response (JSON from the check_deps prompt),
    determines if there are missing dependencies, and publishes structured
    results that the quality gate summarizer can act on.

    Expected LLM output format:
    {
        "missing_dependencies": ["pyyaml", "requests"],
        "details": [
            {"import": "yaml", "package": "pyyaml", "file": "loader.py"},
            ...
        ],
        "install_command": "uv add pyyaml requests"
    }
    or: {"missing_dependencies": []}
    """

    raw = step_input.context.get("inference_response", "")

    # THE DETERMINISTIC VERDICT OUTRANKS THE READ ONE. gather_dep_info already
    # decided whether each manifest is a usable declaration set; an LLM's
    # "pytest is covered" cannot overrule "this file declares nothing", and a
    # missing/unparseable LLM answer cannot suppress it either. Checked FIRST,
    # ahead of every fail-open below, because those are the paths a broken
    # manifest would otherwise slip through.
    defects = list(step_input.context.get("dep_manifest_defects") or [])
    if defects:
        # EVERY defect in the reason, numbered. The reason becomes the goal
        # description, which becomes the diagnosis brief — it is the only
        # channel to the model, so naming one defect while withholding another
        # spends a repair round per defect and re-reports the leftover as new.
        detail = (
            defects[0]
            if len(defects) == 1
            else " ".join(f"({i}) {d}" for i, d in enumerate(defects, 1))
        )
        return StepOutput(
            result={"deps_ok": False, "missing_count": len(defects)},
            observations="\n".join(
                ["Manifest is not a valid declaration set:", *defects]
            ),
            context_updates={
                "dep_coverage_result": {"missing_dependencies": [], "defects": defects},
                "dep_coverage_issues": defects,
                "gate_failure_reason": (
                    f"manifest is not a valid declaration set "
                    f"({len(defects)} defect(s)) — {detail}"
                ),
            },
        )

    # Parse JSON response
    from agent.llm_json import parse_llm_json

    result_data = None
    if isinstance(raw, str):
        result_data = parse_llm_json(raw)

    if not result_data or not isinstance(result_data, dict):
        return StepOutput(
            result={"deps_ok": True},
            observations="Could not parse dep check response — assuming OK",
            context_updates={"dep_coverage_result": {"missing_dependencies": []}},
        )

    missing = result_data.get("missing_dependencies", [])
    # Dev-scoped misses (packages imported only by test files — the gatherer
    # labels those and the prompt routes them here) still fail the gate, but
    # the filed fix names a DEV dependency group. Runtime `dependencies` is
    # an install-time contract with every consumer of the artifact; pytest
    # does not belong in it because a test imports it.
    missing_dev = result_data.get("missing_dev_dependencies", [])
    if not missing and not missing_dev:
        return StepOutput(
            result={"deps_ok": True},
            observations="All dependencies are declared in the manifest",
            context_updates={"dep_coverage_result": result_data},
        )

    # Missing deps found — format for quality gate failure
    details = result_data.get("details", [])
    install_cmd = result_data.get("install_command", "")

    issue_lines = []
    if missing:
        issue_lines.append(f"Missing dependencies: {', '.join(missing)}")
    if missing_dev:
        issue_lines.append(
            f"Missing DEV dependencies (test-only imports): "
            f"{', '.join(missing_dev)} — declare in a dev dependency group "
            f"(e.g. [dependency-groups] dev / uv add --dev), NOT in the "
            f"runtime dependencies list"
        )
    for d in details[:10]:
        issue_lines.append(
            f"  {d.get('file', '?')}: imports '{d.get('import', '?')}' "
            f"→ package '{d.get('package', '?')}'"
        )
    if install_cmd:
        issue_lines.append(f"  Fix: {install_cmd}")

    return StepOutput(
        result={
            "deps_ok": False,
            "missing_count": len(missing) + len(missing_dev),
        },
        observations="\n".join(issue_lines),
        context_updates={
            "dep_coverage_result": result_data,
            # Merge into validation_results so summarize sees it
            "dep_coverage_issues": issue_lines,
            # LOAD-BEARING (2026-08-10). This branch routes straight to
            # gate_fail, skipping the rung that builds quality_results — so
            # the harvester downstream sees NO findings and, before this,
            # completed the mission on a failed gate. Naming the reason here
            # is what lets it file a SPECIFIC goal ("declare pyyaml") instead
            # of a generic "the gate failed".
            "gate_failure_reason": (
                "undeclared dependencies — "
                + ", ".join(
                    [*missing[:6]]
                    + [f"{m} (dev group — test-only)" for m in missing_dev[:6]]
                )
                + (" are" if len(missing) + len(missing_dev) != 1 else " is")
                + " imported but not in the manifest"
                + (f"; fix: {install_cmd}" if install_cmd else "")
            ),
        },
    )


# ── Generic LLM JSON parsing action ──────────────────────────────────


async def action_parse_inference_json(step_input: StepInput) -> StepOutput:
    """Parse JSON from the latest inference response and publish fields to context.

    Reads inference_response from context, parses it via parse_llm_json,
    and publishes each top-level key as a separate context key. This lets
    downstream resolver conditions check clean typed values instead of
    string-matching raw LLM text.

    Used by interact/evaluate_outcome to extract goal_met as a boolean.

    Params:
        source_key: Context key to read raw text from (default: "inference_response")
        required_fields: List of field names that must be present (default: [])

    Publishes: each top-level key from the parsed JSON
    Result: parsed=True/False, plus each parsed field
    """
    from agent.llm_json import parse_llm_json

    source_key = step_input.params.get("source_key", "inference_response")
    required_fields = step_input.params.get("required_fields", [])

    raw = step_input.context.get(source_key, "")
    if not raw:
        return StepOutput(
            result={"parsed": False},
            observations=f"No content in {source_key}",
        )

    data = parse_llm_json(raw)
    if not isinstance(data, dict):
        return StepOutput(
            result={"parsed": False},
            observations=f"Could not parse JSON from {source_key}",
        )

    # Check required fields
    missing = [f for f in required_fields if f not in data]
    if missing:
        return StepOutput(
            result={"parsed": False, "missing_fields": missing},
            observations=f"Parsed JSON missing required fields: {missing}",
        )

    # Publish each field to context AND result
    context_updates = {}
    result = {"parsed": True}
    for key, value in data.items():
        context_updates[key] = value
        result[key] = value

    return StepOutput(
        result=result,
        observations=f"Parsed {len(data)} fields from {source_key}",
        context_updates=context_updates,
    )


# ── Deterministic evaluation ─────────────────────────────────────────

# Error patterns that indicate failure even when exit code is 0 — consolidated
# into the shared liveness predicate (oracle_actions) so this deterministic eval
# and the oracle rungs scan for the SAME error-strings (was a duplicated list).
from agent.actions.oracle_actions import _FAILURE_PATTERNS  # noqa: E402


async def action_evaluate_deterministic_result(step_input: StepInput) -> StepOutput:
    """Evaluate a deterministic run_commands result without inference.

    Checks exit code (via all_passed) and scans terminal_output for
    error patterns. Publishes goal_met, summary, and headline — same
    keys as the inference-based parse_evaluation step, so downstream
    routing and report compilation work identically.

    Context required: terminal_output, all_passed
    Inputs (optional): goal_id — used to read the goal's should-raise contract.
    Publishes: goal_met, summary, headline
    """
    terminal_output = step_input.context.get("terminal_output", "")
    all_passed = step_input.context.get("all_passed", False)

    # Scan for error patterns in output
    found_errors = []
    for pattern in _FAILURE_PATTERNS:
        if pattern in terminal_output:
            found_errors.append(pattern.rstrip(":"))
    # Pytest error idioms — NOT Python tracebacks, so the shared pattern list
    # misses them: a fixture/setup error run ("fixture 'mocker' not found",
    # the ==== ERRORS ==== bar, collection errors) was graded goal_met when
    # the exit-code capture also missed it (the b5d fsspec false success —
    # the mission completed confidently while the grader still failed).
    for pat, label in (
        ("= ERRORS =", "pytest ERRORS"),
        ("' not found\n", "pytest missing fixture"),
        ("errors during collection", "pytest collection error"),
        ("ERROR at setup", "pytest setup error"),
        ("INTERNALERROR", "pytest internal error"),
    ):
        if pat in terminal_output and label not in found_errors:
            found_errors.append(label)
    if "fixture '" in terminal_output and "not found" in terminal_output:
        if "pytest missing fixture" not in found_errors:
            found_errors.append("pytest missing fixture")

    # Should-raise contract (opt-in — GoalRecord.expected_error, set by the
    # diagnose CONCLUDE). When a fix's success criterion is that some input the
    # code used to accept must now be REJECTED by raising, the named exception
    # is the PASS signal, not a regression. Without this, a deliberately-
    # propagated exception trips the blanket _FAILURE_PATTERNS fail — the flask
    # should-raise false-fail. Scoped to THIS goal via goal_id: a normal goal
    # (empty expected_error) is completely unaffected.
    expected_error = ""
    goal_id = str((step_input.inputs or {}).get("goal_id", "") or "")
    effects = step_input.effects
    if goal_id and effects is not None:
        try:
            mission = await effects.load_mission()
            goal = next(
                (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id),
                None,
            )
            if goal is not None:
                expected_error = str(getattr(goal, "expected_error", "") or "").strip()
        except Exception:
            expected_error = ""

    # Match the exception WITH its colon (e.g. "ValueError:") — this is exactly
    # the message-bearing form _FAILURE_PATTERNS blanket-fails on, i.e. an
    # exception that actually fired and propagated. It deliberately does NOT
    # match pytest's "DID NOT RAISE <class 'ValueError'>" (no colon), so a
    # should-raise test that failed because nothing raised is still correctly
    # graded a failure.
    _exc = expected_error.rstrip(":")
    raised = bool(_exc) and f"{_exc}:" in terminal_output
    if raised:
        # The required exception fired — drop it (and the traceback a propagated
        # exception prints) from the failure signals so the contract isn't read
        # as a bug. Other, unrelated errors stay and still fail the goal.
        found_errors = [
            e
            for e in found_errors
            if e != _exc and e != "Traceback (most recent call last)"
        ]

    # Determine goal_met
    if raised and not found_errors:
        # Should-raise contract met: the required exception was raised and
        # nothing ELSE went wrong. A deliberately-propagated exception yields a
        # non-zero exit + traceback tokens — both expected here — so this
        # supersedes the exit-code / error-scan checks below.
        goal_met = True
        summary = f"Should-raise contract met: {expected_error} raised as required."
    elif not all_passed:
        goal_met = False
        summary = "Command exited with non-zero status."
        if found_errors:
            summary += f" Errors detected: {', '.join(found_errors)}"
        elif terminal_output:
            # Show tail of output for context
            tail = terminal_output.strip().splitlines()[-3:]
            summary += " Output tail: " + " | ".join(tail)
    elif found_errors:
        goal_met = False
        summary = (
            f"Command exited 0 but output contains errors: "
            f"{', '.join(found_errors)}"
        )
    elif not terminal_output.strip():
        # Exit 0 but no output at all — could be fine (silent success)
        # or could mean the command didn't actually run. Accept it.
        goal_met = True
        summary = "Command completed with exit code 0 (no output)."
    else:
        goal_met = True
        summary = "Command completed successfully with exit code 0."

    # Derive a compact headline — parse_evaluation's inference path
    # produces one via the eval JSON, but the deterministic path never
    # had this (b75 regression: 40 reports with empty headlines blocking
    # the before/after regression diffing across retry cycles).
    #
    # For failures, extract the first error-looking line from the
    # terminal tail — that's the signal most useful for regression
    # detection. For successes, use a fixed short string.
    if goal_met:
        headline = "Command ran to success exit 0"
    else:
        headline = _derive_failure_headline(terminal_output, found_errors)

    return StepOutput(
        result={"goal_met": goal_met},
        observations=summary,
        context_updates={
            "goal_met": goal_met,
            "summary": summary,
            "headline": headline,
        },
    )


def _derive_failure_headline(terminal_output: str, found_errors: list[str]) -> str:
    """Extract a ~10-15 word headline from failure evidence.

    Prefers the last exception line if present (e.g.
    ``TypeError: non-default argument follows default argument``),
    falling back to the first detected error pattern + tail line,
    then to a generic string. The goal is a stable signal that
    changes when the underlying failure changes — the regression-
    detection machinery compares consecutive reports' headlines.
    """
    lines = [ln.strip() for ln in terminal_output.splitlines() if ln.strip()]
    # Look for a Python exception line: something like
    # "ModuleNotFoundError: No module named 'foo'"
    for ln in reversed(lines[-15:]):
        # Heuristic: CamelCase word starting a line followed by ":"
        # covers Python's stdlib + most common exceptions.
        if ":" in ln and ln[0:1].isupper():
            head, _, tail = ln.partition(":")
            if head.isalpha() and len(head) <= 40 and len(ln) <= 140:
                return ln[:140]
    # Fallback 1: use found_errors + last output line
    if found_errors and lines:
        return (f"{found_errors[0]} — {lines[-1][:90]}")[:140]
    # Fallback 2: just the tail
    if lines:
        return lines[-1][:140]
    return "Command failed with no diagnostic output"


# ══════════════════════════════════════════════════════════════════════
# Per-goal grounded acceptance checks (ops definition-of-done port)
# ══════════════════════════════════════════════════════════════════════
# A functional goal's "definition of done" was the goal description alone,
# judged by the interact evaluator's goal_met. These three actions add the
# ops-side machinery per GOAL: derive shell acceptance checks ONCE, grounded
# in the explored session (interact's derive_acceptance step), store them
# tighten-only on the goal, run them each verification pass, and fold the
# result into the evaluator's verdict as a deterministic TIGHTENER — the
# checks can veto a credulous goal_met, never certify a goal on their own,
# and zero checks means the evaluator judges alone (no vacuous verification).


async def action_gate_goal_acceptance(step_input: StepInput) -> StepOutput:
    """LOAD the goal's stored acceptance checks (no pre-pass derivation).

    Runs before the evaluator on every verification pass: publishes the
    stored checks so run_acceptance_checks enforces them as a regression
    guard, and reports whether the goal still needs its check DERIVED
    (functional/quality AND not yet grounded). Derivation itself happens
    only on the SUCCESS branch, after a genuine pass (see arm_acceptance /
    store_goal_acceptance) — this action never triggers it.

    Inputs: goal_id.  Result: has_checks, needs_derive.
    Publishes: mission, goal_acceptance_checks, acceptance_needs_derive.
    """
    effects = step_input.effects
    goal_id = str(step_input.inputs.get("goal_id", "") or "")
    try:
        mission = await effects.load_mission() if effects else None
    except Exception:
        mission = None
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None or getattr(goal, "type", "") not in ("functional", "quality"):
        return StepOutput(
            result={"has_checks": False, "needs_derive": False},
            observations="goal-acceptance: no eligible goal — evaluator judges alone",
            context_updates={
                "goal_acceptance_checks": [],
                "acceptance_needs_derive": False,
            },
        )
    checks = list(getattr(goal, "acceptance_checks", None) or [])
    # ── ATTEMPT CEILING on an authored test ──────────────────────────
    # The quarantine in action_reconcile_acceptance only fires when the
    # evaluator returns goal_met=true WHILE the check fails. A test that is
    # unsatisfiable AND sits on a goal whose behaviour also fails never
    # reaches that step at all: goal_met=false ends the round, the conflict
    # counter never moves, and the goal loops until the wall.
    #
    # Observed live (gpt-oss-medium, 2026-08-09): a well-scoped victory-screen
    # test whose scenario could not win the fight (it never equipped the boss
    # weakness) sat at 8 failed attempts and 3 escalations with conflicts
    # stuck at 2, while four other goals waited at zero. That is the
    # 51-retest immortalization returning through the door the quarantine
    # does not cover.
    #
    # So bound it on EFFORT as well as contradiction: past the ceiling the
    # authored check goes advisory and the dispute is raised, exactly as the
    # quarantine would have. Nothing is deleted; the test stays readable.
    if getattr(goal, "authored_test", None) and checks:
        attempts = len(getattr(goal, "failed_attempts", None) or [])
        armed = [
            c
            for c in checks
            if c.get("source") == "authored" and c.get("required", True)
        ]
        if armed and attempts >= _AUTHORED_ATTEMPT_CEILING:
            from agent.persistence.models import WarningRecord

            for c in armed:
                c["required"] = False
            mission.pending_warnings.append(
                WarningRecord(
                    kind="authored_test_unsatisfied",
                    subject=str(
                        getattr(goal, "authored_test", {}).get("path") or goal.id
                    )[:120],
                    evidence=(
                        f"The authored regression test for "
                        f"'{goal.description[:70]}' has stayed RED across "
                        f"{attempts} fix attempts. The behaviour never passed "
                        f"either, so the contradiction quarantine could not "
                        f"fire. Either the fix is genuinely out of reach, or "
                        f"the test cannot reach the state it asserts (e.g. it "
                        f"drives a scenario that cannot succeed)."
                    ),
                    prescribed_fix=(
                        "Read the test and decide which: repair the code, or "
                        "correct the test's scenario. It has been demoted to "
                        "advisory so the goal is no longer blocked by it."
                    ),
                    source_flow="gate_goal_acceptance",
                )
            )
            logger.warning(
                "goal-acceptance: authored test DEMOTED after %d fix attempts "
                "without passing ('%s') — goal was blocked, not contradicted",
                attempts,
                goal.description[:50],
            )
            if effects:
                try:
                    await effects.save_mission(mission)
                except Exception:  # noqa: BLE001 - never break the rung
                    logger.debug("goal-acceptance: save failed", exc_info=True)
            checks = list(goal.acceptance_checks or [])
    # An AUTHORED test outranks derivation (v13): it was probed red against the
    # broken code, so deriving a replay check on top of it would only re-add the
    # brittle layer it replaced. Gated on authored_test rather than on
    # acceptance_grounded because reconcile RESETS grounded on any disarm — an
    # unrelated derived check wearing out would otherwise re-arm derivation on a
    # goal that already has a real test.
    needs = not bool(getattr(goal, "acceptance_grounded", False)) and not bool(
        getattr(goal, "authored_test", None)
    )
    return StepOutput(
        result={"has_checks": bool(checks), "needs_derive": needs},
        observations=(
            f"goal-acceptance: {len(checks)} stored check(s)"
            + (" — will derive after a pass" if needs else "")
        ),
        context_updates={
            "mission": mission,
            "goal_acceptance_checks": checks,
            "acceptance_needs_derive": needs,
        },
    )


async def action_store_goal_acceptance(step_input: StepInput) -> StepOutput:
    """VALIDATE the derived acceptance checks against the just-passed state,
    then merge the survivors onto the goal (TIGHTEN-ONLY union by command).

    Runs only on the SUCCESS branch (the goal just passed goal_met AND
    acceptance_ok), so a correct check MUST exit 0 against the current
    working dir right now. Each candidate is probed via the SAME runner the
    regression pass uses (format_completion_criteria + run_validation_checks,
    so identical /bin/sh -c wrapping); any that does not pass NOW — a
    SyntaxError command, a missing binary, a mis-grounded grep — is dropped
    and logged. This is why no error-vs-fail classification is needed: a
    broken/mis-grounded check simply fails the known-good state and never
    gets armed. One-shot: acceptance_grounded is set even when all
    candidates are dropped (the evaluator judges alone thereafter — never
    vacuous), so we never re-pay the derivation inference.

    Context: mission, inference_response.  Inputs: goal_id.
    Publishes: mission, goal_acceptance_checks.
    """
    from agent.actions.operations_actions import _parse_completion_criteria
    from agent.actions.refinement_actions import action_run_validation_checks
    from agent.formatters import format_completion_criteria

    effects = step_input.effects
    mission = step_input.context.get("mission")
    goal_id = str(step_input.inputs.get("goal_id", "") or "")
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None:
        return StepOutput(
            result={"criteria_count": 0},
            observations="goal-acceptance: no goal",
            context_updates={"goal_acceptance_checks": []},
        )
    candidates = _parse_completion_criteria(
        str(step_input.context.get("inference_response", ""))
    )

    # Validate-on-create: probe each candidate against the known-good state.
    survivors = list(candidates)
    dropped = 0
    if candidates and effects:
        probe_strategy = format_completion_criteria(
            {"source": [{**c, "required": False} for c in candidates]}, {}
        )
        probe = await action_run_validation_checks(
            StepInput(
                effects=effects,
                context={"validation_strategy": probe_strategy},
                params={"max_checks": max(len(candidates), 1)},
            )
        )
        results = probe.context_updates.get("validation_results") or []
        survivors = []
        for i, cand in enumerate(candidates):
            row = results[i] if i < len(results) else None
            if row is not None and row.get("passed"):
                survivors.append(cand)
            else:
                dropped += 1
                logger.warning(
                    "goal-acceptance: dropped non-passing check (rc=%s): %s | %s",
                    (row or {}).get("return_code"),
                    str(cand.get("command", ""))[:120],
                    str((row or {}).get("stderr", ""))[:200],
                )

    merged = list(getattr(goal, "acceptance_checks", None) or [])
    seen = {c.get("command") for c in merged}
    added = 0
    for c in survivors:
        if c["command"] not in seen:
            merged.append(c)
            seen.add(c["command"])
            added += 1
    goal.acceptance_checks = merged
    goal.acceptance_grounded = True
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"criteria_count": len(merged)},
        observations=(
            f"goal-acceptance: {len(merged)} check(s) "
            f"(+{added} grounded, {dropped} dropped as non-passing)"
        ),
        context_updates={"mission": mission, "goal_acceptance_checks": merged},
    )


_ACCEPTANCE_DISARM_K = 2  # behavior-refutes-check disarm threshold (cf _SHAPE_REFUTE_K)
# An AUTHORED test is never disarmed — at this many contradictions it is
# demoted to advisory and the dispute is raised as a warning. Higher than the
# disarm threshold because a check with a verified negative control deserves
# more benefit of the doubt than one derived from a transcript.
_AUTHORED_QUARANTINE_K = 3
# Failed fix attempts after which an authored test that has NEVER passed goes
# advisory regardless of what the evaluator said. The contradiction quarantine
# above needs goal_met=true to fire; this covers the case where the behaviour
# fails too, which is the only way an authored test can immortalize a goal.
# Four gives the fixer a fair run (the stuck-goal escalation has fired twice
# by then) without letting one goal eat a run's remaining wall.
_AUTHORED_ATTEMPT_CEILING = 4


def _harness_red(output: str) -> bool:
    """True when a red never reached an assertion — a broken test harness.

    The quarantine's whole premise is that a FAILING ASSERTION disagreeing with
    a green behavioural evaluator is evidence the assertion is wrong. A test
    that died in setup asserts nothing, so it is evidence of nothing, and
    letting it advance the conflict counter is how a harness bug retires a
    correct test. Shares its exception set with the arming probe, which now
    refuses to arm these in the first place — this is the backstop for tests
    armed before that gate existed, and for a harness that only breaks later
    (a data file moved, a dependency dropped).
    """
    from agent.actions.authored_test_actions import _environment_red

    return bool(_environment_red(output or ""))


async def action_apply_acceptance_verdict(step_input: StepInput) -> StepOutput:
    """Fold the acceptance-check run into a deterministic verdict for the
    evaluator. acceptance_ok means "no deterministic objection" — with zero
    checks run it is vacuously True and acceptance_summary stays EMPTY (the
    vacuous-verification rule: never render 0 checks as evidence of passing);
    a required failure makes it False, which vetoes the evaluator's goal_met
    in parse_evaluation's resolver.

    Context: validation_results (optional).
    Publishes: acceptance_ok, acceptance_summary.
    """
    from agent.formatters import format_validation_results

    results = list(step_input.context.get("validation_results") or [])
    if not results:
        return StepOutput(
            result={"acceptance_ok": True, "checks_run": 0},
            observations="goal-acceptance: no checks ran — evaluator judges alone",
            context_updates={"acceptance_ok": True, "acceptance_summary": ""},
        )
    ok = all(r.get("passed") for r in results if r.get("required", True))
    summary = format_validation_results({"source": results}, {})
    return StepOutput(
        result={"acceptance_ok": ok, "checks_run": len(results)},
        observations=(
            f"goal-acceptance: {'PASS' if ok else 'FAIL'} ({len(results)} check(s))"
        ),
        context_updates={"acceptance_ok": ok, "acceptance_summary": summary},
    )


async def action_reconcile_acceptance(step_input: StepInput) -> StepOutput:
    """Reached only when the evaluator returned goal_met=true but a required
    acceptance check FAILED (parse_evaluation's middle rule). The behavior
    passed while the stored check objects — the check is REFUTED BY BEHAVIOR (a
    brittle exact-grep an intentional edit broke, or a stateful check run in a
    new context), NOT a real regression (a real regression keeps goal_met=false
    and never routes here). Increment each failing check's per-goal conflict
    counter; disarm (remove from acceptance_checks) any that reach
    _ACCEPTANCE_DISARM_K; recompute the verdict over the survivors. now_ok=True
    (the goal completes) iff every failing check was disarmed this pass — else
    the counter advanced and disarm follows on a later pass. Mirrors the
    shape_refutes suppression and is the SOLE self-healing path for a grounded
    check (which never re-derives), so a false positive cannot immortalize a
    goal.

    Context: mission, validation_results.  Inputs: goal_id.
    Publishes: mission, now_ok, acceptance_ok.
    """
    from agent.persistence.models import NoteRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    goal_id = str(step_input.inputs.get("goal_id", "") or "")
    results = list(step_input.context.get("validation_results") or [])
    goal = next(
        (g for g in getattr(mission, "goals", []) or [] if g.id == goal_id), None
    )
    if goal is None or not results:
        return StepOutput(
            result={"now_ok": False},
            observations="reconcile: no goal/results — keeping the veto",
            context_updates={"now_ok": False, "acceptance_ok": False},
        )

    def _key(row: dict) -> str:
        # run_acceptance_checks renders commands as ["/bin/sh","-c",cmd] via
        # format_completion_criteria, so the row's command is that list; the
        # stored acceptance_checks[].command is the raw cmd (the last element).
        raw = row.get("command")
        return raw[-1] if isinstance(raw, (list, tuple)) and raw else str(raw)

    failed = [r for r in results if r.get("required", True) and not r.get("passed")]
    # AUTHORED tests are exempt from disarm (v13). Disarm exists to wear out
    # mis-grounded REPLAY checks — checks derived from a transcript, never
    # verified against anything. An authored test was probed RED against the
    # broken code and green after the fix, so it has a negative control the
    # evaluator does not; deleting it because two LLM verdicts disagreed would
    # silently destroy the only ground truth on the goal. Quarantine instead
    # (below): demote to advisory and route the contradiction to a human/fixer.
    by_command = {str(c.get("command", "")): c for c in (goal.acceptance_checks or [])}

    def _is_authored(key: str) -> bool:
        return by_command.get(key, {}).get("source") == "authored"

    disarmed: set[str] = set()
    quarantined: set[str] = set()
    advisory: set[str] = set()  # authored + already quarantined = no veto left
    harness: set[str] = set()  # red on its own setup — not a contradiction at all
    for row in failed:
        k = _key(row)
        # A CONTRADICTION IS AN ASSERTION THAT DISAGREES WITH THE EVALUATOR.
        # A test that never reached its assertion contradicts nothing — it is
        # red on its own harness, and counting that as evidence against the
        # test is how three CORRECT tests got disarmed on 2026-08-10. All three
        # `os.chdir`-ed into a temp dir to isolate their writes, so the
        # product's relative `open("world.yaml")` raised FileNotFoundError in
        # setup; the behaviour they asserted was present and passing the whole
        # time (verified after the run — the save DID contain every key they
        # checked). The conflict counter must not advance on those rounds, or
        # a harness bug wears down a test that was right.
        if _is_authored(k) and _harness_red(
            f"{row.get('stdout', '')}\n{row.get('stderr', '')}"
        ):
            harness.add(k)
            continue
        goal.acceptance_conflicts[k] = goal.acceptance_conflicts.get(k, 0) + 1
        if _is_authored(k):
            if by_command.get(k, {}).get("required", True) is False:
                # Already quarantined on an earlier round. It is advisory now:
                # no second warning, and it must not re-acquire the veto.
                advisory.add(k)
            elif goal.acceptance_conflicts[k] >= _AUTHORED_QUARANTINE_K:
                quarantined.add(k)
            continue
        if goal.acceptance_conflicts[k] >= _ACCEPTANCE_DISARM_K:
            disarmed.add(k)

    if harness:
        from agent.persistence.models import WarningRecord

        for k in harness:
            mission.pending_warnings.append(
                WarningRecord(
                    kind="authored_test_harness_broken",
                    subject=str(
                        (getattr(goal, "authored_test", None) or {}).get("path") or k
                    )[:120],
                    evidence=(
                        f"The authored regression test for "
                        f"'{goal.description[:70]}' is red on its own SETUP — "
                        f"it never reaches the assertion, so it neither "
                        f"confirms nor contradicts the behaviour. Most often "
                        f"the test changes the working directory and the "
                        f"program then cannot find its own data files. "
                        f"Command: {k[:200]}."
                    ),
                    prescribed_fix=(
                        "Repair the TEST, not the code: the behaviour it "
                        "asserts has not been measured yet. It keeps its veto "
                        "and its conflict count is unchanged — a harness bug "
                        "is not evidence against the assertion."
                    ),
                    source_flow="reconcile_acceptance",
                )
            )
            logger.warning(
                "reconcile: authored test on '%s' is red on its HARNESS, not "
                "its assertion — conflict NOT counted",
                goal.description[:50],
            )

    if quarantined:
        from agent.persistence.models import WarningRecord

        for k in quarantined:
            check = by_command.get(k, {})
            check["required"] = False
            mission.pending_warnings.append(
                WarningRecord(
                    kind="authored_test_contradicted",
                    # The acceptance-check dict has no `path` — only the
                    # authored_test record does, so this read always fell back
                    # to the raw pytest command and every quarantine warning
                    # was subject-lined with 90 characters of invocation where
                    # a filename belongs.
                    subject=str(
                        (getattr(goal, "authored_test", None) or {}).get("path") or k
                    )[:120],
                    evidence=(
                        f"The authored regression test for "
                        f"'{goal.description[:70]}' has now failed while the "
                        f"behavioural evaluator returned goal_met=true "
                        f"{_AUTHORED_QUARANTINE_K} times. Command: {k[:200]}. "
                        f"Either the test asserts something the code no longer "
                        f"owes, or the behaviour is passing for the wrong "
                        f"reason and the evaluator is being fooled."
                    ),
                    prescribed_fix=(
                        "Read the test and decide which side is wrong: correct "
                        "the code, or correct the test. It has been demoted to "
                        "advisory (it no longer vetoes completion) and moved to "
                        ".agent/quarantine/ so it stays readable without "
                        "shipping in the artifact."
                    ),
                    source_flow="reconcile_acceptance",
                )
            )
            # RELOCATE, don't just demote. A quarantined test left in tests/
            # is harness residue wearing project clothes: it keeps `pytest
            # tests/` red in the deliverable, and downstream scanners treat
            # its imports as project imports — the 2026-08-22 deepseek run's
            # quality gate saw `import pytest` in an abandoned test and
            # "fixed" it by adding pytest to the game's RUNTIME dependencies.
            # .agent/ is already excluded from staging, scans, and judging,
            # and the pending warning above names the new location.
            _qpath = str((getattr(goal, "authored_test", None) or {}).get("path") or "")
            if _qpath:
                try:
                    await effects.makedirs(".agent/quarantine", exist_ok=True)
                    _mv = await effects.run_command(
                        [
                            "mv",
                            _qpath,
                            f".agent/quarantine/{_qpath.rsplit('/', 1)[-1]}",
                        ]
                    )
                    if getattr(_mv, "return_code", 1) != 0:
                        logger.warning(
                            "reconcile: could not relocate quarantined test "
                            "%s (left in place): %s",
                            _qpath,
                            str(getattr(_mv, "stderr", ""))[:200],
                        )
                except Exception as _e:  # noqa: BLE001 — relocation is best-effort
                    logger.warning(
                        "reconcile: quarantine relocation failed for %s: %s",
                        _qpath,
                        _e,
                    )
            logger.warning(
                "reconcile: QUARANTINED authored test on '%s' after %d "
                "behaviour contradictions",
                goal.description[:50],
                _AUTHORED_QUARANTINE_K,
            )

    if disarmed:
        goal.acceptance_checks = [
            c
            for c in (goal.acceptance_checks or [])
            if c.get("command") not in disarmed
        ]
        for k in disarmed:
            goal.acceptance_conflicts.pop(k, None)
            mission.notes.append(
                NoteRecord(
                    content=(
                        f"regression check DISARMED on '{goal.description[:70]}': "
                        f"behavior passed (goal_met) while this check failed "
                        f"{_ACCEPTANCE_DISARM_K}x -> refuted by behavior. "
                        f"Removed: {k[:160]}"
                    ),
                    category="failure_analysis",
                    tags=["regression", "disarm", goal.finding_signature or goal.id],
                    source_flow="reconcile_acceptance",
                )
            )
            logger.info("reconcile: disarmed check on '%s'", goal.description[:50])

        # A DISARMED CHECK MUST BE REPLACED, NOT JUST REMOVED (operator,
        # 2026-08-06 — the deferred "component 3"). Grounding is one-shot
        # (`needs_derive = not grounded`), so without this reset the
        # completion round skips re-derivation and the goal finishes with
        # fewer checks than it earned — in the observed case ZERO, which
        # takes it out of the regression sweep entirely: future edits could
        # silently break it with no auto-reopen, ever. Resetting grounded
        # (and saying so in context for THIS round's arm_acceptance) makes
        # the now_ok completion round — which is a genuine behavioural pass
        # with the transcript in hand — derive a FRESH check grounded in
        # the CURRENT world. Derive-after-pass stays inviolate; the stale
        # assumption is replaced instead of leaving a guard hole.
        goal.acceptance_grounded = False

    # A quarantined authored test is advisory from here on, so it no longer
    # holds the veto — otherwise the goal it was written to certify could never
    # complete again.
    resolved = disarmed | quarantined | advisory
    now_ok = not [r for r in failed if _key(r) not in resolved]
    if effects:
        await effects.save_mission(mission)
    updates: dict = {"mission": mission, "now_ok": now_ok, "acceptance_ok": now_ok}
    # Veto marker (operator, 2026-08-07): this step is only reachable when
    # BEHAVIOUR PASSED and a deterministic replay check failed. When the
    # round still ends failed (check not yet worn out), the report carries
    # acceptance_vetoed so the sweep dispatches a plain RETEST instead of a
    # full diagnosis — the checks were meant as a replay guard ("does the
    # same session still work"), not a testing standard, and a veto is
    # never evidence of a code defect.
    if not now_ok:
        updates["acceptance_vetoed"] = True
    # Re-derivation replaces a disarmed REPLAY check. A goal that already
    # carries an authored test doesn't need one — it has the better guard.
    if disarmed and not getattr(goal, "authored_test", None):
        updates["acceptance_needs_derive"] = True
    return StepOutput(
        result={
            "now_ok": now_ok,
            "disarmed": len(disarmed),
            "quarantined": len(quarantined),
        },
        observations=(
            f"reconcile: {len(disarmed)} disarmed, {len(failed)} failing, "
            f"now_ok={now_ok}"
            + (
                " — grounding reset, fresh check derives on this pass"
                if disarmed and not getattr(goal, "authored_test", None)
                else ""
            )
            + (
                f" — {len(quarantined)} AUTHORED test(s) quarantined "
                "(demoted to advisory, warning raised)"
                if quarantined
                else ""
            )
        ),
        context_updates=updates,
    )
