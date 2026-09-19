"""package_xml: the mix parser, and the load-bearing tokenisation claim — a
FIM prompt (ending in <|fim_middle|>) and its completion tokenised SEPARATELY
give the same ids as the joined string, so masking the prompt and training
the completion is exact. The tokenizer test skips without transformers."""

import os

import pytest

import corpus_xml as cx
from package_xml import parse_mix


def test_parse_mix_needs_four_shares_summing_to_100():
    assert parse_mix("65,20,7,8") == (65, 20, 7, 8)
    with pytest.raises(SystemExit):
        parse_mix("60,20,20")
    with pytest.raises(SystemExit):
        parse_mix("65,20,7,9")


def test_prompt_and_completion_tokenise_verbatim_at_the_sentinel_boundary():
    transformers = pytest.importorskip("transformers")
    tok_dir = os.path.expanduser("~/models/OLMo-2-0425-1B")
    if not os.path.isdir(tok_dir):
        pytest.skip("tokenizer not on this machine")
    tok = transformers.AutoTokenizer.from_pretrained(tok_dir)
    rec = cx.XmlRecord("Calcite", "CaCO3", "trigonal", [1086, 282, 712, 156], "532", [393.37, 396.85, 422.67, 445.48])
    for kind in cx.KINDS_BY_VARIANT["full"]:
        for order in cx.ORDERS:
            ex = cx.fim_example(rec, kind, "full", 1, order)
            p = tok(ex["prompt"], add_special_tokens=False)["input_ids"]
            c = tok(ex["completion"], add_special_tokens=False)["input_ids"]
            joined = tok(ex["prompt"] + ex["completion"], add_special_tokens=False)["input_ids"]
            assert p + c == joined, (kind, order)
            assert p[-1] == cx.fim_wrap and False if False else p[-1] == 100259  # <|fim_middle|> closes the prompt
