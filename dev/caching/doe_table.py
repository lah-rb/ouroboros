#!/usr/bin/env python3
"""Build the FACTOR × RESPONSE table for a tier sweep, and say which factors
are actually estimable from it.

    python3 dev/caching/doe_table.py ~/ouroboros-runs/tier_20260730-183039

WHY THIS SHAPE. A fleet sweep varies model, strategy, features, context and
sampling all at once, so it cannot support an A/B reading. What it CAN support
is factor screening: tabulate each arm's factor levels beside its responses,
look for strong main effects and couplings, and — critically — refuse to
report an effect for a factor that never varied independently of another.

That last part is the whole point of the estimability section. Two factors
that are perfectly correlated across the arms carry ONE piece of information
between them, and no amount of arithmetic separates them; printing a coefficient
for each would be inventing a result. This script names those pairs instead.

Responses come from the run's trace summary, including the `server` block
added 2026-07-30 (the cache/feature register, sampled from health). Its
`delta` is the first production evidence of whether the flow cache actually
fires — absolute counters are cumulative over the server's lifetime and mean
nothing per-run.
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CONFIGS = REPO / "llmvp" / "configs"


def _load_config(name: str) -> dict:
    """Resolve a config by stem, applying one `extends:` layer like the server."""
    import yaml

    for cand in (CONFIGS / f"{name}.yaml", CONFIGS / "experiments" / f"{name}.yaml"):
        if cand.is_file():
            d = yaml.safe_load(cand.read_text()) or {}
            if d.get("extends"):
                base = (
                    yaml.safe_load((CONFIGS / f"{d['extends']}.yaml").read_text()) or {}
                )
                for sec in ("model", "resources", "generation"):
                    merged = {**(base.get(sec) or {}), **(d.get(sec) or {})}
                    if merged:
                        d[sec] = merged
            return d
    return {}


# `cache_strategy:` is a top-level SHORTHAND that llmvp expands at LOAD time
# into resident_seq_cache / kv_unified / decode_mode. This script reads raw
# YAML — llmvp is not importable from this venv (it has its own) — so a config
# written with the shorthand would read as "not resident, not batched" and be
# filed silently as S1: the wrong cell, with nothing to show it was wrong.
# The authoritative table is CACHE_STRATEGIES in llmvp/core/config.py. An
# unrecognised value yields "?" so drift surfaces as a visible gap, never as a
# confident misclassification.
_STRATEGY_SHORTHAND = {"replay": "S1", "resident": "S2", "batched": "S3"}


def factors(cfg: dict) -> dict:
    m, r = cfg.get("model") or {}, cfg.get("resources") or {}
    resident = bool(m.get("resident_seq_cache"))
    batched = (r.get("decode_mode") or "pool") == "batched"
    shorthand = cfg.get("cache_strategy")
    return {
        "strategy": (
            _STRATEGY_SHORTHAND.get(str(shorthand).strip().lower(), "?")
            if shorthand is not None
            else ("S3" if batched else ("S2" if resident else "S1"))
        ),
        "family": m.get("family", "?"),
        "flow": bool(m.get("flow_kv_cache")),
        "swap": bool(m.get("reasoning_head_swap")),
        "swa_full": bool(m.get("swa_full")),
        "kv_unified": bool(m.get("kv_unified")),
        # Per-STREAM window: under batched n_ctx is the shared POOL and
        # model_max_context bounds one stream — but a stream cannot exceed the
        # pool it lives in, and several configs declare the model's full
        # TRAINED range there (laguna: 1,048,576 against an n_ctx of 234,496).
        # Taking model_max_context alone reported a window 4x the allocation.
        "stream_ctx": (
            min(m["model_max_context"], m["n_ctx"])
            if m.get("model_max_context") and m.get("n_ctx")
            else (m.get("model_max_context") or m.get("n_ctx"))
        ),
        "weights_gb": round((m.get("probe_verified_weights_bytes") or 0) / 1e9, 1),
    }


def responses(agent_dir: Path) -> dict:
    """Newest trace summary for an arm, flattened to the response variables."""
    summaries = sorted((agent_dir / "traces").glob("*.summary.json"))
    if not summaries:
        return {}
    s = json.loads(summaries[-1].read_text())
    body = s.get("summary") or s
    tok = body.get("tokens") or {}
    srv = body.get("server") or {}
    delta = (srv or {}).get("delta") or {}
    cached, fresh = tok.get("cached_prefix", 0), tok.get("fresh_prefill", 0)
    total_in = cached + fresh
    inf = body.get("inf_phase") or {}
    return {
        "wall_min": round((body.get("total_wall_ms") or 0) / 60000, 1),
        "inferences": (body.get("counts") or {}).get("inferences"),
        # THE cache quantity (corpus §6) — not "hit rate", which saturates.
        "prefix_reuse": round(cached / total_in, 3) if total_in else None,
        "prefill_ms": round(inf.get("prefill_ms", 0) / 1000, 1),
        "decode_ms": round(inf.get("decode_ms", 0) / 1000, 1),
        "cot_pct": (body.get("tokens_pct") or {}).get("cot_pct"),
        # Observed strategy, NOT the requested one — resident is a request the
        # architecture can refuse, and §12 tags arms by what actually ran.
        "obs_strategy": srv.get("strategy") or "",
        "can_shift": srv.get("can_shift"),
        "flow_hits": delta.get("flowHits"),
        "flow_builds": delta.get("flowBuilds"),
        "flow_fallbacks": delta.get("flowFallbacks"),
        "wiped": srv.get("cache_wiped_mid_run"),
    }


def estimability(rows: list[dict], keys: list[str]) -> list[str]:
    """Report factor pairs that never varied independently.

    A pair whose level-combinations form a 1:1 mapping carries one piece of
    information between them. Any 'effect' attributed to one is equally an
    effect of the other, so the honest output is the pairing, not a number.
    """
    notes = []
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            fwd, rev = defaultdict(set), defaultdict(set)
            for r in rows:
                fwd[r[a]].add(r[b])
                rev[r[b]].add(r[a])
            if all(len(v) == 1 for v in fwd.values()) and all(
                len(v) == 1 for v in rev.values()
            ):
                notes.append(f"{a} ⟂ {b}: perfectly confounded — one factor, not two")
    for k in keys:
        levels = {r[k] for r in rows}
        if len(levels) < 2:
            notes.append(
                f"{k}: only one level present ({levels}) — no effect estimable"
            )
        else:
            counts = {lv: sum(1 for r in rows if r[k] == lv) for lv in levels}
            # Report EVERY thin level, not just the minimum one. Naming a single
            # level implies the others are adequately covered — with strategy at
            # S1=1, S2=7, S3=1 the old form reported only S3, and a reader would
            # reasonably conclude S1 was fine. Both ends were resting on one arm.
            thin = sorted(
                ((lv, c) for lv, c in counts.items() if c < 2), key=lambda kv: str(kv[0])
            )
            if thin:
                shown = ", ".join(f"{lv!r} ({c}x)" for lv, c in thin)
                notes.append(
                    f"{k}: level(s) {shown} too thin to fit — report descriptively"
                )
    return notes


_ARM_RE = re.compile(r"ARM\s+(\d+)\s*/\s*\d+\s*:\s*(\S+)")
_DONE_RE = re.compile(
    r"done\s+(\d+)min\s+files=(\d+)\s+py_ok=(\d+)\s+py_fail=(\d+)\s+degen=(\d+)"
    r"(?:\s*\|\s*Status:\s*(\S+))?(?:.*?Goals\s*\((\d+)/(\d+))?"
)


def outcomes(base: Path) -> dict[str, dict]:
    """Per-arm WORK OUTPUT, parsed from batch.log's arm markers.

    WHY THE LOG AND NOT STATE.json. The runner computes these with
    `_authored()` (which excludes .venv/__pycache__/caches — the exclusion
    that corrected a phantom 7x 'productivity gap') plus a real `compile()`
    per .py file. But STATE.json only ever holds the CURRENT arm: its
    `results` list is empty on disk. The log line is therefore the only
    per-arm record that survives, and it is the same number the operator
    watched scroll past.

    WHY THIS BELONGS IN THE TABLE AT ALL. Without it the sweep tabulated
    every cache and throughput factor against no outcome — the responses
    were all *how* the machine ran and none were *what it produced*. A
    factor screen with no primary response cannot rank anything.
    """
    log = base / "batch.log"
    if not log.is_file():
        return {}
    out, cur = {}, None
    for line in log.read_text(errors="ignore").splitlines():
        m = _ARM_RE.search(line)
        if m:
            cur = m.group(2)
            continue
        d = _DONE_RE.search(line)
        if d and cur:
            wall, files, ok, bad, degen = (int(d.group(i)) for i in range(1, 6))
            out[cur] = {
                "files": files,
                "py_ok": ok,
                "py_fail": bad,
                "degen": degen,
                "status": d.group(6) or "",
                "goals": (
                    f"{d.group(7)}/{d.group(8)}" if d.group(7) and d.group(8) else ""
                ),
                # THE comparable throughput number. Arms do NOT get equal wall
                # time: the backstop is evaluated at cycle boundaries
                # (agent/loop.py), so a model with long cycles overruns it
                # further and is handed MORE time than a fast one. Absolute
                # file counts therefore flatter slow models, exactly backwards.
                "files_per_min": round(files / wall, 2) if wall else None,
                "log_wall_min": wall,
            }
            cur = None
    return out


def nested(rows: list[dict], keys: list[str]) -> list[str]:
    """One-WAY confounds, which the pairwise check above cannot see.

    `estimability` flags PERFECT (bidirectional) confounding: a 1:1 mapping
    between two factors. The commoner and sneakier case is asymmetric. In the
    2026-07-30 sweep every S1 arm was a qwen while S2 spanned seven families —
    not a 1:1 mapping, so the pairwise test stayed silent, yet any "S1 beats
    S2" claim is equally a "qwen beats everything else" claim and the design
    cannot separate them.

    Reported when a level of A occurs with exactly ONE level of B and that B
    level appears nowhere else: the two are then interchangeable as
    explanations. Levels with a single arm are skipped — "too thin to fit"
    already covers those and saying both would be noise.
    """
    notes = []
    for a in keys:
        for b in keys:
            if a == b:
                continue
            for lv in {r[a] for r in rows}:
                inside = [r for r in rows if r[a] == lv]
                if len(inside) < 2:
                    continue  # already reported as too thin
                bs = {r[b] for r in inside}
                if len(bs) != 1:
                    continue
                only = next(iter(bs))
                if any(r[b] == only for r in rows if r[a] != lv):
                    continue  # that B level also occurs elsewhere — separable
                notes.append(
                    f"{a}={lv!r} occurs ONLY with {b}={only!r} "
                    f"({len(inside)} arms) — an effect of one is an effect of "
                    f"the other; this design cannot separate them"
                )
    return notes


def main(base: Path) -> int:
    tally = outcomes(base)
    rows = []
    for agent_dir in sorted(base.glob("*_agent")):
        name = agent_dir.name[: -len("_agent")]
        row = {
            "arm": name,
            **factors(_load_config(name)),
            **responses(agent_dir),
            **tally.get(name, {}),
        }
        rows.append(row)
    if not rows:
        print(f"no completed arms under {base}")
        return 1

    def _emit(title: str, cols: list[str]) -> None:
        widths = {
            c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) + 1 for c in cols
        }
        print(f"\n── {title} ──")
        print("".join(c.ljust(widths[c]) for c in cols))
        for r in rows:
            print("".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))

    # Split FACTORS from RESPONSES: one screen was 14 columns wide and mixed
    # what was SET with what was MEASURED, which is the one distinction a
    # factor screen exists to keep straight.
    _emit(
        "FACTORS (what was set)",
        [
            "arm",
            "strategy",
            "obs_strategy",
            "family",
            "flow",
            "swap",
            "stream_ctx",
            "weights_gb",
        ],
    )
    _emit(
        "RESPONSES (what was measured)",
        [
            "arm",
            "files",
            "py_ok",
            "py_fail",
            "files_per_min",
            "wall_min",
            "degen",
            "status",
            "goals",
            "inferences",
            "prefix_reuse",
            "flow_hits",
            "wiped",
        ],
    )

    walls = [r["log_wall_min"] for r in rows if r.get("log_wall_min")]
    if walls and max(walls) > 1.5 * min(w for w in walls if w):
        print(
            f"\n   ⚠️ UNEQUAL WALL TIME: arms ran {min(walls)}–{max(walls)} min against"
            "\n      a single declared backstop. The limit is checked at CYCLE"
            "\n      boundaries, so slow models overrun it further and receive MORE"
            "\n      time. Rank on files_per_min; absolute `files` is not comparable."
        )

    print("\n── requested vs OBSERVED strategy ──")
    for r in rows:
        if r.get("obs_strategy") and r["obs_strategy"] not in ("", None):
            want = {"S1": "full_replay", "S2": "resident", "S3": "resident"}[
                r["strategy"]
            ]
            if r["obs_strategy"] != want:
                print(
                    f"   {r['arm']}: config implies {want}, server ran "
                    f"{r['obs_strategy']} (can_shift={r['can_shift']})"
                )

    print("\n── did the flow cache FIRE? (delta, not absolute) ──")
    print("   ⚠️ REGIME: the flow cache is designed for the interact -> diagnose")
    print("      -> fix cycle, where the SAME flows are re-entered many times.")
    print("      A `top_phase: structural` run enters each flow about once, so a")
    print("      low hit count here is EXPECTED and is NOT evidence the feature")
    print("      does not pay. The question it was built for needs a functional-")
    print("      phase run. (operator, 2026-07-30)")
    for r in rows:
        if r.get("flow"):
            h = r.get("flow_hits")
            verdict = "NOT SAMPLED" if h is None else ("fired" if h else "NEVER FIRED")
            print(
                f"   {r['arm']:<30} hits={h} builds={r.get('flow_builds')} "
                f"fallbacks={r.get('flow_fallbacks')}  → {verdict}"
            )

    print("\n── estimability (read BEFORE any effect claim) ──")
    FACTORS = ["strategy", "family", "flow", "swap"]
    for n in estimability(rows, FACTORS):
        print(f"   {n}")

    # An arm that produced NOTHING still carries factor levels, so the check
    # above counts it as coverage. It is not: a run that died before writing a
    # file cannot inform any claim about what the factors DO. Re-run the same
    # check over the arms that actually produced work — this is usually the
    # stricter, and the honest, answer.
    live = [r for r in rows if (r.get("files") or 0) > 0]
    dead = [r["arm"] for r in rows if (r.get("files") or 0) == 0]
    if dead and live:
        print(
            f"\n   ── on the OUTCOME-INFORMATIVE subset ({len(live)}/{len(rows)} arms) ──"
        )
        print(f"      produced nothing, carry no outcome info: {', '.join(dead)}")
        for n in estimability(live, FACTORS):
            print(f"      {n}")
        for n in nested(live, FACTORS):
            print(f"      ⚠️ {n}")
    wiped = [r["arm"] for r in rows if r.get("wiped")]
    if wiped:
        print(f"\n   ⚠️ context refresh wiped the caches mid-run: {', '.join(wiped)}")
        print("      those arms are not comparable to unwiped ones on cache responses")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser()))
