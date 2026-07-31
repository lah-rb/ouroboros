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


def factors(cfg: dict) -> dict:
    m, r = cfg.get("model") or {}, cfg.get("resources") or {}
    resident = bool(m.get("resident_seq_cache"))
    batched = (r.get("decode_mode") or "pool") == "batched"
    return {
        "strategy": "S3" if batched else ("S2" if resident else "S1"),
        "family": m.get("family", "?"),
        "flow": bool(m.get("flow_kv_cache")),
        "swap": bool(m.get("reasoning_head_swap")),
        "swa_full": bool(m.get("swa_full")),
        "kv_unified": bool(m.get("kv_unified")),
        # Per-STREAM window: under batched, n_ctx is the shared pool.
        "stream_ctx": m.get("model_max_context") or m.get("n_ctx"),
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
            if min(counts.values()) < 2:
                notes.append(
                    f"{k}: level {min(counts, key=counts.get)!r} appears "
                    f"{min(counts.values())}x — too thin to fit, report descriptively"
                )
    return notes


def main(base: Path) -> int:
    rows = []
    for agent_dir in sorted(base.glob("*_agent")):
        name = agent_dir.name[: -len("_agent")]
        row = {"arm": name, **factors(_load_config(name)), **responses(agent_dir)}
        rows.append(row)
    if not rows:
        print(f"no completed arms under {base}")
        return 1

    cols = [
        "arm",
        "strategy",
        "obs_strategy",
        "family",
        "flow",
        "swap",
        "stream_ctx",
        "weights_gb",
        "wall_min",
        "inferences",
        "prefix_reuse",
        "flow_hits",
        "flow_fallbacks",
        "wiped",
    ]
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) + 1 for c in cols}
    print("".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("".join(str(r.get(c, "")).ljust(widths[c]) for c in cols))

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
    for r in rows:
        if r.get("flow"):
            h = r.get("flow_hits")
            verdict = "NOT SAMPLED" if h is None else ("fired" if h else "NEVER FIRED")
            print(
                f"   {r['arm']:<30} hits={h} builds={r.get('flow_builds')} "
                f"fallbacks={r.get('flow_fallbacks')}  → {verdict}"
            )

    print("\n── estimability (read BEFORE any effect claim) ──")
    for n in estimability(rows, ["strategy", "family", "flow", "swap"]):
        print(f"   {n}")
    wiped = [r["arm"] for r in rows if r.get("wiped")]
    if wiped:
        print(f"\n   ⚠️ context refresh wiped the caches mid-run: {', '.join(wiped)}")
        print("      those arms are not comparable to unwiped ones on cache responses")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1] if len(sys.argv) > 1 else ".").expanduser()))
