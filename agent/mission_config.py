"""Declarative YAML mission configuration loader.

Loads mission parameters from a YAML file, validates them with Pydantic,
and executes lifecycle commands around mission creation.

Lifecycle:
    1. pre_create  — runs before mission creation (wipe dirs, clean logs)
    2. mission create — creates the mission state
    3. post_create — runs after mission creation (start agent, setup scripts)

Usage:
    uv run ouroboros.py mission create --mission_config test
    # Loads test.yaml from current directory

    uv run ouroboros.py mission create --mission_config ./configs/test.yaml
    # Loads from explicit path
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)

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
    llmvp_endpoint: str = "http://localhost:8000/graphql"
    principles: list[str] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)

    # Lifecycle commands — executed in the invoking cwd (not working_dir)
    pre_create: list[str] = Field(default_factory=list)
    post_create: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_objective_not_empty(self) -> "MissionYAMLConfig":
        if not self.objective.strip():
            raise ValueError("objective must not be empty")
        return self


# ── Loading ───────────────────────────────────────────────────────────


def resolve_config_path(name_or_path: str) -> Path:
    """Resolve a mission config name or path to a concrete file path.

    Rules:
    - If name_or_path ends in .yaml or .yml, treat as a direct path.
    - Otherwise, search for {name_or_path}.yaml in the current directory.

    Args:
        name_or_path: Either a filename/path or a bare name.

    Returns:
        Resolved Path to the YAML file.

    Raises:
        FileNotFoundError: If the resolved path does not exist.
    """
    if name_or_path.endswith(".yaml") or name_or_path.endswith(".yml"):
        path = Path(name_or_path)
    else:
        path = Path(f"{name_or_path}.yaml")

    if not path.exists():
        raise FileNotFoundError(
            f"Mission config not found: {path}\n" f"  Searched: {path.resolve()}"
        )
    return path


def load_mission_config(name_or_path: str) -> MissionYAMLConfig:
    """Load and validate a mission config from a YAML file.

    Args:
        name_or_path: Config name (searches for {name}.yaml in cwd)
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
