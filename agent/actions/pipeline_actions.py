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


async def action_persist_validation_env(step_input: StepInput) -> StepOutput:
    """Parse LLM-generated validation config and save to .agent/env.json.

    The inference response should be a JSON object mapping extensions
    to validation commands (syntax, import, lint).
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
        observations=f"Saved validation config for: {', '.join(env_config.keys())}",
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

    try:
        await effects.push_note(
            content=note_content,
            category="lint_warning",
            tags=["lint", "non_blocking"],
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
        import_lines.append(f"--- {filepath} ---")
        for imp in imports:
            import_lines.append(f"  {imp}")
    imports_text = "\n".join(import_lines)

    manifest_text_parts = []
    for mf, content in manifest_contents.items():
        manifest_text_parts.append(f"--- {mf} ---")
        manifest_text_parts.append(content)
    manifest_text = "\n".join(manifest_text_parts)

    return StepOutput(
        result={"dep_check_skipped": False, "files_scanned": len(import_map)},
        observations=f"Extracted imports from {len(import_map)} files, "
        f"found {len(manifest_contents)} manifest(s)",
        context_updates={
            "dep_check_imports": imports_text,
            "dep_check_manifest": manifest_text,
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

    summary = (
        "dependency claim unverified: the code imports "
        + ", ".join(missing)
        + " but the dependency manifest does not mention "
        + ("it" if len(missing) == 1 else "them")
        + ". Either add the distribution(s) or confirm the import is provided "
        "another way — 'it is in the standard library' is checkable and these "
        "are not in it."
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
    if not missing:
        return StepOutput(
            result={"deps_ok": True},
            observations="All dependencies are declared in the manifest",
            context_updates={"dep_coverage_result": result_data},
        )

    # Missing deps found — format for quality gate failure
    details = result_data.get("details", [])
    install_cmd = result_data.get("install_command", "")

    issue_lines = [f"Missing dependencies: {', '.join(missing)}"]
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
            "missing_count": len(missing),
        },
        observations="\n".join(issue_lines),
        context_updates={
            "dep_coverage_result": result_data,
            # Merge into validation_results so summarize sees it
            "dep_coverage_issues": issue_lines,
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
    needs = not bool(getattr(goal, "acceptance_grounded", False))
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
    disarmed: set[str] = set()
    for row in failed:
        k = _key(row)
        goal.acceptance_conflicts[k] = goal.acceptance_conflicts.get(k, 0) + 1
        if goal.acceptance_conflicts[k] >= _ACCEPTANCE_DISARM_K:
            disarmed.add(k)

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

    now_ok = not [r for r in failed if _key(r) not in disarmed]
    if effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"now_ok": now_ok, "disarmed": len(disarmed)},
        observations=(
            f"reconcile: {len(disarmed)} disarmed, {len(failed)} failing, "
            f"now_ok={now_ok}"
        ),
        context_updates={"mission": mission, "now_ok": now_ok, "acceptance_ok": now_ok},
    )
