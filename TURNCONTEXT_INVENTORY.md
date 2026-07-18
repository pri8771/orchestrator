# TurnContext migration inventory (V3 board 2.3a)

GENERATED — do not hand-edit. Regenerate with:
`python3 tests/test_cfg_key_inventory.py`. The companion
gate (tests/test_cfg_key_inventory.py) fails on any NEW
underscore key and on stale allowlist entries.

0 written keys · 0 write sites · 201 read sites across orchestrator.py, visualqa.py, urlfetch.py, uicrawl.py

| key | writes | reads | write sites |
|---|---|---|---|

Read-only keys (written nowhere in the scanned files —
either dead reads or written via non-subscript paths;
verify before migrating):

- `_agent_role_overrides` — 1 read(s): orchestrator.py:6371
- `_allow_writes` — 11 read(s): orchestrator.py:610; orchestrator.py:617; orchestrator.py:631; orchestrator.py:635
- `_app_dir` — 6 read(s): orchestrator.py:1136; orchestrator.py:1139; orchestrator.py:1174; orchestrator.py:1825
- `_autonomy` — 1 read(s): orchestrator.py:6939
- `_base_models` — 1 read(s): orchestrator.py:7735
- `_base_resolved` — 1 read(s): orchestrator.py:7736
- `_budget` — 4 read(s): orchestrator.py:3705; orchestrator.py:5046; orchestrator.py:6105; orchestrator.py:6352
- `_build_dir` — 15 read(s): orchestrator.py:610; orchestrator.py:611; orchestrator.py:612; orchestrator.py:4576
- `_checked_any_agent_runnable` — 1 read(s): orchestrator.py:7213
- `_claude_model_override` — 2 read(s): orchestrator.py:678; orchestrator.py:1151
- `_completeness` — 1 read(s): orchestrator.py:7462
- `_deadline` — 13 read(s): orchestrator.py:1097; orchestrator.py:1118; orchestrator.py:1367; orchestrator.py:4597
- `_drop_prior_discussions` — 1 read(s): orchestrator.py:1831
- `_explicit_app` — 2 read(s): orchestrator.py:7197; orchestrator.py:7950
- `_gemini_disabled_reason` — 5 read(s): orchestrator.py:3770; orchestrator.py:8006; orchestrator.py:8010; orchestrator.py:8505
- `_gemini_unavailable` — 2 read(s): orchestrator.py:823; orchestrator.py:824
- `_health_key` — 2 read(s): orchestrator.py:1251; orchestrator.py:1380
- `_installed_ollama_models` — 4 read(s): orchestrator.py:3666; orchestrator.py:3670; orchestrator.py:3673; orchestrator.py:3866
- `_iter_verify_toolchain_absent` — 2 read(s): orchestrator.py:4572; orchestrator.py:4577
- `_knowledge` — 3 read(s): orchestrator.py:1859; orchestrator.py:6410; orchestrator.py:6412
- `_noted_indep_grader` — 1 read(s): orchestrator.py:5922
- `_noted_local_active_limit` — 1 read(s): orchestrator.py:3761
- `_noted_local_lane_skip` — 1 read(s): orchestrator.py:3899
- `_noted_local_ram_gate` — 1 read(s): orchestrator.py:3740
- `_noted_ollama_sprint_skip` — 1 read(s): orchestrator.py:3708
- `_noted_ollama_uninstalled_skip` — 1 read(s): orchestrator.py:3718
- `_original_prompt` — 1 read(s): orchestrator.py:2392
- `_personalities` — 1 read(s): orchestrator.py:6369
- `_phase_deadline` — 13 read(s): orchestrator.py:1097; orchestrator.py:1118; orchestrator.py:1367; orchestrator.py:4687
- `_phase_exemplar` — 1 read(s): orchestrator.py:1844
- `_phase_instructions` — 1 read(s): orchestrator.py:1854
- `_phase_key` — 3 read(s): orchestrator.py:944; orchestrator.py:950; orchestrator.py:3787
- `_phase_playbook` — 2 read(s): orchestrator.py:1841; orchestrator.py:6398
- `_prior_disc_cap` — 2 read(s): orchestrator.py:1811; orchestrator.py:1812
- `_prior_discussions` — 1 read(s): orchestrator.py:1830
- `_read_dir` — 3 read(s): orchestrator.py:617; orchestrator.py:618; orchestrator.py:619
- `_resolved` — 14 read(s): orchestrator.py:629; orchestrator.py:678; orchestrator.py:834; orchestrator.py:890
- `_role_by_id` — 1 read(s): orchestrator.py:6371
- `_role_routing` — 1 read(s): orchestrator.py:5259
- `_roles` — 2 read(s): orchestrator.py:6370; orchestrator.py:7883
- `_round_multiplier` — 1 read(s): orchestrator.py:6346
- `_routed_rounds` — 2 read(s): orchestrator.py:6340; orchestrator.py:6341
- `_routed_turn_timeout` — 1 read(s): orchestrator.py:6353
- `_routing` — 4 read(s): orchestrator.py:1094; orchestrator.py:1115; orchestrator.py:3788; orchestrator.py:5143
- `_session` — 3 read(s): orchestrator.py:637; orchestrator.py:687; orchestrator.py:1582
- `_session_cwd` — 2 read(s): orchestrator.py:723; orchestrator.py:724
- `_sim_ctx` — 1 read(s): uicrawl.py:272
- `_state` — 2 read(s): orchestrator.py:1170; orchestrator.py:1173
- `_target_digest` — 3 read(s): orchestrator.py:1871; orchestrator.py:6455; orchestrator.py:6462
- `_target_path` — 11 read(s): orchestrator.py:6272; orchestrator.py:6276; orchestrator.py:6456; orchestrator.py:6457
- `_target_paths` — 5 read(s): orchestrator.py:6224; orchestrator.py:6449; orchestrator.py:7893; orchestrator.py:7893
- `_tech_stack_block` — 2 read(s): orchestrator.py:1847; orchestrator.py:7999
- `_turn_timeout` — 1 read(s): orchestrator.py:1352
- `_url_context` — 5 read(s): orchestrator.py:1867; orchestrator.py:3010; orchestrator.py:7776; urlfetch.py:24
- `_verify_context` — 1 read(s): orchestrator.py:1876
- `_warned_no_git_repo` — 1 read(s): orchestrator.py:7204
- `_workflow_name` — 14 read(s): orchestrator.py:1228; orchestrator.py:4602; orchestrator.py:4618; orchestrator.py:5015
- `_workflow_target` — 11 read(s): orchestrator.py:1939; orchestrator.py:2046; orchestrator.py:2147; orchestrator.py:2351
- `_workflow_verify_spec` — 1 read(s): orchestrator.py:4584
