"""ingest_workspace — the brownfield ADOPTION entry for a foreign repo.

code_core's other entry (design_and_plan) designs greenfield, which is wrong
for a project that already exists (a tb task container). ingest_workspace reads
the existing code into mission.architecture WITHOUT redesigning, then hands off
to mission_control so the pending_directive drives the repair sweep.

Pins: the compiled wiring (scan → repomap → extract → parse → [research] →
handoff to mission_control), the config-gated research branch, and the new
MissionConfig.web_research flag the tb adapter turns off for hermetic runs.
"""

from __future__ import annotations


from agent.persistence.models import ArchitectureState, MissionConfig
from tests.conftest import compiled_flows as _compiled


def test_ingest_workspace_compiled_wiring():
    c = _compiled()
    assert "ingest_workspace" in c
    flow = c["ingest_workspace"]
    assert flow["entry"] == "load_mission"
    steps = flow["steps"]

    # Read the existing workspace, then EXTRACT (not design) its architecture.
    assert (
        steps["load_mission"]["resolver"]["rules"][0]["transition"] == "scan_workspace"
    )
    assert steps["scan_workspace"]["resolver"]["rules"][0]["transition"] == (
        "build_repomap"
    )
    assert (
        steps["build_repomap"]["resolver"]["rules"][0]["transition"]
        == "extract_architecture"
    )
    # The one new prompt — an extract-don't-redesign variant of design_architecture.
    assert (
        steps["extract_architecture"]["prompt_template"]["template"]
        == "design_and_plan/extract_architecture"
    )
    assert steps["extract_architecture"]["action"] == "inference"

    # Parse/store, then optionally research — research ONLY when web_research is on.
    parse_rules = steps["parse_architecture"]["resolver"]["rules"]
    pt = [(r["condition"], r["transition"]) for r in parse_rules]
    # First rule gates research on BOTH a successful parse and the config flag.
    cond, dest = pt[0]
    assert dest == "domain_research"
    assert "architecture_parsed == true" in cond
    assert "config.web_research == true" in cond
    # Fallback (no research / parse failed) goes straight to the director handoff.
    assert pt[-1] == ("true", "handoff")


def test_ingest_workspace_hands_off_to_mission_control():
    c = _compiled()
    steps = c["ingest_workspace"]["steps"]
    # The flow does NOT derive build-everything goals — it transfers control to
    # the director, where the pending_directive drives replan/repair.
    handoff = steps["handoff"]
    assert handoff["tail_call"]["flow"] == "mission_control"
    assert handoff["tail_call"]["input_map"]["last_status"] == "success"
    # No greenfield goal derivation in this flow.
    assert "derive_goals" not in steps
    assert all(s.get("action") != "derive_project_goals" for s in steps.values())


def test_research_branch_returns_to_handoff():
    c = _compiled()
    steps = c["ingest_workspace"]["steps"]
    dr = {
        r["condition"]: r["transition"]
        for r in steps["domain_research"]["resolver"]["rules"]
    }
    assert dr["result.status == 'success'"] == "save_research"
    assert dr["true"] == "handoff"
    assert steps["save_research"]["resolver"]["rules"][0]["transition"] == "handoff"


def test_pending_directive_routes_to_replan_before_greenfield_planning():
    # A freshly ingested mission has architecture but no goals yet, which trips
    # needs_plan. The pending_directive rule must intercept FIRST and route to
    # the phase router (whose first phase is replan) — otherwise design_and_plan
    # runs greenfield ahead of the directive (the ingest_workspace → replan path).
    c = _compiled()
    rules = c["mission_control"]["steps"]["apply_last_result"]["resolver"]["rules"]
    conds = [r["condition"] for r in rules]
    # The pending_directive guard exists and routes to check_phase...
    pend = next((r for r in rules if "pending_directive" in r["condition"]), None)
    assert pend is not None and pend["transition"] == "check_phase"
    # ...and it precedes the needs_plan → dispatch_planning (greenfield) rule.
    pend_i = conds.index(pend["condition"])
    needs_i = next(i for i, ct in enumerate(conds) if "needs_plan" in ct)
    assert pend_i < needs_i


def test_design_and_plan_research_is_web_research_gated():
    # Proactive domain research only fires when config.web_research is on, so a
    # hermetic run (tb adapter sets it off) stays fully offline. In the design_gate
    # topology the research fork lives on design_gate_pass (post-coherence, the old
    # parse_architecture fork) and design_gate_route (pre-design routing) — both
    # web_research-gated, else → derive_goals.
    c = _compiled()
    steps = c["design_and_plan"]["steps"]
    for step in ("design_gate_pass", "design_gate_route"):
        rules = steps[step]["resolver"]["rules"]
        research_rule = next(
            (r for r in rules if r["transition"] == "domain_research"), None
        )
        assert research_rule is not None, f"{step} lost its research fork"
        assert "config.web_research == true" in research_rule["condition"]
        assert rules[-1]["transition"] == "derive_goals"


def test_web_research_flag_default_on_off_is_explicit():
    # Default ON preserves greenfield/ingest grounding; the tb adapter sets it
    # OFF explicitly for hermetic, comparison-clean runs.
    assert MissionConfig(working_directory="/x").web_research is True
    assert (
        MissionConfig(working_directory="/x", web_research=False).web_research is False
    )


def test_ingest_scan_covers_shell_and_non_python_sources():
    # The default scan pattern set is Python-centric; a bash project scanned
    # with it yields a 0-file manifest -> the extractor sees nothing -> emits an
    # empty/invalid architecture. ingest_workspace scans broadly to avoid this.
    c = _compiled()
    pats = c["ingest_workspace"]["steps"]["scan_workspace"]["params"][
        "include_patterns"
    ]
    for ext in ("*.sh", "*.py", "*.js", "*.go", "*.rs", "*.yaml", "*.json"):
        assert ext in pats, f"{ext} missing from ingest scan patterns"


def test_import_scheme_coerces_out_of_vocabulary_to_flat():
    # A no-imports project (shell scripts, single file) makes the LLM emit ""
    # or "none"/"bash" for import_scheme — out of the strict Literal. Coerce to
    # flat rather than crash parse_and_store_architecture mid-cycle.
    for bad in ("", "none", "bash", "shell", None, ["x"], "FLAT"):
        assert ArchitectureState(import_scheme=bad).import_scheme == "flat"
    assert ArchitectureState(import_scheme="package").import_scheme == "package"
    assert ArchitectureState(import_scheme="relative").import_scheme == "relative"
