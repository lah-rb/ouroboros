# SINGLE_MODEL_FLIGHT_PROMPT — the canonical N-way same-model judge prompt

For the question `FLIGHT_PROMPT.md` cannot ask: **not** "which model is
better" but "which of THIS model's runs is best, and how much do its runs
vary?" Established 2026-08-05 for the qwen3-next-coder-80b-a3 triple.

Two existing instruments each answer half of it, which is why this file
exists rather than an improvisation:

* `FLIGHT_PROMPT.md` carries the current axes (TIER_RUBRIC v2.1, ten forced
  choices, two panels) but is strictly **pairwise** — A or B, per-axis ties
  forbidden. Three artifacts do not fit it.
* `JUDGE_PROMPT_tier_2026-07-27.md` is **N-way** (alpha/beta/gamma/delta) and
  is the shape this needs, but scores solo `/50` across five v1 dimensions —
  a path METHODS.md retired outright ("The solo-scoring path (v1) is retired
  for future campaigns").

So: the 07-27 N-way shape, the v2.1 axes, ranking instead of forced choice.

## What is different about a same-model flight

Every artifact came from the same model, the same config, and the same brief.
So the attribution label **model-innate is a CONSTANT across the set** — it
cannot explain a difference between these arms. Whatever separates them is
run-level: interaction with the harness, framework coupling, cycle budget,
and chance. Say which, per decisive difference. That judgement is the point
of the flight, and it is the one thing a pairwise model-vs-model flight never
has to make.

Corollary: the SPREAD is a finding in its own right. Three runs landing in a
tight band and three runs landing far apart are different facts about the
model, and both are worth recording even when the ranking is obvious.

## Panel size

METHODS.md §3 requires three independent judges and gives the reason — judge
severity drifts between rounds. A single-judge flight is legitimate for a
same-model variability read, where the question is the ORDER within one
model rather than a placement on the ladder, but the record must then say
`SINGLE JUDGE — order is provisional, no cross-judge convergence`. Do not
promote a single-judge result into a ladder placement.

## Running one

    python3 dev/blind_panel/stage.py --judges N <a>/alpha <b>/alpha <c>/alpha \
        --out <blind_root>
    python3 dev/blind_panel/make_judge_packet.py <blind_root>/judge1/alpha \
        --out <blind_root>/packet/alpha        # and beta, gamma

Keep `KEY.json` outside every packet. Fill `{PACKET_ROOT}` and
`{LABELS}` below and send the block **verbatim** to one freshly spawned
subagent per judge (claude-opus-5 unless the record says otherwise).

---

You are a BLIND JUDGE for {N} anonymous software artifacts. Your final message is the official record.

PACKET ROOT: {PACKET_ROOT}
- Artifacts: {LABELS} — each at <packet root>/<label>/artifact/
- The rubric and checklist are at <packet root>/alpha/RUBRIC.md and <packet root>/alpha/CHECKLIST.md (the other labels' copies are identical). Read BOTH documents fully before touching any artifact.

HARD WALLS (violating any of these invalidates the flight):
- Read and write ONLY inside the packet root and your scratch dir: <packet root>/scratch/ (create it; give each artifact its own subdir for play so save files cannot collide — COPY an artifact into scratch before playing it, because these programs write into their own tree and whatever you leave behind becomes something the next reader charges to the artifact).
- NEVER read ~/ouroboros-runs/, the ouroboros repo (except the single interpreter binary below), any config, log, trace, or git anything.
- Do not try to identify which model or system produced any artifact. Do not guess. They may all be the same system; that is not a hint about quality.
- Interpreter: use /Users/lah-rb/Repos/ouroboros/.venv/bin/python to RUN the games. That path is allowed ONLY as an interpreter.

PROTOCOL — play first, in this order:
1. Read RUBRIC.md and CHECKLIST.md fully. Note it is TIER_RUBRIC v2.1: TEN axes in TWO panels, tallied separately and never summed.
2. PLAY every artifact, in label order (interactive: drive real input through the program's own command line; iterate on what it actually accepts; push toward the WIN CONDITION). Source reading is allowed ONLY to explain a failure you already observed in play, never as a substitute for play. Static review has systematically misranked these: the artifact that READS better has repeatedly been the one unplayable past room 3 of 8.
3. Robustness probes on each: unknown commands, empty input, invalid moves, mid-combat oddities, EOF/Ctrl-D. Distinguish clean refusal from traceback from silent misinterpretation.
4. MODIFICATION PROBE on each (mandatory, feeds axis B9): add a ninth room, or change a weapon's damage, WITHOUT touching unrelated code. Report exactly what you had to touch and what broke.
5. Per artifact, record:
   - a PREMISE LINE — two sentences on what this game IS, in your own words, plus one quoted line of its prose. Never ranked; it exists so the record says what the artifact was, not only how it worked.
   - completability class (WON / WINNABLE-NOT-WON / UNWINNABLE / NO-TERMINAL-STATES — playing toward the win is mandatory)
   - the conformance tally per CHECKLIST.md (met/47, unmet item numbers, then the binary verdict with its trigger)
   - a state-integrity note, the robustness table, the modification-probe result, the furthest point reached
   - a quoted transcript excerpt for every decisive finding
   - attribution labels: model-innate / interaction / framework-coupled. CHARGE WHAT SHIPS: everything in the artifact tree is IN comparison regardless of label — record the label, never exclude on it. The ONE exclusion is PROJECT LAYOUT, which the scaffold dictates identically for every artifact — documentation IS in comparison (axis B10).
   - **Expect cross-module seam bugs.** In every panel run so far the decisive defect has been a mismatched identifier or key BETWEEN files that are each internally reasonable (`shadow_lord` vs `shadow_lich`; a `Boss` class lacking the `attack` attribute the engine probed for). Look for them specifically, and say whether a seam bug is what stopped you.

FLIGHT VERDICT — rank ALL artifacts on each axis, best first, with one line of justification. Ties within an axis are FORBIDDEN; break them on the evidence you actually observed and say what broke it.

PANEL A — DELIVERY (does it work):
  A1 working surface — how much of what it offers works when invoked
  A2 state integrity — persistence/reset round-trips without rewriting the world
  A3 robustness — clean refusal vs traceback vs silent misinterpretation
  A4 delivered scope — how much landed, absolute

PANEL B — CHARACTER (what is it):
  B5 ambition — which REACHED FURTHER, judged on the design it set out to build, NOT on how much of it works. An artifact that attempted a two-phase boss with a hidden weakness and failed to wire it reached further than one that shipped a single-phase boss cleanly. Delivery is charged on Panel A; charging it again here is double-counting. If you are writing "but none of it survived", you are voting on the wrong panel.
  B6 imagination (world & voice) — premise, place, prose, the non-obvious idea, IGNORING whether it works. A vivid unreachable world beats a generic reachable one on this axis.
  B7 experience/UX (felt play) — pacing, discoverability, feedback, tension, sense of place. Is combat tense or arithmetic? Are the NPCs worth talking to? THIS IS NOT THE HELP TEXT.
  B8 craft/UI (surface) — help accuracy, status legibility, naming, error messages, input tolerance, prompt hygiene.
  B9 workability — could a person work in this? Logic organisation, project organisation, and reusability as ONE question, decided substantially by your modification probe.
  B10 documentation — accuracy against play first (a claim contradicted by play is worse than no claim), then completeness: would this let a stranger run and extend it?

Then report, SEPARATELY and without summing them:
- **Panel A order** and **Panel B order** (each a full ranking, with the count of first places per artifact)
- **ONE overall ranking**, best first
- If the two panels disagree on which artifact leads, say "PANEL SPLIT" explicitly and state in one paragraph what the disagreement means — which delivers more, which reaches further, and why you called the overall as you did. Do not resolve a split by summing.
- If any adjacent pair in the overall ranking is within noise, say "SELF-FLAG: CLOSE <labels>" explicitly.

FINALLY — THE SPREAD (this flight's distinctive question):
- In one paragraph: how FAR apart are these artifacts? Are they three attempts of the same quality, or is there a real gap? Name the single largest difference you observed and say whether it looks like a difference of ambition, of execution, or of how much got finished.
- These artifacts may share an author. If they do, model-innate cannot explain any difference between them — so for each decisive difference, say whether it reads as interaction (how the author worked with its build harness), framework-coupled, or simply how far that run got. Do not speculate about WHO the author is; only about WHAT KIND of difference you are seeing.

Your final message must contain: every per-artifact record (premise line first), the ten axis rankings with justifications, both panel orders, the overall ranking, the spread paragraph, and any PANEL SPLIT or CLOSE flag.
