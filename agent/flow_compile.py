"""CUE flow-set compiler — validate + export + merge to compiled.json.

Extracted from the ouroboros.py CLI handler (compiler logic living in the
CLI made it untestable and unusable programmatically). The CLI's
``cue-compile`` subcommand is now a thin shell over :func:`compile_flows`.

Flow sets are directories under flows/: ``shared/`` holds the set-agnostic
layer (schemas, templates, generic leaf flows); every other directory with
.cue files is a set (code_core, ops, scraper, ...). All files share
``package ouroboros`` — each set compiles as a file-list instance of
shared + the set's own files (CUE hidden fields like _templates are not
importable across packages, so file-list unification is the mechanism,
not cue.mod imports).

Pipeline per invocation:
  1. shared/ vets + exports STANDALONE — a shared file referencing a
     set-local symbol fails here (the cross-set guard).
  2. each set vets + exports as shared + the set's file list.
  3. exports merge into one flat flows/compiled.json; shared keys dedupe
     (deep-equal), differing duplicates are an error.
  4. quick structural sanity pass (entry/transition targets exist) — a
     fast fail-safe; the deep contract checks live in agent/flow_lint.py.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
from typing import Callable


class FlowCompileError(Exception):
    """Compilation failed; the message is user-presentable."""


def _find_cue_binary(project_root: str) -> str:
    for candidate in ("cue", os.path.join(project_root, "cue")):
        try:
            subprocess.run([candidate, "version"], capture_output=True, check=True)
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    raise FlowCompileError(
        "'cue' not found. Install from https://cuelang.org/docs/install/"
    )


def compile_flows(
    flows_dir: str,
    log: Callable[[str], None] = print,
) -> tuple[str, int]:
    """Compile every flow set under ``flows_dir`` into compiled.json.

    Returns ``(compiled_path, flow_count)``; raises FlowCompileError on any
    validation/merge/structural failure (compiled.json is written atomically
    and only after a clean merge).
    """
    project_root = os.path.dirname(os.path.abspath(flows_dir))
    compiled_path = os.path.join(flows_dir, "compiled.json")

    if os.path.isdir(os.path.join(flows_dir, "cue")):
        raise FlowCompileError(
            "flows/cue/ still exists — this checkout predates the flow-set "
            "layout (flows/shared/ + flows/<set>/). Pull or migrate."
        )

    shared_files = sorted(
        os.path.relpath(p, flows_dir)
        for p in glob.glob(os.path.join(flows_dir, "shared", "*.cue"))
    )
    if not shared_files:
        raise FlowCompileError("flows/shared/ has no .cue files")

    set_names = sorted(
        d
        for d in os.listdir(flows_dir)
        if d != "shared"
        and os.path.isdir(os.path.join(flows_dir, d))
        and glob.glob(os.path.join(flows_dir, d, "*.cue"))
    )
    if not set_names:
        raise FlowCompileError("no flow-set directories found under flows/")

    cue_bin = _find_cue_binary(project_root)

    def _cue(verb: str, files: list[str], label: str) -> str:
        result = subprocess.run(
            [cue_bin, verb, *files] + (["--out", "json"] if verb == "export" else []),
            capture_output=True,
            text=True,
            cwd=flows_dir,
        )
        if result.returncode != 0:
            raise FlowCompileError(f"CUE {verb} failed for {label}:\n{result.stderr}")
        return result.stdout

    # Step 1: shared must stand alone (cross-set guard).
    log("Validating shared layer (standalone)...")
    _cue("vet", shared_files, "shared")
    try:
        shared_keys = set(json.loads(_cue("export", shared_files, "shared")))
    except json.JSONDecodeError as e:
        raise FlowCompileError(f"shared export produced invalid JSON: {e}") from e

    # Steps 2-3: vet + export each set, merge.
    merged: dict = {}
    provenance: dict[str, str] = {}
    for set_name in set_names:
        set_files = shared_files + sorted(
            os.path.relpath(p, flows_dir)
            for p in glob.glob(os.path.join(flows_dir, set_name, "*.cue"))
        )
        log(f"Compiling flow set '{set_name}' ({len(set_files)} files)...")
        _cue("vet", set_files, set_name)
        try:
            data = json.loads(_cue("export", set_files, set_name))
        except json.JSONDecodeError as e:
            raise FlowCompileError(
                f"CUE export for '{set_name}' produced invalid JSON: {e}"
            ) from e
        for key, value in data.items():
            if key not in merged:
                merged[key] = value
                provenance[key] = set_name
                continue
            if merged[key] == value:
                if key not in shared_keys:
                    log(
                        f"⚠️  '{key}' defined identically in "
                        f"'{provenance[key]}' and '{set_name}' — move it to shared/"
                    )
                continue
            raise FlowCompileError(
                f"'{key}' differs between flow sets '{provenance[key]}' and "
                f"'{set_name}' — flow names must be globally unique."
            )

    flow_count = sum(1 for v in merged.values() if isinstance(v, dict) and "flow" in v)

    # Step 4: quick structural sanity BEFORE writing — a broken graph should
    # never replace a good compiled.json. (The CLI previously wrote first and
    # exited 1 after, leaving the bad artifact in place.)
    errors = []
    for name, flow_def in merged.items():
        if not isinstance(flow_def, dict) or "flow" not in flow_def:
            continue
        entry = flow_def.get("entry", "")
        steps = flow_def.get("steps", {})
        if entry and entry not in steps:
            errors.append(f"  {name}: entry step '{entry}' not in steps")
        for step_name, step_def in steps.items():
            if not isinstance(step_def, dict):
                continue
            resolver = step_def.get("resolver", {})
            if resolver.get("type") == "rule":
                for rule in resolver.get("rules", []):
                    target = rule.get("transition", "")
                    if target and target not in steps:
                        errors.append(
                            f"  {name}.{step_name}: transition '{target}' not in steps"
                        )
    if errors:
        raise FlowCompileError("Structural issues found:\n" + "\n".join(errors))

    # Atomic write — compiled.json is never invalid mid-write. Key order is
    # deterministic without sort_keys: cue export's ordering is stable and
    # sets merge in sorted order.
    tmp_path = compiled_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(merged, f, indent=2)
    os.replace(tmp_path, compiled_path)

    return compiled_path, flow_count
