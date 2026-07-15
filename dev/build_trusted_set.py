#!/usr/bin/env python3
"""Condense all legacy label sets into the trusted training set per JUDGE_STANDARD v1.0 §5-6.

Offline gates only (no panels): full 3-vote count, unanimity, label in {low, medium}
(legacy highs are never retro-accepted), candidate/prompt corruption screen (pre-featurizer-fix
truncation signatures), and cross-set prompt dedup. Everything else lands in a quarantine
manifest with the remediation tier it needs.

Outputs: dev/trusted_labels_v1.json, dev/label_quarantine_v1.json
"""
import hashlib
import json
import re

STANDARD = "JUDGE_STANDARD v1.0"
TAG = re.compile(r"<\|[^|>]{1,32}\|>")


def corruption_flags(text):
    """Truncation signatures left by the pre-becacca featurizer bare-< bug (§6)."""
    if not isinstance(text, str) or not text.strip():
        return ["empty"]
    t = TAG.sub("￿", text)
    flags = []
    if t.rstrip().endswith(("<", "<=")):
        flags.append("trailing_lt")
    if re.search(r"<[ \t]*\n", t):
        flags.append("lt_eol")
    return flags


def sha(text):
    return hashlib.sha256("".join(str(text).split()).encode()).hexdigest()[:16]


def load_set(name):
    """Yield (orig_id, label, tally, task, stratum, prompt, candidate_actions{lvl: text})."""
    if name == "phaseB":
        labels = json.load(open("dev/phaseB_labels_clean.json"))
        content = {r["id"]: r for r in json.load(open("dev/phaseB_labeling.json"))}
        for r in labels:
            c = content.get(r["id"])
            if c is None:
                yield r["id"], r["label"], r["tally"], None, None, None, None
                continue
            acts = {lv: c["candidates"][lv]["action"] for lv in c["candidates"]}
            yield r["id"], r["label"], r["tally"], c["task"], c.get("bucket"), c["context"], acts
    elif name in ("phaseC", "tb1_cf"):
        lf = {"phaseC": "dev/phaseC_labels_clean.json", "tb1_cf": "dev/tb1_cf_labels.json"}[name]
        cf = {"phaseC": "dev/phaseC_labeling.json", "tb1_cf": "dev/tb1_cf_labeling.json"}[name]
        content = {r["id"]: r for r in json.load(open(cf))}
        for r in json.load(open(lf)):
            c = content.get(r["id"])
            if c is None:
                yield r["id"], r.get("label"), r.get("tally", {}), None, None, None, None
                continue
            acts = {lv: c["candidates"][lv].get("action", "") for lv in c["candidates"]}
            yield (r["id"], r.get("label"), r.get("tally", {}), c.get("task"),
                   c.get("stratum"), c.get("context"), acts)
    elif name == "grow":
        content = {r["id"]: r for r in json.load(open("dev/grow_panel_input.json"))}
        for r in json.load(open("dev/grow_labels.json")):
            c = content.get(r["id"])
            if c is None:
                yield r["id"], r.get("label"), r.get("tally", {}), None, None, None, None
                continue
            # candidates live at top level as low/medium/high keys
            acts = {}
            for lv in ("low", "medium", "high"):
                v = c.get(lv)
                acts[lv] = v.get("action", "") if isinstance(v, dict) else (v or "")
            yield (r["id"], r.get("label"), r.get("tally", {}), c.get("task"),
                   c.get("bucket"), c.get("context"), acts)
    elif name == "v3":
        turns = {t["id"]: t for t in json.load(open("dev/clean_turns_v3.json"))}
        acts = {}
        for line in open("dev/clean_actions_v3.jsonl"):
            line = line.strip()
            if not line:
                continue
            try:
                a = json.loads(line)
            except ValueError:
                continue
            if a.get("error"):
                continue
            acts.setdefault(a["id"], {})[a.get("level")] = a.get("action", "")
        for r in json.load(open("dev/clean_labels_v3.json")):
            t = turns.get(r["id"])
            if t is None:
                yield r["id"], r.get("level"), r.get("tally", {}), None, None, None, None
                continue
            yield (r["id"], r.get("level"), r.get("tally", {}), t.get("task"), None,
                   t.get("prompt"), acts.get(r["id"], {}))


SOURCES = ["phaseB", "phaseC", "tb1_cf", "grow", "v3"]
trusted, quarantine, seen_prompt = [], [], {}

