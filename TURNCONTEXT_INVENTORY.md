# TurnContext migration inventory (V3 board 2.3a)

GENERATED — do not hand-edit. Regenerate with:
`python3 tests/test_cfg_key_inventory.py`. The companion
gate (tests/test_cfg_key_inventory.py) fails on any NEW
underscore key and on stale allowlist entries.

64 written keys · 120 write sites · 205 read sites across orchestrator.py, visualqa.py, urlfetch.py, uicrawl.py

| key | writes | reads | write sites |
|---|---|---|---|
| `_<dynamic>_%s_sessions` | 1 | 0 | orchestrator.py:1574 |
| `_agent_health` | 3 | 0 | orchestrator.py:4101; orchestrator.py:5141; orchestrator.py:7830 |
| `_agent_role_overrides` | 1 | 1 | orchestrator.py:7862 |
| `_allow_writes` | 2 | 11 | orchestrator.py:6397; orchestrator.py:6646 |
| `_app_dir` | 1 | 6 | orchestrator.py:7805 |
| `_autonomy` | 2 | 1 | orchestrator.py:7829; orchestrator.py:7853 |
| `_base_models` | 1 | 1 | orchestrator.py:7716 |
| `_base_resolved` | 1 | 1 | orchestrator.py:7717 |
| `_budget` | 1 | 4 | orchestrator.py:8028 |
| `_build_dir` | 5 | 13 | orchestrator.py:610; orchestrator.py:4754; orchestrator.py:6398; orchestrator.py:6416; orchestrator.py:6647 |
| `_checked_any_agent_runnable` | 1 | 1 | orchestrator.py:7201 |
| `_claude_model_override` | 3 | 2 | orchestrator.py:1163; orchestrator.py:4927; orchestrator.py:5318 |
| `_claude_sessions` | 2 | 0 | orchestrator.py:5142; orchestrator.py:6404 |
| `_codex_sessions` | 2 | 0 | orchestrator.py:5143; orchestrator.py:6405 |
| `_completeness` | 1 | 1 | orchestrator.py:7843 |
| `_deadline` | 2 | 13 | orchestrator.py:8029; orchestrator.py:8039 |
| `_drop_prior_discussions` | 1 | 1 | orchestrator.py:4699 |
| `_explicit_app` | 1 | 2 | orchestrator.py:8983 |
| `_gemini_disabled_reason` | 1 | 5 | orchestrator.py:8532 |
| `_gemini_unavailable` | 1 | 2 | orchestrator.py:914 |
| `_health_key` | 6 | 2 | orchestrator.py:1259; orchestrator.py:4750; orchestrator.py:4871; orchestrator.py:5392; orchestrator.py:5588; orchestrator.py:5997 |
| `_installed_ollama_models` | 1 | 4 | orchestrator.py:3664 |
| `_iter_verify_toolchain_absent` | 1 | 2 | orchestrator.py:4601 |
| `_knowledge` | 4 | 3 | orchestrator.py:5658; orchestrator.py:6379; orchestrator.py:6384; orchestrator.py:6651 |
| `_new_session_id` | 3 | 0 | orchestrator.py:671; orchestrator.py:1586; orchestrator.py:1598 |
| `_noted_indep_grader` | 1 | 1 | orchestrator.py:5906 |
| `_noted_local_active_limit` | 1 | 1 | orchestrator.py:3754 |
| `_noted_local_lane_skip` | 1 | 1 | orchestrator.py:3893 |
| `_noted_local_ram_gate` | 1 | 1 | orchestrator.py:3733 |
| `_noted_ollama_sprint_skip` | 1 | 1 | orchestrator.py:3701 |
| `_noted_ollama_uninstalled_skip` | 1 | 1 | orchestrator.py:3711 |
| `_original_prompt` | 1 | 1 | orchestrator.py:7819 |
| `_personalities` | 1 | 1 | orchestrator.py:7861 |
| `_phase_deadline` | 5 | 13 | orchestrator.py:6089; orchestrator.py:8030; orchestrator.py:8111; orchestrator.py:8113; orchestrator.py:8115 |
| `_phase_exemplar` | 1 | 1 | orchestrator.py:6374 |
| `_phase_instructions` | 1 | 1 | orchestrator.py:5229 |
| `_phase_key` | 1 | 3 | orchestrator.py:5145 |
| `_phase_playbook` | 3 | 2 | orchestrator.py:5657; orchestrator.py:6375; orchestrator.py:6650 |
| `_prior_disc_cap` | 2 | 2 | orchestrator.py:6413; orchestrator.py:6649 |
| `_prior_discussions` | 1 | 1 | orchestrator.py:8097 |
| `_read_dir` | 5 | 3 | orchestrator.py:6432; orchestrator.py:6439; orchestrator.py:6445; orchestrator.py:6652; orchestrator.py:7870 |
| `_resolved` | 5 | 17 | orchestrator.py:1168; orchestrator.py:5230; orchestrator.py:5273; orchestrator.py:7719; orchestrator.py:8512 |
| `_role_by_id` | 1 | 1 | orchestrator.py:7865 |
| `_role_routing` | 1 | 1 | orchestrator.py:5234 |
| `_roles` | 1 | 2 | orchestrator.py:7861 |
| `_round_multiplier` | 2 | 1 | orchestrator.py:7828; orchestrator.py:7842 |
| `_routed_rounds` | 1 | 2 | orchestrator.py:5225 |
| `_routed_turn_timeout` | 1 | 1 | orchestrator.py:5221 |
| `_routing` | 1 | 5 | orchestrator.py:5551 |
| `_session` | 6 | 3 | orchestrator.py:1248; orchestrator.py:1578; orchestrator.py:1583; orchestrator.py:1594; orchestrator.py:1597; orchestrator.py:5587 |
| `_session_cwd` | 3 | 2 | orchestrator.py:6409; orchestrator.py:6411; orchestrator.py:6648 |
| `_sim_ctx` | 1 | 1 | visualqa.py:368 |
| `_state` | 1 | 2 | orchestrator.py:7806 |
| `_target_digest` | 5 | 5 | orchestrator.py:6431; orchestrator.py:6436; orchestrator.py:6444; orchestrator.py:6653; orchestrator.py:7869 |
| `_target_path` | 2 | 11 | orchestrator.py:7868; orchestrator.py:7875 |
| `_target_paths` | 1 | 5 | orchestrator.py:7872 |
| `_tech_stack_block` | 1 | 2 | orchestrator.py:7982 |
| `_turn_timeout` | 2 | 1 | orchestrator.py:6332; orchestrator.py:8031 |
| `_url_context` | 2 | 5 | orchestrator.py:7791; orchestrator.py:8014 |
| `_verify_context` | 4 | 1 | orchestrator.py:5659; orchestrator.py:6362; orchestrator.py:6368; orchestrator.py:6654 |
| `_warned_no_git_repo` | 1 | 1 | orchestrator.py:7193 |
| `_workflow_name` | 1 | 14 | orchestrator.py:7854 |
| `_workflow_target` | 1 | 11 | orchestrator.py:7855 |
| `_workflow_verify_spec` | 1 | 1 | orchestrator.py:7858 |
