"""Declarative YAML mission configuration loader.

Loads mission parameters from a YAML file, validates them with Pydantic,
and executes lifecycle commands around mission creation.

Lifecycle:
    1. pre_create  — runs before mission creation (wipe dirs, clean logs)
    2. mission create — creates the mission state
    3. post_create — runs after mission creation (start agent, setup scripts)

Usage:
    uv run ouroboros.py mission create --mission_config game_challenge
    # Bare names resolve in cwd, then missions/ (missions/game_challenge.yaml)

    uv run ouroboros.py mission create --mission_config ./missions/ops_demo.yaml
    # Or an explicit path
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

# ── Durations ─────────────────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"^(?:(?P<d>\d+(?:\.\d+)?)d)?"
    r"(?:(?P<h>\d+(?:\.\d+)?)h)?"
    r"(?:(?P<m>\d+(?:\.\d+)?)m)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?$"
)


def parse_duration(value: float | int | str) -> float:
    """Duration to seconds: bare numbers are seconds; strings compose
    d/h/m/s suffixes ("3h", "90m", "1h30m", "1.5h", "2d12h").
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        text = str(value).strip().lower()
        try:
            seconds = float(text)
        except ValueError:
            match = _DURATION_RE.match(text)
            if not match or not any(match.groupdict().values()):
                raise ValueError(
                    f"invalid duration {value!r} — use seconds or d/h/m/s "
                    f'suffixes ("3h", "90m", "1h30m")'
                ) from None
            parts = {k: float(v or 0) for k, v in match.groupdict().items()}
            seconds = (
                parts["d"] * 86400 + parts["h"] * 3600 + parts["m"] * 60 + parts["s"]
            )
    if seconds <= 0:
        raise ValueError(f"duration must be positive, got {value!r}")
    return seconds


# ── YAML Config Model ────────────────────────────────────────────────


class MissionYAMLConfig(BaseModel):
    """Declarative mission configuration loaded from a YAML file.

    All fields mirror the CLI flags for `mission create`, plus additional
    features like lifecycle commands that are only available via YAML config.
    """

    model_config = {"extra": "forbid"}

    # Required
    objective: str

    # Optional mission settings
    working_dir: str = "."
    effects_profile: Literal["local", "git_managed", "dry_run"] = "local"
    llmvp_endpoint: str = "http://localhost:8008/graphql"
    flow_set: str = "code_core"
    # "batch" was named "parallel" until 2026-07-23 — renamed because true
    # parallelism now means the swarm/batched-engine paths; this mode is one
    # batched GENERATION, not concurrent workers. Legacy value accepted and
    # normalized at the read site (structural_sweep_next).
    # "session": one file per turn in ONE inference session, with a
    # deterministic cross-file check between turns. See MissionConfig for why.
    structural_mode: Literal["batch", "parallel", "serial", "session"] = "batch"
    # Stackable-phase ceiling (flow_sets.PHASE_RANKS keys): highest phase to
    # pursue before 'complete'. Default = the full pipeline through quality.
    top_phase: Literal[
        "structural", "environment", "functional", "test_suite", "quality", "polish"
    ] = "quality"
    # OPT-IN: design-phase domain research runs the deep_research sweep
    # instead of the one-shot search. Feasible as a daily only on batched
    # gpt-oss; punishing on pooled substrates. Default off.
    deep_research: bool = False
    principles: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)
    # Scraper flow set: how many candidate papers the corpus should reach.
    # The planner decides the SHAPE (relative weight per aspect), this the
    # SCALE — aspect targets are rescaled to sum here. 0 = leave the
    # planner's own numbers alone. See MissionConfig.corpus_target.
    corpus_target: int = Field(default=0, ge=0)

    # Run-termination policy. "completed" runs until the mission reaches
    # a terminal status (cycle budget becomes an opt-in backstop);
    # max_wall_clock parks the mission as paused when elapsed time
    # exceeds it ("3h", "90m", "1h30m", or bare seconds).
    run_until: Literal["cycle_budget", "completed"] = "cycle_budget"
    max_cycles: int | None = Field(default=None, gt=0)
    max_wall_clock: float | None = None

    @field_validator("max_wall_clock", mode="before")
    @classmethod
    def normalize_wall_clock(cls, v):
        return None if v is None else parse_duration(v)

    # Lifecycle commands — executed in the invoking cwd (not working_dir)
    pre_create: list[str] = Field(default_factory=list)
    post_create: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_objective_not_empty(self) -> "MissionYAMLConfig":
        if not self.objective.strip():
            raise ValueError("objective must not be empty")
        return self

    @model_validator(mode="after")
    def validate_flow_set_registered(self) -> "MissionYAMLConfig":
        # Fail fast at create time; runtime lookups fall back instead.
        from agent.flow_sets import FLOW_SETS

        if self.flow_set not in FLOW_SETS:
            raise ValueError(
                f"unknown flow_set {self.flow_set!r} — known sets: {sorted(FLOW_SETS)}"
            )
        return self


DEFAULT_MAX_CYCLES = 50


