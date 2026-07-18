# TurnContext migration inventory (V3 board 2.3a)

GENERATED — do not hand-edit. Regenerate with:
`python3 tests/test_cfg_key_inventory.py`. The companion
gate (tests/test_cfg_key_inventory.py) fails on any NEW
underscore key and on stale allowlist entries.

42 written keys · 56 write sites · 204 read sites across orchestrator.py, visualqa.py, urlfetch.py, uicrawl.py

| key | writes | reads | write sites |
|---|---|---|---|
| `_<dynamic>_%s_sessions` | 1 | 0 | orchestrator.py:1570 |
| `_agent_health` | 3 | 0 | orchestrator.py:4100; orchestrator.py:5139; orchestrator.py:7830 |
| `_agent_role_overrides` | 1 | 1 | orchestrator.py:7862 |
| `_app_dir` | 1 | 6 | orchestrator.py:7805 |
| `_autonomy` | 2 | 1 | orchestrator.py:7829; orchestrator.py:7853 |
| `_base_models` | 1 | 1 | orchestrator.py:7716 |
| `_base_resolved` | 1 | 1 | orchestrator.py:7717 |
| `_budget` | 1 | 4 | orchestrator.py:8029 |
| `_checked_any_agent_runnable` | 1 | 1 | orchestrator.py:7201 |
| `_claude_sessions` | 2 | 0 | orchestrator.py:5140; orchestrator.py:6412 |
| `_codex_sessions` | 2 | 0 | orchestrator.py:5141; orchestrator.py:6413 |
| `_completeness` | 1 | 1 | orchestrator.py:7843 |
| `_deadline` | 2 | 13 | orchestrator.py:8030; orchestrator.py:8040 |
| `_explicit_app` | 1 | 2 | orchestrator.py:8984 |
| `_gemini_disabled_reason` | 1 | 5 | orchestrator.py:8533 |
| `_gemini_unavailable` | 1 | 2 | orchestrator.py:915 |
| `_installed_ollama_models` | 1 | 4 | orchestrator.py:3663 |
| `_iter_verify_toolchain_absent` | 1 | 2 | orchestrator.py:4600 |
| `_noted_local_active_limit` | 1 | 1 | orchestrator.py:3753 |
| `_noted_local_lane_skip` | 1 | 1 | orchestrator.py:3892 |
| `_noted_local_ram_gate` | 1 | 1 | orchestrator.py:3732 |
| `_noted_ollama_sprint_skip` | 1 | 1 | orchestrator.py:3700 |
| `_noted_ollama_uninstalled_skip` | 1 | 1 | orchestrator.py:3710 |
| `_original_prompt` | 1 | 1 | orchestrator.py:7819 |
| `_personalities` | 1 | 1 | orchestrator.py:7861 |
| `_phase_deadline` | 5 | 13 | orchestrator.py:6093; orchestrator.py:8031; orchestrator.py:8112; orchestrator.py:8114; orchestrator.py:8116 |
| `_prior_discussions` | 1 | 1 | orchestrator.py:8098 |
| `_resolved` | 2 | 16 | orchestrator.py:7719; orchestrator.py:8513 |
| `_role_by_id` | 1 | 1 | orchestrator.py:7865 |
| `_roles` | 1 | 2 | orchestrator.py:7861 |
| `_round_multiplier` | 2 | 1 | orchestrator.py:7828; orchestrator.py:7842 |
| `_routing` | 1 | 5 | orchestrator.py:5552 |
| `_sim_ctx` | 1 | 1 | visualqa.py:368 |
| `_state` | 1 | 2 | orchestrator.py:7806 |
| `_target_path` | 2 | 11 | orchestrator.py:7868; orchestrator.py:7876 |
| `_target_paths` | 1 | 5 | orchestrator.py:7873 |
| `_tech_stack_block` | 1 | 2 | orchestrator.py:7983 |
| `_url_context` | 2 | 5 | orchestrator.py:7791; orchestrator.py:8015 |
| `_warned_no_git_repo` | 1 | 1 | orchestrator.py:7193 |
| `_workflow_name` | 1 | 14 | orchestrator.py:7854 |
| `_workflow_target` | 1 | 11 | orchestrator.py:7855 |
| `_workflow_verify_spec` | 1 | 1 | orchestrator.py:7858 |

Read-only keys (written nowhere in the scanned files —
either dead reads or written via non-subscript paths;
verify before migrating):

- `_allow_writes` — 11 read(s): orchestrator.py:610; orchestrator.py:617; orchestrator.py:631; orchestrator.py:635
- `_build_dir` — 15 read(s): orchestrator.py:610; orchestrator.py:611; orchestrator.py:612; orchestrator.py:4564
- `_claude_model_override` — 2 read(s): orchestrator.py:678; orchestrator.py:1152
- `_drop_prior_discussions` — 1 read(s): orchestrator.py:1832
- `_health_key` — 2 read(s): orchestrator.py:1252; orchestrator.py:1381
- `_knowledge` — 3 read(s): orchestrator.py:1860; orchestrator.py:6397; orchestrator.py:6399
- `_noted_indep_grader` — 1 read(s): orchestrator.py:5909
- `_phase_exemplar` — 1 read(s): orchestrator.py:1845
- `_phase_instructions` — 1 read(s): orchestrator.py:1855
- `_phase_key` — 3 read(s): orchestrator.py:943; orchestrator.py:951; orchestrator.py:3778
- `_phase_playbook` — 2 read(s): orchestrator.py:1842; orchestrator.py:6385
- `_prior_disc_cap` — 2 read(s): orchestrator.py:1812; orchestrator.py:1813
- `_read_dir` — 3 read(s): orchestrator.py:617; orchestrator.py:618; orchestrator.py:619
- `_role_routing` — 1 read(s): orchestrator.py:5247
- `_routed_rounds` — 2 read(s): orchestrator.py:6327; orchestrator.py:6328
- `_routed_turn_timeout` — 1 read(s): orchestrator.py:6340
- `_session` — 3 read(s): orchestrator.py:637; orchestrator.py:687; orchestrator.py:1583
- `_session_cwd` — 2 read(s): orchestrator.py:723; orchestrator.py:724
- `_target_digest` — 3 read(s): orchestrator.py:1872; orchestrator.py:6442; orchestrator.py:6449
- `_turn_timeout` — 1 read(s): orchestrator.py:1353
- `_verify_context` — 1 read(s): orchestrator.py:1877
