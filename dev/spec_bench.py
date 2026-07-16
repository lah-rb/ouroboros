#!/usr/bin/env python3
"""A/B benchmark for n-gram speculative decoding (LlamaNGramMapDecoding).

The server's `decodeTpsRecent` (a prefill-excluded decode rate that rises as drafted
tokens are accepted) is the DECISION metric; chars/s incl-prefill is a cross-check.
Non-streaming (the shim doesn't emit SSE). Two prompts:
  REWRITE  — reproduce a real source file verbatim: the rewrite-heavy win case, where the
             prior file is in-context so the n-gram draft accepts long runs (the shape of
             the agent's generate_rewrite turns).
  CONTROL  — low-repetition creative text: the draft should rarely help and its logits_all
             overhead could even hurt — the guard against a net loss.
Run with speculative OFF, then ON; compare decodeTpsRecent on each prompt.
  usage: spec_bench.py [label]
"""
import glob
import json
import sys
import time
import urllib.request

URL = "http://localhost:8008/v1/completions"
GQL = "http://localhost:8008/graphql"

_cand = glob.glob("/tmp/marathon_*bytecode-vm*/vm/vm.py") or glob.glob("/tmp/marathon_*/**/*.py")
CODE = (open(_cand[0]).read()[:6000] if _cand else "def f(x):\n    return x * 2\n" * 60)

REWRITE = (
    "Here is a Python source file:\n\n```python\n" + CODE + "\n```\n\n"
    "Output the ENTIRE file again verbatim inside a ```python code block, adding a "
    "one-line module docstring at the very top. Do not omit or summarize anything.\n"
)
CONTROL = (
    "Write a short original poem about the sea, then explain what each line means. "
    "Be inventive and avoid repeating yourself.\n"
)


def _post(url, payload, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


def decode_tps_recent():
    return _post(GQL, {"query": "{health{decodeTpsRecent}}"}, 10)["data"]["health"]["decodeTpsRecent"]


def gen(prompt, max_tokens):
    t0 = time.time()
    d = _post(URL, {"prompt": prompt, "max_tokens": max_tokens, "temperature": 0.0,
                    "stream": False}, 600)
    wall = time.time() - t0
    txt = (d.get("choices") or [{}])[0].get("text", "")
    return len(txt), wall


def main():
    label = sys.argv[1] if len(sys.argv) > 1 else "?"
    print(f"=== spec_bench [{label}]  (decodeTpsRecent = decision metric) ===")
    for name, prompt, mx in [("REWRITE(win-case)", REWRITE, 1500), ("CONTROL(low-rep)", CONTROL, 500)]:
        runs = [gen(prompt, mx) for _ in range(2)]          # 2 reps; take the fuller one
        chars, wall = max(runs, key=lambda r: r[0])
        cps = chars / wall if wall else 0.0
        print(f"  {name}: {chars} chars in {wall:.1f}s = {cps:.0f} chars/s (incl prefill) "
              f"| server decodeTpsRecent={decode_tps_recent()}")


if __name__ == "__main__":
    main()
