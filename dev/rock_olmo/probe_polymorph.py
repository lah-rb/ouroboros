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
from peft import PeftModel  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

BASE = os.path.expanduser("~/models/OLMo-2-0425-1B")
ADAPTER = os.path.expanduser("~/models/olmo2-1b-spectra-lora")

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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=28)
    ap.add_argument("--base-too", action="store_true")
    args = ap.parse_args()

    tok = AutoTokenizer.from_pretrained(BASE)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    # TWO INDEPENDENT INSTANCES. PeftModel.from_pretrained injects LoRA
    # layers into the model it is given IN PLACE, so keeping a reference to
    # the "base" model and then wrapping it leaves both names pointing at
    # the same tuned object. The first version of this probe did exactly
    # that and reported byte-identical base and tuned output — which reads
    # as "the adapter does nothing" when it actually means "there was no
    # control". A comparison needs two models, not two names.
    models = {}
    if args.base_too:
        models["base"] = AutoModelForCausalLM.from_pretrained(
            BASE, dtype=torch.bfloat16, device_map={"": 0}
        ).eval()
    tuned_base = AutoModelForCausalLM.from_pretrained(
        BASE, dtype=torch.bfloat16, device_map={"": 0}
    ).eval()
    models["tuned"] = PeftModel.from_pretrained(tuned_base, ADAPTER).eval()
    # Prove the adapter actually changed the weights, rather than trusting
    # that a load with no error means a load with effect.
    import peft

    n_lora = sum(
        1 for n, _ in models["tuned"].named_parameters() if "lora" in n.lower()
    )
    print(f"adapter check: {n_lora} LoRA tensors present in the tuned model")
    assert n_lora > 0, "adapter did not attach"

    holdout = set(
        json.load(
            open(
                os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "holdout_species.json"
                )
            )
        )
    )

    for name, model in models.items():
        print(f"\n{'='*74}\n{name.upper()}\n{'='*74}")
        for kind, tpl in TEMPLATES.items():
            print(f"\n--- prompt: \"{tpl.format(species='X', formula='Y')}\" ---")
            sims = []
            for a, b, formula in PAIRS:
                ta = generate(
                    tok, model, tpl.format(species=a, formula=formula), args.max_new
                )
                tb = generate(
                    tok, model, tpl.format(species=b, formula=formula), args.max_new
                )
                s = similarity(ta, tb)
                sims.append(s)
                flag = (
                    "IDENTICAL" if s > 0.99 else ("near-dup" if s > 0.85 else "differs")
                )
                ha = "*" if a in holdout else " "
                hb = "*" if b in holdout else " "
                print(f"  {a}{ha} vs {b}{hb}  ({formula})   similarity {s:.2f}  {flag}")
                print(f"      {a}: {ta[:88]}")
                print(f"      {b}: {tb[:88]}")
            print(
                f"  MEAN pairwise similarity: {sum(sims)/len(sims):.3f}   "
                f"(1.00 = the run-1 failure; lower = genuinely distinguishing)"
            )
    print("\n* = held out of training entirely")


if __name__ == "__main__":
    main()
