# FLIGHT_PROMPT — the canonical blind comparative judge prompt

Fill `{PACKET_ROOT}` and send the block below **verbatim** to one freshly
spawned subagent (claude-opus-5 unless the record says otherwise). Do not
compose a flight prompt from memory: the v2.0 campaign's eight flights were
prompted from chat, and every one of them silently omitted the documentation
change `TIER_RUBRIC_v2.md` had already scheduled.

Build the two packets first, one per artifact:

    python3 dev/blind_panel/make_judge_packet.py <staged>/alpha --out {PACKET_ROOT}/A
    python3 dev/blind_panel/make_judge_packet.py <other>/alpha  --out {PACKET_ROOT}/B

Keep the A/B key OUTSIDE the packet root, and vary which side the candidate
sits on across flights — the assignment is also the position-bias check.

---

You are a BLIND COMPARATIVE JUDGE for two anonymous software artifacts. Your final message is the official record.

PACKET ROOT: {PACKET_ROOT}
- Artifact A: <packet>/A/artifact/    Artifact B: <packet>/B/artifact/
- The rubric and checklist are at <packet>/A/RUBRIC.md and <packet>/A/CHECKLIST.md (B's copies are identical). Read BOTH documents fully before touching either artifact.

HARD WALLS (violating any of these invalidates the flight):
- Read and write ONLY inside the packet root and your scratch dir: <packet>/scratch/ (create it; give each artifact its own subdir for play so save files cannot collide — copy an artifact into scratch before playing if it writes files into its own tree).
- NEVER read ~/ouroboros-runs/, the ouroboros repo (except the single interpreter binary below), any config, log, trace, or git anything.
- Do not try to identify which model or system produced either artifact. Do not guess.
- Interpreter: use /Users/lah-rb/Repos/ouroboros/.venv/bin/python to RUN the games. That path is allowed ONLY as an interpreter.

PROTOCOL — play first, in this order:
1. Read RUBRIC.md and CHECKLIST.md fully. Note it is TIER_RUBRIC v2.1: TEN forced choices in TWO panels, tallied separately and never summed.
2. PLAY artifact A, then artifact B (interactive: drive real input through the program's own command line; iterate on what it actually accepts; push toward the WIN CONDITION). Source reading is allowed ONLY to explain a failure you already observed in play, never as a substitute for play.
3. Robustness probes on each: unknown commands, empty input, invalid moves, mid-combat oddities, EOF/Ctrl-D. Distinguish clean refusal from traceback from silent misinterpretation.
4. MODIFICATION PROBE on each (mandatory, feeds axis B9): add a ninth room, or change a weapon's damage, WITHOUT touching unrelated code. Report exactly what you had to touch and what broke.
5. Per artifact, record:
   - a PREMISE LINE — two sentences on what this game IS, in your own words, plus one quoted line of its prose. This is never ranked; it exists so the record says what the artifact was, not only how it worked.
   - completability class (WON / WINNABLE-NOT-WON / UNWINNABLE / NO-TERMINAL-STATES — playing toward the win is mandatory)
   - the conformance tally per CHECKLIST.md (met/47, unmet item numbers, then the binary verdict with its trigger)
   - a state-integrity note, the robustness table, the modification-probe result, the furthest point reached
   - a quoted transcript excerpt for every decisive finding
   - attribution labels: model-innate / interaction / framework-coupled. At this brief size, framework-coupled means PROJECT LAYOUT ONLY — documentation IS in comparison (axis B10). Framework-coupled observations are recorded and excluded from the comparison.

FLIGHT VERDICT — ten forced choices, A or B, one line of justification each. Per-axis ties are FORBIDDEN.

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

Then report the two panel tallies SEPARATELY (e.g. "Delivery: A 4-0 · Character: B 5-1") and make ONE overall forced choice (A / B / TIE — overall tie allowed).

If the panels disagree on direction, say "PANEL SPLIT" explicitly and state in one paragraph what the disagreement means: which artifact delivers more, which reaches further, and why you called the overall as you did. Do not resolve a split by summing.

If the overall is within noise, say "SELF-FLAG: CLOSE" explicitly.

Your final message must contain: both per-artifact records (premise line first), the ten axis choices with justifications, both panel tallies, the overall choice, and any PANEL SPLIT or CLOSE flag.
