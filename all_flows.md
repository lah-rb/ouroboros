# Ouroboros Flow Diagrams

## System View

```mermaid
flowchart TD
    %% Ouroboros — Cross-Flow System View

    capture_learnings["capture_learnings\n5 steps"]
    create_file["create_file\n10 steps"]
    create_plan["create_plan\n7 steps"]
    create_tests["create_tests\n12 steps"]
    design_architecture["design_architecture\n7 steps"]
    diagnose_issue["diagnose_issue\n13 steps"]
    document_project["document_project\n16 steps"]
    explore_spike["explore_spike\n9 steps"]
    integrate_modules["integrate_modules\n15 steps"]
    manage_packages["manage_packages\n7 steps"]
    mission_control["mission_control\n16 steps"]
    modify_file["modify_file\n15 steps"]
    prepare_context["prepare_context\n11 steps"]
    quality_gate["quality_gate\n9 steps"]
    refactor["refactor\n19 steps"]
    request_review["request_review\n8 steps"]
    research_codebase_history["research_codebase_history\n4 steps"]
    research_context["research_context\n11 steps"]
    research_repomap["research_repomap\n3 steps"]
    research_technical["research_technical\n4 steps"]
    retrospective["retrospective\n11 steps"]
    revise_plan["revise_plan\n6 steps"]
    run_in_terminal["run_in_terminal\n7 steps"]
    setup_project["setup_project\n8 steps"]
    test_branching["test_branching\n6 steps"]
    test_inference["test_inference\n6 steps"]
    test_simple["test_simple\n4 steps"]
    validate_behavior["validate_behavior\n10 steps"]
    validate_output["validate_output\n8 steps"]

    %% Tail-call edges
    create_file -.->|complete| mission_control
    create_file -.->|failed| mission_control
    create_plan -.->|complete| mission_control
    create_tests -.->|complete| mission_control
    create_tests -.->|failed| mission_control
    design_architecture -.->|complete| mission_control
    design_architecture -.->|failed| mission_control
    diagnose_issue -.->|complete| mission_control
    diagnose_issue -.->|diagnosis_failed| mission_control
    document_project -.->|complete| mission_control
    document_project -.->|documentation_adequate| mission_control
    document_project -.->|failed| mission_control
    explore_spike -.->|complete| mission_control
    integrate_modules -.->|complete| mission_control
    integrate_modules -.->|already_integrated| mission_control
    integrate_modules -.->|nothing_to_integrate| mission_control
    integrate_modules -.->|too_complex| mission_control
    integrate_modules -.->|failed| mission_control
    manage_packages -.->|complete| mission_control
    manage_packages -.->|failed| mission_control
    mission_control -.->|dispatch_retrospective| retrospective
    mission_control -.->|invoke_quality_fix| mission_control
    dynamic_dispatch(("dynamic\ndispatch"))
    style dynamic_dispatch fill:#f9f,stroke:#333
    mission_control -.->|dispatch| dynamic_dispatch
    mission_control -.->|dispatch_planning| create_plan
    mission_control -.->|idle| mission_control
    modify_file -.->|complete| mission_control
    modify_file -.->|failed| mission_control
    modify_file -.->|abandon| mission_control
    refactor -.->|complete| mission_control
    refactor -.->|code_is_clean| mission_control
    refactor -.->|too_risky| mission_control
    refactor -.->|needs_tests_first| mission_control
    refactor -.->|cannot_refactor| mission_control
    refactor -.->|failed| mission_control
    request_review -.->|changes_needed| mission_control
    request_review -.->|major_rework| mission_control
    request_review -.->|review_unavailable| mission_control
    retrospective -.->|complete| mission_control
    retrospective -.->|too_early| mission_control
    retrospective -.->|return_to_mission| mission_control
    setup_project -.->|complete| mission_control
    setup_project -.->|failed| mission_control
    validate_behavior -.->|skip_not_ready| mission_control
    validate_behavior -.->|complete| mission_control
    validate_behavior -.->|failed| mission_control

    %% Sub-flow invocation edges
    create_file ==>|gather_context| prepare_context
    create_file ==>|validate| validate_output
    create_file ==>|capture_learnings| capture_learnings
    create_file ==>|capture_failure_note| capture_learnings
    create_plan ==>|gather_context| prepare_context
    create_tests ==>|gather_context| prepare_context
    create_tests ==>|capture_learnings| capture_learnings
    create_tests ==>|capture_failure_note| capture_learnings
    diagnose_issue ==>|gather_context| prepare_context
    diagnose_issue ==>|gather_additional_context| prepare_context
    diagnose_issue ==>|capture_diagnosis_learnings| capture_learnings
    diagnose_issue ==>|capture_failure_note| capture_learnings
    document_project ==>|gather_context| prepare_context
    document_project ==>|verify_no_behavior_change| validate_output
    document_project ==>|capture_learnings| capture_learnings
    document_project ==>|capture_failure_note| capture_learnings
    explore_spike ==>|scan_structure| prepare_context
    explore_spike ==>|external_research| research_context
    explore_spike ==>|capture_findings| capture_learnings
    integrate_modules ==>|gather_context| prepare_context
    integrate_modules ==>|validate| quality_gate
    integrate_modules ==>|diagnose_integration_failure| diagnose_issue
    integrate_modules ==>|capture_learnings| capture_learnings
    integrate_modules ==>|capture_failure_note| capture_learnings
    manage_packages ==>|gather_context| prepare_context
    manage_packages ==>|run_setup| run_in_terminal
    manage_packages ==>|capture_learnings| capture_learnings
    manage_packages ==>|capture_failure_note| capture_learnings
    mission_control ==>|invoke_revise_plan| revise_plan
    mission_control ==>|quality_check| quality_gate
    modify_file ==>|gather_context| prepare_context
    modify_file ==>|validate| validate_output
    modify_file ==>|diagnose_before_retry| diagnose_issue
    modify_file ==>|capture_learnings| capture_learnings
    modify_file ==>|create_fallback| create_file
    modify_file ==>|capture_failure_note| capture_learnings
    prepare_context ==>|research| research_context
    refactor ==>|gather_context| prepare_context
    refactor ==>|capture_learnings| capture_learnings
    refactor ==>|capture_failure_note| capture_learnings
    request_review ==>|gather_review_context| prepare_context
    request_review ==>|approved| capture_learnings
    research_context ==>|route_repomap| research_repomap
    research_context ==>|route_history| research_codebase_history
    research_context ==>|route_technical| research_technical
    retrospective ==>|capture_learnings| capture_learnings
    retrospective ==>|no_changes_needed| capture_learnings
    setup_project ==>|capture_learnings| capture_learnings
    setup_project ==>|capture_failure_note| capture_learnings
    validate_behavior ==>|gather_context| prepare_context
    validate_behavior ==>|run_tests| run_in_terminal
    validate_behavior ==>|capture_learnings| capture_learnings
    validate_behavior ==>|capture_failure_note| capture_learnings

    style mission_control fill:#ddf,stroke:#339,stroke-width:3px
```
