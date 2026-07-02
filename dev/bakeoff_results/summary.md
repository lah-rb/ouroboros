# Curator text bake-off

| model | parse(rev) | parse(pack) | gold | grounding | new-key rate | near-dups | needle | wall |
|---|---|---|---|---|---|---|---|---|
| gemma-4-31b | 1.0 | 1.0 | 0.9 | 0.9317 | 0.532 | 1 | 1.0 | 63.8m |
| devstral-2-small-24b | 1.0 | 1.0 | 0.9 | 0.8303 | 0.421 | 1 | 1.0 | 48.7m |
| gpt-oss-120b-a5 | 1.0 | 1.0 | 0.8 | 0.9492 | 0.803 | 0 | 1.0 | 17.2m |
| qwen3-next-coder-80b-a3 | 1.0 | 1.0 | 0.8 | 0.894 | 0.908 | 4 | 1.0 | 13.5m |
| qwen3.6-27b | 0.9 | 1.0 | 0.778 | 0.9358 | 0.868 | 4 | None | 84.2m |

## M6 decision (2026-07-02)

**Vision: Qwen3-VL-8B-Instruct-8bit** — best faithfulness/overlap (0.80)
at 10.5 s/fig; 30B pays 60% more time for no overlap gain; 4B weaker;
qwen3.6-mtmd works but 37 s/fig with think-blocks; gemma-4 blocked by
system CLI age.

**Text — the trade:**
- gemma-4-31b: best balanced quality (0.90 gold, 0.93 grounding, best
  key discipline) but 6.4 min/paper.
- gpt-oss-120b-a5: best grounding (0.949), ZERO near-dup coinage,
  perfect needle, 1.7 min/paper (3.7x gemma), and the model every tier
  (resident cache, snapshot stress) is validated on. Weakness: strictest
  reviewer (0.80 gold = two over-denies; recoverable — denials carry
  reasons and can be revisited).
- qwen3-next: fastest but worst vocabulary discipline (4 near-dups).
- qwen3.6-27b: reproducible degeneration on one review turn
  (repetition guard, run-length 48) + slowest + weakest gold — out.
- devstral: weakest grounding (0.83 — invents/converts values) — out.

**Recommendation: gpt-oss-120b-a5 text + Qwen3-VL-8B vision** (speed +
grounding + infrastructure maturity), with gemma-4-31b as the quality
alternate if the live deny-rate runs hot.
