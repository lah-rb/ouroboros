"""GAIA benchmark adapter — runs Ouroboros ops missions against GAIA questions.

Mirrors adapters.swe's shape (loader / runner / scorer / run_gaia CLI) with two
deliberate differences: missions run on the HOST via LocalEffects in a scratch
workspace (no per-question container), and web_research is ON — GAIA is a
web-research benchmark, the production validation of the deep_search loop.

Imports are kept lazy (submodules import their own deps) so importing the
package never requires datasets/huggingface_hub until a loader is used.
"""
