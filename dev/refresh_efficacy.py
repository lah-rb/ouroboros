#!/usr/bin/env python3
"""Rigorous, CONTROLLED efficacy test for the in-process context refresh.

The per-run stub-rate conflates server-souring with spec-difficulty: a hard spec
(e.g. primary-backup) stubs even on a fresh server, so "a soured run followed by a
clean run" can't prove the refresh did anything. This removes the confound with a
fixed-prompt BEFORE/AFTER on the SAME hard prompt:

  before := stub-rate of a FIXED hard code-authoring prompt on LLMVP-stateless (N reps)
  -- the probe is captured from a CLEAN run's FILE-producing rewrite (a prompt a
     HEALTHY server answers with code), so a high `before` is unambiguously SERVER
     rot, not spec difficulty --
  if before is high -> the server is soured; fire refreshContext (in-process rebuild)
  after  := stub-rate of the SAME prompt, N reps
  CURED iff after << before.

Run it when the server is volume-aged/soured (refresh OFF in the marathon so souring
accumulates). On a fresh server `before` is low -> it self-reports INCONCLUSIVE.

Usage:
  python dev/refresh_efficacy.py            # before/after (auto-skips if not soured)
  python dev/refresh_efficacy.py capture    # (re)capture the fixed probe prompt
"""
import json, re, sys, os, glob, urllib.request
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agent.trace_health import classify

LLMVP_URL = "http://localhost:8008/graphql"


def _llmvp_raw(prompt, temp, mt=2500):
    """Raw LLMVP completion via GraphQL (inlined from the retired
    dev/vanilla_compare.py forensics script)."""
    import urllib.request

    body = json.dumps({
        "query": "query($r: CompletionRequest!){ rawCompletion(request:$r){ rawText tokensGenerated } }",
        "variables": {"r": {"prompt": prompt, "maxTokens": mt, "temperature": temp}},
    }).encode()
    try:
        r = json.load(urllib.request.urlopen(urllib.request.Request(
            LLMVP_URL, body, {"Content-Type": "application/json"}), timeout=300))
    except Exception as ex:
        return None, str(ex)[:80]
    if r.get("errors"):
        return None, str(r["errors"][:1])[:80]
    raw = r["data"]["rawCompletion"]["rawText"]
    fm = re.search(r"final<\|message\|>(.*?)(?:<\|end\|>|<\|return\|>|$)", raw, re.DOTALL)
    return (fm.group(1).strip() if fm else raw.strip()), None

PROBE_F = "dev/efficacy_probe.txt"
GQL = "http://localhost:8008/graphql"
N = 10
SOURED_BEFORE = 0.25   # below this the server isn't soured -> test inconclusive
TEMP = 0.35


def capture_probe():
    """Capture the HARDEST file-producing generate_rewrite prompt from a preserved
    CLEAN run — a prompt a healthy server answered with code, so a high stub-rate on
    it later is unambiguously server souring (not spec difficulty)."""
    cands = (glob.glob("runs/marathon_capture/*/traces/*.jsonl")
             + glob.glob("runs/marathon_capture/*/.agent/traces/*.jsonl"))
    best = None
    for f in cands:
        for l in open(f, errors="ignore"):
            try:
                e = json.loads(l)
            except Exception:
                continue
            if (e.get("step") == "generate_rewrite" and e.get("prompt_content")
                    and (e.get("response_content") or "")
                    and classify(e["response_content"]) == "file"
                    and (e.get("tokens_in") or 0) > 1500):
                if best is None or (e.get("tokens_in") or 0) > best[1]:
                    best = (e["prompt_content"], e.get("tokens_in") or 0, f)
    if best is None:
        print("no clean file-producing rewrite (>1500 tok-in) found to use as probe")
        return False
    open(PROBE_F, "w").write(best[0])
    print(f"captured probe: {len(best[0])}c, {best[1]} tok-in, from {best[2].split('/')[-1]} -> {PROBE_F}")
    return True


def fire_refresh():
    body = json.dumps({
        "query": 'mutation{ refreshContext(reason:"efficacy"){ status elapsedS totalRefreshes } }'
    }).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        GQL, body, {"Content-Type": "application/json"}), timeout=180))
    return r["data"]["refreshContext"]


def replay(prompt, n):
    outs = []
    for _ in range(n):
        resp, err = _llmvp_raw(prompt, TEMP)
        outs.append("error" if err else classify(resp))
    return sum(o == "stub" for o in outs) / n, outs


def main():
    if not os.path.exists(PROBE_F) and not capture_probe():
        return
    prompt = open(PROBE_F).read()
    print(f"probe: {len(prompt)}c")

    before, _ = replay(prompt, N)
    print(f"(1) BEFORE refresh: stub {int(round(before*N))}/{N} = {before:.0%}")
    if before < SOURED_BEFORE:
        print(f"  server NOT soured (before {before:.0%} < {SOURED_BEFORE:.0%}) — the "
              f"in-process rot isn't present yet; re-run when the server is volume-aged. "
              f"INCONCLUSIVE (this is the expected result on a fresh server).")
        return

    ref = fire_refresh()
    print(f"(2) refreshContext: {ref}")
    after, _ = replay(prompt, N)
    print(f"(3) AFTER refresh:  stub {int(round(after*N))}/{N} = {after:.0%}")

    print("\n=== REFRESH-EFFICACY VERDICT ===")
    if after <= 0.15 or after <= before * 0.4:
        print(f"  ✅ CURED: stub-rate {before:.0%} -> {after:.0%} after a "
              f"{ref.get('elapsedS')}s in-process refresh. The context rebuild clears the "
              f"LLMVP-process rot WITHOUT a restart — the 24/7 no-restart fix is confirmed.")
    else:
        print(f"  ❌ NOT cured: {before:.0%} -> {after:.0%}. The in-process refresh did not "
              f"clear it — the rot persists in something the context rebuild doesn't reset.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "capture":
        capture_probe()
    else:
        main()
