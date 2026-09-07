"""The pre-registered decisive test for LoRA run 2 (PROCEDURE.md §16).

WHY THIS AND NOT EVAL LOSS. Run 1 posted a respectable -9.77% while
emitting IDENTICAL band lists for Quartz and Hematite. A loss computed on
a holdout that is 84% markdown measures markdown modelling; it cannot see
whether the model distinguishes two minerals. So the pre-registered test
is behavioural: does the model now produce DIFFERENT output for members
of a polymorph pair?

WHY POLYMORPHS ARE THE RIGHT PROBE. Anatase, Rutile and Brookite are all
TiO2. Composition cannot separate them — every composition-based
predictor we built sat at or below the unrelated-minerals floor on them.
Run 2's corpus states their structures explicitly (`structure` view) and
contrasts them directly (`polymorph` view). If that transferred, the
outputs must diverge. If they are still identical, the structural views
did not transfer and no loss number redeems it.

PAIRS ARE HELD OUT. Anatase, Brookite, Marcasite, Adamite, Laumontite,
Atacamite, Clinoatacamite, Minium and Andalusite are all in
holdout_species.json, so the model has never seen them; their partners
(Rutile, Pyrite, Paradamite, Wairakite, Botallackite, Scrutinyite,
Mullite) were in training. That asymmetry is deliberate — a difference
here cannot come from memorising the held-out member.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os

os.environ.setdefault("CUDA_VISIBLE_DEVICES", "0")

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

BASE = os.path.expanduser("~/models/OLMo-2-0425-1B")

PAIRS = [
    ("Anatase", "Rutile", "TiO2"),
    ("Brookite", "Rutile", "TiO2"),
    ("Marcasite", "Pyrite", "FeS2"),
    ("Adamite", "Paradamite", "Zn2(AsO4)(OH)"),
    ("Laumontite", "Wairakite", "CaAl2Si4O12·4H2O"),
    ("Atacamite", "Botallackite", "Cu2Cl(OH)3"),
    ("Andalusite", "Mullite", "Al2SiO5"),
]

TEMPLATES = {
    "raman": "{species} ({formula}) shows Raman bands at",
    "structure": "{species} ({formula}) is",
}


def generate(tok, model, prompt: str, max_new: int) -> str:
    enc = tok(prompt, return_tensors="pt").to(model.device)
    with torch.no_grad():
        out = model.generate(
            **enc,
            max_new_tokens=max_new,
            do_sample=False,
            pad_token_id=tok.eos_token_id,
        )
    return tok.decode(
        out[0][enc["input_ids"].shape[1] :], skip_special_tokens=True
    ).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def load_models(spec: str) -> dict:
    """name=DIR[,name=DIR...] -> {name: model}. Plain full checkpoints, one
    independent instance each (the v3 PEFT in-place hazard no longer applies:
    corpus v4 trains full weights)."""
    out = {}
    for item in spec.split(","):
        name, path = item.split("=", 1)
        out[name] = AutoModelForCausalLM.from_pretrained(
            os.path.expanduser(path), dtype=torch.bfloat16, device_map={"": 0}
        ).eval()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=28)
    ap.add_argument("--models", default=f"base={BASE}", help="name=DIR[,name=DIR...]")
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/probe_polymorph.json"))
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    models = load_models(args.models)
    from holdout import load_holdout_file

    probe = load_holdout_file()  # corpus v4: the reference-only probe set

    report = {}
    for name, model in models.items():
        print(f"\n{'='*74}\n{name.upper()}\n{'='*74}")
        report[name] = {}
        for kind, tpl in TEMPLATES.items():
            print(f"\n--- prompt: \"{tpl.format(species='X', formula='Y')}\" ---")
            sims = []
            pairs = []
            for a, b, formula in PAIRS:
                ta = generate(
                    tok, model, tpl.format(species=a, formula=formula), args.max_new
                )
                tb = generate(
                    tok, model, tpl.format(species=b, formula=formula), args.max_new
                )
                s = similarity(ta, tb)
                sims.append(s)
                pairs.append(
                    {
                        "a": a,
                        "b": b,
                        "formula": formula,
                        "similarity": round(s, 3),
                        "gen_a": ta[:160],
                        "gen_b": tb[:160],
                    }
                )
                flag = (
                    "IDENTICAL" if s > 0.99 else ("near-dup" if s > 0.85 else "differs")
                )
                ha = "*" if a in probe else " "
                hb = "*" if b in probe else " "
                print(f"  {a}{ha} vs {b}{hb}  ({formula})   similarity {s:.2f}  {flag}")
                print(f"      {a}: {ta[:88]}")
                print(f"      {b}: {tb[:88]}")
            mean = sum(sims) / len(sims)
            identical = sum(1 for s in sims if s > 0.99)
            report[name][kind] = {
                "mean_similarity": round(mean, 3),
                "identical_pairs": identical,
                "pairs": pairs,
            }
            print(
                f"  MEAN pairwise similarity: {mean:.3f}   identical {identical}/{len(sims)}   (1.00 = the run-1 failure; lower = distinguishing)"
            )
    json.dump(report, open(args.out, "w"), indent=1)
    print(
        f"\n* = in the reference-only probe set (never trained on); everything else was SEEN in corpus v4\n-> {args.out}"
    )


if __name__ == "__main__":
    main()
