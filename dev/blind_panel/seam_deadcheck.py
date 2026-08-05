"""What does the seam gate's OWN reachability pass know, across every arm?

The gate (`_phase_exit_seam_gate`) computes `_symbol_reachability(sources)` at
every phase exit. Its `dead` key means "defined, never referenced anywhere
else". That output is consumed ONLY to decide whether an already-reported
typecheck mismatch is blocking -- it never raises a report of its own. So a
method that is perfectly written and simply has no callers is invisible to the
gate, which is the shape that killed arm13 (see OPEN_TASKS §18).

This sweeps the staged artifacts and asks the question that decides the fix:

  * If dead symbols are RARE and land on decisive defects, blocking is safe.
  * If they are COMMON and mostly harmless helpers, the report must be
    advisory, and only a reachability REGRESSION (live at the previous phase
    exit, dead at this one) should ever block.

Read-only. Usage:
    uv run python -P dev/blind_panel/seam_deadcheck.py [staged_root]
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from agent.actions.batch_structural_actions import _symbol_reachability  # noqa: E402

DEFAULT_ROOT = Path.home() / "ouroboros-runs/tier_20260731-050209/staged"

# Substrings that mark a symbol as part of the game's CORE PLAYABLE LOOP.
# A dead symbol matching one of these is a candidate decisive defect; a dead
# symbol that does not is much more likely to be an unused helper.
CORE = (
    "combat",
    "attack",
    "flee",
    "victory",
    "defeat",
    "restart",
    "win",
    "boss",
    "phase",
    "monster",
    "fight",
    "damage",
    "death",
    "die",
)


# A computed-name getattr is dispatch a static scan CANNOT resolve, so every
# method it can reach looks dead. `_symbol_reachability`'s docstring says
# dynamically dispatched symbols are treated as live, but that only covers
# literal names -- `getattr(self, handler_name, ...)` defeats it. Detected and
# reported, because it is the dead set's dominant false-positive mode.
DYNAMIC = re.compile(r"getattr\(\s*(?:self|cls|engine)\s*,\s*(?!['\"])")


def scan_tree(tree: Path) -> dict:
    sources = {}
    for p in sorted(tree.rglob("*.py")):
        s = str(p)
        if "egg-info" in s or p.name.endswith("-e"):
            continue
        try:
            sources[str(p.relative_to(tree))] = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
    if not sources:
        return {}
    try:
        reach = _symbol_reachability(sources)
    except Exception as e:  # a broken tree is a finding, not a crash
        return {"error": f"{type(e).__name__}: {e}", "files": len(sources)}
    dead = sorted(reach["dead"])
    dynamic = sorted(f for f, src in sources.items() if DYNAMIC.search(src))
    # a second, independent confound: two complete trees shipped side by side
    engines = [f for f in sources if f.endswith("engine.py")]
    return {
        "files": len(sources),
        "dead": dead,
        "core": [d for d in dead if any(k in d.lower() for k in CORE)],
        "dynamic": dynamic,
        "dup_tree": len(engines) > 1,
    }


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    if not root.exists():
        print(f"no staged root at {root}")
        return 1

    arms = sorted(d for d in root.iterdir() if d.is_dir())
    print(f"staged root: {root}\narms: {len(arms)}\n")

    tot_dead = tot_core = arms_with_core = 0
    rows = []
    for arm in arms:
        # each arm holds the artifact under a label dir (alpha/judge1/...);
        # scan exactly ONE copy per arm so per-judge duplicates do not inflate
        cands = [d for d in sorted(arm.iterdir()) if d.is_dir()] or [arm]
        tree = next((c for c in cands if any(c.rglob("*.py"))), None)
        if tree is None:
            rows.append((arm.name, 0, [], [], "no python", [], False))
            continue
        r = scan_tree(tree)
        if not r or "error" in r:
            rows.append(
                (
                    arm.name,
                    r.get("files", 0),
                    [],
                    [],
                    r.get("error", "empty"),
                    [],
                    False,
                )
            )
            continue
        rows.append(
            (
                arm.name,
                r["files"],
                r["dead"],
                r["core"],
                "",
                r.get("dynamic") or [],
                r.get("dup_tree", False),
            )
        )
        tot_dead += len(r["dead"])
        tot_core += len(r["core"])
        arms_with_core += 1 if r["core"] else 0

    print(f"{'arm':8s} {'files':>5s} {'dead':>5s} {'core':>5s}  symbols")
    print("-" * 92)
    for name, files, dead, core, err, dyn, dup in rows:
        if err:
            print(f"{name:8s} {files:5d}     -     -  ({err})")
            continue
        mark = "   <-- CORE LOOP" if core else ""
        names = ", ".join(d.split("::")[-1] for d in dead) or "-"
        print(f"{name:8s} {files:5d} {len(dead):5d} {len(core):5d}  {names[:70]}{mark}")
        if dyn or dup:
            why = []
            if dyn:
                why.append("computed-name getattr dispatch in " + ", ".join(dyn))
            if dup:
                why.append("TWO engine.py trees shipped")
            print(f"{'':22s}!! DEAD SET UNRELIABLE HERE: {'; '.join(why)}")

    n = len([r for r in rows if not r[4]])
    conf = len([r for r in rows if not r[4] and (r[5] or r[6])])
    print(f"\nSCANNED {n} arms with a readable tree")
    print(f"  total dead symbols               : {tot_dead}")
    print(f"  dead symbols in the CORE LOOP    : {tot_core}")
    print(f"  arms with a CORE-LOOP dead symbol: {arms_with_core} of {n}")
    print(f"  arms where the dead set is CONFOUNDED: {conf} of {n}")
    print("\nREADING: a high dead count with few core hits argues the report must")
    print("be ADVISORY (unused helpers are normal). Core hits are the candidates")
    print("for blocking -- and a reachability REGRESSION is narrower still.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
