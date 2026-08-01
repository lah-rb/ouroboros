"""Does the seam gate's OWN reachability pass already know combat is dead?

Read-only. Runs the existing _symbol_reachability over the shipped arm13 tree
and asks whether initiate_combat -- the method whose loss made the entire
combat/boss/victory/defeat subsystem unreachable -- appears in its `dead` set.

If it does, the gate had the finding in hand and discarded it: `dead` is
consumed only to SUPPRESS typecheck mismatches, never to raise one.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from agent.actions.batch_structural_actions import _symbol_reachability  # noqa: E402

TREE = Path.home() / "ouroboros-runs/tier_20260731-050209/staged/arm13/alpha"


def main():
    sources = {}
    for p in sorted(TREE.rglob("*.py")):
        if "-e" in p.name or "egg-info" in str(p):
            continue  # skip the stray backup and build output
        sources[str(p.relative_to(TREE))] = p.read_text()
    print(f"files fed to the gate: {len(sources)}")
    for f in sources:
        print(f"  {f}")

    reach = _symbol_reachability(sources)
    dead = reach["dead"]
    print(f"\nDEAD SYMBOLS REPORTED: {len(dead)}")
    for d in sorted(dead):
        print(f"  {d}")

    hit = [d for d in dead if "initiate_combat" in d]
    print(f"\ninitiate_combat in dead set: {bool(hit)}  {hit}")

    # Is the dead set a whole SUBSYSTEM or just stray helpers? Anything only
    # reachable from a dead symbol is dead too -- that is the high-signal shape.
    combat_ish = [d for d in dead
                  if any(k in d.lower() for k in
                         ("combat", "attack", "flee", "victory", "defeat",
                          "restart", "phase", "monster"))]
    print(f"\ncombat-cluster symbols in the dead set: {len(combat_ish)}")
    for d in sorted(combat_ish):
        print(f"  {d}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