for src in SOURCES:
    for oid, label, tally, task, stratum, prompt, acts in load_set(src):
        uid = f"{src}:{oid}"
        rec = {"uid": uid, "source": src, "orig_id": oid, "label": label,
               "tally": tally, "task": task, "stratum": stratum}

        def park(tier, detail=None):
            quarantine.append({**rec, "tier": tier, "detail": detail})

        if prompt is None or not acts or label not in ("low", "medium", "high"):
            park("malformed_or_unjoinable")
            continue
        nvotes = sum(tally.values()) if isinstance(tally, dict) and tally else 0
        if nvotes < 3:
            park("short_votes", f"votes={nvotes}")
            continue
        unanimous = max(tally.values()) == nvotes
        if label == "high":
            park("high_unanimous" if unanimous else "high_split")
            continue
        if not unanimous:
            park("split_2_1", f"tally={tally}")
            continue
        cflags = {"prompt": corruption_flags(prompt)}
        for lv, a in acts.items():
            cflags[lv] = corruption_flags(a)
        hit = {k: v for k, v in cflags.items() if v}
        if hit:
            park("corruption_suspect", hit)
            continue
        ph = sha(prompt)
        if ph in seen_prompt:
            prior = seen_prompt[ph]
            if prior.get("_conflicted"):
                park("dedup_duplicate", f"prompt_conflicted_earlier ({prior['uid']})")
            elif prior["label"] == label:
                park("dedup_duplicate", f"dup_of={prior['uid']}")
            else:
                park("dedup_conflict", f"conflicts_with={prior['uid']} ({prior['label']} vs {label})")
                trusted[:] = [t for t in trusted if t["uid"] != prior["uid"]]
                quarantine.append({k: v for k, v in prior.items() if k != "_conflicted"}
                                  | {"tier": "dedup_conflict", "detail": f"conflicts_with={uid}"})
                prior["_conflicted"] = True
            continue
        rec.update({"prompt_sha256": ph,
                    "candidate_sha256": {lv: sha(a) for lv, a in acts.items()},
                    "accepted_by": "retro-gate-3-0", "standard": STANDARD,
                    "caveats": ["candidates_pre_featurizer_fix"]})
        seen_prompt[ph] = rec
        trusted.append(rec)

SET_LEVEL = [
    {"tier": "no_raw_votes", "source": "phaseB2", "count": 367,
     "detail": "tallies never stored; ungateable; anomalous 49% medium mix",
     "action": "exclude; relabel under v1.0 only if its turns fill class gaps"},
    {"tier": "superseded_corpus", "source": "clean_v1", "count": 850,
     "detail": "corpus rebuilt as v3 with reassigned ids; turn cleaning status unknown",
     "action": "exclude; do not resurrect"},
    {"tier": "rule_labels_not_judged", "source": "grow_short", "count": 229,
     "detail": "blanket short->low; phaseB short bucket was 32 low / 8 MED, so ~20% mislabeled",
     "action": "exclude from gold; short->NOT-HIGH stays valid for routing"},
    {"tier": "dropped_by_prior_cleaning", "source": "phaseB(-22)+phaseC(-380)", "count": 402,
     "detail": "records the _clean variants already removed",
     "action": "stay dropped"},
]

from collections import Counter
tiers = Counter(q["tier"] for q in quarantine)
by_src = Counter(t["source"] for t in trusted)
by_lab = Counter(t["label"] for t in trusted)
by_task = Counter(t["task"] for t in trusted)

header = {"standard": STANDARD, "built": "2026-07-15",
          "gates": ["votes>=3", "unanimous", "label in {low,medium}",
                    "corruption screen (trailing_lt/lt_eol/empty on prompt+3 candidates)",
                    "cross-set prompt dedup"],
          "caveat": "ALL candidates pre-date the featurizer fix (llmvp becacca 2026-07-03); "
                    "pending the 250-turn post-fix regeneration calibration, this set is "
                    "TRUSTED-RETRO tier, not final gold.",
          "counts": {"trusted": len(trusted), "by_source": dict(by_src),
                     "by_label": dict(by_lab), "quarantined": len(quarantine),
                     "quarantine_tiers": dict(tiers)}}
json.dump({"header": header, "records": trusted},
          open("dev/trusted_labels_v1.json", "w"), indent=1)
json.dump({"header": {"standard": STANDARD, "built": "2026-07-15",
                      "tiers": dict(tiers), "set_level_exclusions": SET_LEVEL},
           "records": quarantine},
          open("dev/label_quarantine_v1.json", "w"), indent=1)

print(f"TRUSTED: {len(trusted)}  by_label={dict(by_lab)}  by_source={dict(by_src)}")
print(f"  tasks: {len(by_task)} distinct; top: {by_task.most_common(5)}")
print(f"QUARANTINED (record-level): {len(quarantine)}")
for t, c in tiers.most_common():
    print(f"  {t}: {c}")
print(f"SET-LEVEL EXCLUSIONS: {sum(s['count'] for s in SET_LEVEL)} across {len(SET_LEVEL)} sets")
