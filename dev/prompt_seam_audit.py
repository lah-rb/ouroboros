"""Prompt-seam audit — mine agent CoT for prompts arguing with themselves.

THE IDEA (operator, 2026-08-18): a model that over-deliberates is a
contradiction DETECTOR. Ask it normal questions, watch it reason, and the
places it stalls are the places the prompt disagrees with itself. We already
run this experiment thousands of times a day — every traced inference is a
local model reading our prompts under real task pressure — we just never
read the results.

Proof of concept, found by hand in one CoT tail:

    "The user said 'The charter must contain five labeled parts' but listed
     six. We include six. Good."

charter_function.yaml had claimed five and listed six since CONSTRAINT was
added. Nobody caught it; the model caught it every single time and paid for
it in tokens.

WHAT THIS DOES
  1. reads every traced inference_call with thinking_content
  2. scores each CoT for SEAM SIGNALS — the model naming a conflict, an
     ambiguity, or an instruction it cannot satisfy
  3. joins step -> the prompt templates that step assembles (compiled.json),
     so a hit points at FILES, not just a step name
  4. ranks templates by hit RATE and by CoT concentration (tokens spent per
     turn), because a prompt that is merely long is not the same as a prompt
     that is confusing
  5. prints verbatim excerpts — the excerpt is the deliverable; the score
     only decides reading order

    python3 dev/prompt_seam_audit.py [--min-hits N] [--top N] [--quiet]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ── seam signals ──────────────────────────────────────────────────────
# Each pattern is a phrase a model uses when the PROMPT is the problem, not
# the task. Tuned to avoid ordinary task deliberation ("let me check the
# code") which is not a seam. Weight = how strongly it implicates the prompt.
SIGNALS: list[tuple[str, str, int]] = [
    # explicit self-contradiction in the instructions
    (
        "contradiction",
        r"\bbut (?:the prompt|the user|it|they) (?:said|listed|says|asks)\b",
        5,
    ),
    ("contradiction", r"\bcontradict\w*\b", 5),
    (
        "contradiction",
        r"\bbut listed\b|\bbut there are\b(?=[^.]{0,40}\b(?:six|five|four|three|seven)\b)",
        5,
    ),
    ("contradiction", r"\b(?:conflicts?|inconsistent) with\b", 4),
    # a constraint the task cannot satisfy (the §15 shape)
    ("unsatisfiable", r"\bmust adhere\b", 4),
    ("unsatisfiable", r"\bexceeds? (?:the )?limit\b", 4),
    ("unsatisfiable", r"\b(?:cheat|work ?around|get around)\b", 4),
    ("unsatisfiable", r"\bnot allowed\b", 3),
    ("unsatisfiable", r"\bimpossible to\b", 3),
    # ambiguity the model has to resolve on its own
    ("ambiguity", r"\b(?:unclear|ambiguous|vague)\b", 3),
    ("ambiguity", r"\bnot sure (?:what|which|whether|if)\b", 2),
    ("ambiguity", r"\bor should (?:I|we)\b", 2),
    ("ambiguity", r"\bwhich one (?:do|does|should)\b", 2),
    # self-auditing against the rules (the 30k-token charter signature)
    ("rule_audit", r"\bpotential issue\b", 3),
    ("rule_audit", r"\bthe (?:rule|instruction|brief|spec) says\b", 2),
    ("rule_audit", r"\bre-?read the (?:prompt|instructions?)\b", 3),
]
COMPILED = [(k, re.compile(p, re.I), w) for k, p, w in SIGNALS]


# Live workspaces live in /tmp, which does NOT survive a reboot — the CoT
# corpus is ~21 hours of compute and is not reproducible on demand. Archived
# copies under ~/ouroboros-runs/_workspace_archive_*/ are scanned too, so an
# audit still works after the machine cycles. Duplicate paths are harmless:
# the same call appearing in both places is the same trace, and archives are
# snapshots of workspaces that have since been wiped.
TRACE_GLOBS = (
    "/tmp/tier/*/.agent/traces/*.jsonl",
    str(
        Path.home()
        / "ouroboros-runs"
        / "_workspace_archive_*"
        / "*"
        / ".agent"
        / "traces"
        / "*.jsonl"
    ),
)


def cot_calls():
    """Every traced inference_call carrying reasoning, live or archived."""
    seen: set = set()
    paths = []
    for g in TRACE_GLOBS:
        paths += sorted(Path("/").glob(g.lstrip("/")))
    for tr in paths:
        # workspace label = the dir two levels above traces/
        model = tr.parent.parent.parent.name
        if tr.name in seen:
            continue
        seen.add(tr.name)
        for line in tr.open(errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("event_type") != "inference_call":
                continue
            if not r.get("thinking_content"):
                continue
            r["_model"] = model
            yield r


def step_templates() -> dict:
    """(flow, step) -> [prompt template ids] from the compiled flow graph."""
    out: dict = {}
    try:
        flows = json.loads((ROOT.parent / "flows" / "compiled.json").read_text())
    except Exception:
        return out
    for flow, fdef in flows.items():
        if not isinstance(fdef, dict):
            continue
        for step, sdef in (fdef.get("steps") or {}).items():
            tpls = []
            turn = sdef.get("turn") or {}
            for sec in turn.get("sections") or []:
                t = sec.get("template")
                if t:
                    tpls.append(t)
            pt = sdef.get("prompt_template") or turn.get("prompt_template") or {}
            if isinstance(pt, dict) and pt.get("template"):
                tpls.append(pt["template"])
            if tpls:
                out[(flow, step)] = tpls
    return out


_PROMPT_CORPUS: str = ""


def prompt_corpus() -> str:
    """Every prompt we ship, whitespace-normalised, as one haystack.

    THE ECHO PROBLEM. Models quote the brief back while reasoning, so a
    prompt containing the WORD "contradicts" ("behavior that contradicts the
    product's promise") lights up the contradiction detector on every single
    turn that reads it — quality_gate/plan_ux_charter scored 14 hits this way,
    all echo, zero findings. A seam signal only counts when the model wrote
    it ITSELF."""
    global _PROMPT_CORPUS
    if not _PROMPT_CORPUS:
        parts = []
        for f in (ROOT.parent / "prompts").rglob("*.yaml"):
            try:
                parts.append(" ".join(f.read_text(errors="replace").split()))
            except Exception:
                continue
        _PROMPT_CORPUS = "\n".join(parts).lower()
    return _PROMPT_CORPUS


def _is_echo(excerpt: str) -> bool:
    """True when this span is quoted prompt text rather than the model's own
    reasoning. Tested on a 40-char window around the match — long enough to be
    specific, short enough to survive the model's paraphrasing."""
    e = excerpt.lower()
    mid = len(e) // 2
    window = e[max(0, mid - 20) : mid + 20].strip()
    return len(window) > 12 and window in prompt_corpus()


def scan(text: str) -> list[tuple[str, int, str]]:
    """(kind, weight, excerpt) for each seam signal the MODEL wrote itself."""
    hits = []
    for kind, pat, w in COMPILED:
        for m in pat.finditer(text):
            a, b = max(0, m.start() - 130), min(len(text), m.end() + 130)
            ex = " ".join(text[a:b].split())
            if _is_echo(ex):
                continue
            hits.append((kind, w, ex))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-hits", type=int, default=2)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    tpl_map = step_templates()
    per_step: dict = defaultdict(
        lambda: {
            "turns": 0,
            "hits": 0,
            "score": 0,
            "cot": 0,
            "ex": [],
            "kinds": defaultdict(int),
        }
    )
    total = 0
    for r in cot_calls():
        total += 1
        key = (r.get("flow", "?"), r.get("step", "?"))
        d = per_step[key]
        d["turns"] += 1
        d["cot"] += int(r.get("reasoning_tokens") or 0)
        for kind, w, ex in scan(str(r["thinking_content"])):
            d["hits"] += 1
            d["score"] += w
            d["kinds"][kind] += 1
            if len(d["ex"]) < 3:
                d["ex"].append((kind, r["_model"], ex))

    print(
        f"scanned {total} reasoning traces across {len(per_step)} (flow, step) pairs\n"
    )
    ranked = sorted(
        (k for k, v in per_step.items() if v["hits"] >= args.min_hits),
        key=lambda k: per_step[k]["score"] / max(1, per_step[k]["turns"]),
        reverse=True,
    )
    print(
        f"{'flow/step':<42} {'turns':>5} {'hits':>5} {'score/turn':>10} {'CoT/turn':>9}"
    )
    for k in ranked[: args.top]:
        v = per_step[k]
        print(
            f"{k[0]+'/'+k[1]:<42} {v['turns']:>5} {v['hits']:>5} "
            f"{v['score']/max(1,v['turns']):>10.1f} {v['cot']//max(1,v['turns']):>9,}"
        )
    if args.quiet:
        return 0
    print("\n" + "=" * 78)
    print("EXCERPTS — the deliverable. Each points at the templates listed under it.")
    print("=" * 78)
    for k in ranked[: args.top]:
        v = per_step[k]
        kinds = ", ".join(
            f"{a}x{b}" for a, b in sorted(v["kinds"].items(), key=lambda x: -x[1])
        )
        print(f"\n### {k[0]}/{k[1]}  [{kinds}]")
        for t in tpl_map.get(k, []):
            print(f"    prompts/{t}.yaml")
        for kind, model, ex in v["ex"]:
            print(f"  ({kind}) …{ex}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