def resolve_run_policy(
    mission_config,
    cli_max_cycles: int | None = None,
    cli_max_wall_clock: str | None = None,
) -> tuple[int | None, float | None]:
    """(max_cycles, max_wall_clock_s) for run_agent — CLI beats mission
    config beats defaults.

    Under run_until="completed" the cycle budget defaults to None
    (unbounded): the run ends at a terminal mission status, an explicit
    backstop, or the wall clock. Defensive getattr keeps mission.json
    files persisted before these fields existed loading unchanged.
    """
    run_until = getattr(mission_config, "run_until", "cycle_budget")

    if cli_max_cycles is not None:
        max_cycles = cli_max_cycles
    elif getattr(mission_config, "max_cycles", None):
        max_cycles = mission_config.max_cycles
    elif run_until == "completed":
        max_cycles = None
    else:
        max_cycles = DEFAULT_MAX_CYCLES

    if cli_max_wall_clock is not None:
        max_wall_clock_s = parse_duration(cli_max_wall_clock)
    else:
        max_wall_clock_s = getattr(mission_config, "max_wall_clock_s", None)

    return max_cycles, max_wall_clock_s


# ── Loading ───────────────────────────────────────────────────────────


def resolve_config_path(name_or_path: str) -> Path:
    """Resolve a mission config name or path to a concrete file path.

    Rules:
    - If name_or_path ends in .yaml or .yml, treat as a direct path.
    - Otherwise, search for {name_or_path}.yaml in the current directory,
      then in missions/ (both relative to cwd and to the repo root) — the
      canonical home of mission configs since the 2026-07-16 root cleanup.

    Args:
        name_or_path: Either a filename/path or a bare name.

    Returns:
        Resolved Path to the YAML file.

    Raises:
        FileNotFoundError: If the resolved path does not exist.
    """
    if name_or_path.endswith(".yaml") or name_or_path.endswith(".yml"):
        candidates = [Path(name_or_path)]
    else:
        repo_root = Path(__file__).resolve().parents[1]
        candidates = [
            Path(f"{name_or_path}.yaml"),
            Path("missions") / f"{name_or_path}.yaml",
            repo_root / "missions" / f"{name_or_path}.yaml",
        ]

    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(
        f"Mission config not found: {name_or_path}\n  Searched: "
        + ", ".join(str(c.resolve()) for c in candidates)
    )


def load_mission_config(name_or_path: str) -> MissionYAMLConfig:
    """Load and validate a mission config from a YAML file.

    Args:
        name_or_path: Config name (searches {name}.yaml in cwd, then missions/)
                      or explicit path to a .yaml/.yml file.

    Returns:
        Validated MissionYAMLConfig instance.

    Raises:
        FileNotFoundError: If config file not found.
        ValueError: If YAML is invalid or fails validation.
    """
    path = resolve_config_path(name_or_path)
    logger.info(f"Loading mission config from {path.resolve()}")

    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(
            f"Mission config must be a YAML mapping, got {type(raw).__name__}"
        )

    return MissionYAMLConfig(**raw)


# ── Lifecycle Command Execution ───────────────────────────────────────


def run_lifecycle_commands(
    commands: list[str],
    *,
    phase: str = "pre_create",
    dry_run: bool = False,
    stream_output: bool = False,
) -> None:
    """Execute lifecycle commands sequentially in the current working directory.

    Fail-fast: the first non-zero exit aborts the remaining commands.

    Args:
        commands: Shell commands to execute in order.
        phase: Label for display ("pre_create" or "post_create").
        dry_run: If True, print commands without executing.
        stream_output: If True, stream stdout/stderr directly to terminal
                       instead of capturing (useful for long-running commands
                       like starting the agent).

    Raises:
        RuntimeError: If any command exits with non-zero status.
    """
    if not commands:
        return

    cwd = os.getcwd()
    print(f"⚙️  Running {len(commands)} {phase} command(s) in {cwd}")

    for i, cmd in enumerate(commands, 1):
        if dry_run:
            print(f"  [{i}/{len(commands)}] (dry-run) {cmd}")
            continue

        print(f"  [{i}/{len(commands)}] {cmd}")

        if stream_output:
            # Stream output directly to terminal (for long-running commands)
            result = subprocess.run(cmd, shell=True, cwd=cwd)
        else:
            # Capture output (for short setup commands)
            result = subprocess.run(
                cmd, shell=True, cwd=cwd, capture_output=True, text=True
            )
            if result.stdout and result.stdout.strip():
                for line in result.stdout.strip().splitlines():
                    print(f"    {line}")

        if result.returncode != 0:
            if not stream_output:
                stderr_msg = (
                    result.stderr.strip()
                    if hasattr(result, "stderr") and result.stderr
                    else "(no stderr)"
                )
                raise RuntimeError(
                    f"{phase} command failed (exit {result.returncode}): {cmd}\n"
                    f"  stderr: {stderr_msg}"
                )
            else:
                raise RuntimeError(
                    f"{phase} command failed (exit {result.returncode}): {cmd}"
                )

    print(f"  ✅ All {phase} commands completed")
