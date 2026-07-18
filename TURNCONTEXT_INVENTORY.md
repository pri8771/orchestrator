# TurnContext migration inventory (V3 board 2.3a)

GENERATED — do not hand-edit. Regenerate with:
`python3 tests/test_cfg_key_inventory.py`. The companion
gate (tests/test_cfg_key_inventory.py) fails on any NEW
underscore key and on stale allowlist entries.

42 written keys · 59 write sites · 201 read sites across orchestrator.py, visualqa.py, urlfetch.py, uicrawl.py

| key | writes | reads | write sites |
|---|---|---|---|
| `_<dynamic>_%s_sessions` | 1 | 0 | orchestrator.py:1570 |
| `_agent_health` | 3 | 0 | orchestrator.py:4112; orchestrator.py:5151; orchestrator.py:7842 |
| `_agent_role_overrides` | 1 | 1 | orchestrator.py:7874 |
| `_app_dir` | 1 | 6 | orchestrator.py:7817 |
| `_autonomy` | 2 | 1 | orchestrator.py:7841; orchestrator.py:7865 |
| `_base_models` | 1 | 1 | orchestrator.py:7728 |
| `_base_resolved` | 1 | 1 | orchestrator.py:7729 |
| `_budget` | 1 | 4 | orchestrator.py:8041 |
| `_checked_any_agent_runnable` | 1 | 1 | orchestrator.py:7213 |
| `_claude_sessions` | 2 | 0 | orchestrator.py:5152; orchestrator.py:6424 |
| `_codex_sessions` | 2 | 0 | orchestrator.py:5153; orchestrator.py:6425 |
| `_completeness` | 1 | 1 | orchestrator.py:7855 |
| `_deadline` | 2 | 13 | orchestrator.py:8042; orchestrator.py:8052 |
| `_explicit_app` | 1 | 2 | orchestrator.py:8996 |
| `_gemini_disabled_reason` | 1 | 5 | orchestrator.py:8545 |
| `_gemini_unavailable` | 1 | 2 | orchestrator.py:915 |
| `_installed_ollama_models` | 1 | 4 | orchestrator.py:3672 |
| `_iter_verify_toolchain_absent` | 1 | 2 | orchestrator.py:4612 |
| `_noted_local_active_limit` | 1 | 1 | orchestrator.py:3762 |
| `_noted_local_lane_skip` | 1 | 1 | orchestrator.py:3901 |
| `_noted_local_ram_gate` | 1 | 1 | orchestrator.py:3741 |
| `_noted_ollama_sprint_skip` | 1 | 1 | orchestrator.py:3709 |
| `_noted_ollama_uninstalled_skip` | 1 | 1 | orchestrator.py:3719 |
| `_original_prompt` | 1 | 1 | orchestrator.py:7831 |
| `_personalities` | 1 | 1 | orchestrator.py:7873 |
| `_phase_deadline` | 5 | 13 | orchestrator.py:6105; orchestrator.py:8043; orchestrator.py:8124; orchestrator.py:8126; orchestrator.py:8128 |
| `_prior_discussions` | 1 | 1 | orchestrator.py:8110 |
| `_resolved` | 4 | 14 | orchestrator.py:7731; orchestrator.py:7736; orchestrator.py:7740; orchestrator.py:8525 |
| `_role_by_id` | 1 | 1 | orchestrator.py:7877 |
| `_roles` | 1 | 2 | orchestrator.py:7873 |
| `_round_multiplier` | 2 | 1 | orchestrator.py:7840; orchestrator.py:7854 |
| `_routing` | 2 | 4 | orchestrator.py:5145; orchestrator.py:5564 |
| `_sim_ctx` | 1 | 1 | visualqa.py:368 |
| `_state` | 1 | 2 | orchestrator.py:7818 |
| `_target_path` | 2 | 11 | orchestrator.py:7880; orchestrator.py:7888 |
| `_target_paths` | 1 | 5 | orchestrator.py:7885 |
| `_tech_stack_block` | 1 | 2 | orchestrator.py:7995 |
| `_url_context` | 2 | 5 | orchestrator.py:7803; orchestrator.py:8027 |
| `_warned_no_git_repo` | 1 | 1 | orchestrator.py:7205 |
| `_workflow_name` | 1 | 14 | orchestrator.py:7866 |
| `_workflow_target` | 1 | 11 | orchestrator.py:7867 |
| `_workflow_verify_spec` | 1 | 1 | orchestrator.py:7870 |

Read-only keys (written nowhere in the scanned files —
either dead reads or written via non-subscript paths;
verify before migrating):

- `_allow_writes` — 11 read(s): orchestrator.py:610; orchestrator.py:617; orchestrator.py:631; orchestrator.py:635
- `_build_dir` — 15 read(s): orchestrator.py:610; orchestrator.py:611; orchestrator.py:612; orchestrator.py:4576
- `_claude_model_override` — 2 read(s): orchestrator.py:678; orchestrator.py:1152
- `_drop_prior_discussions` — 1 read(s): orchestrator.py:1832
- `_health_key` — 2 read(s): orchestrator.py:1252; orchestrator.py:1381
- `_knowledge` — 3 read(s): orchestrator.py:1860; orchestrator.py:6409; orchestrator.py:6411
- `_noted_indep_grader` — 1 read(s): orchestrator.py:5921
- `_phase_exemplar` — 1 read(s): orchestrator.py:1845
- `_phase_instructions` — 1 read(s): orchestrator.py:1855
- `_phase_key` — 3 read(s): orchestrator.py:943; orchestrator.py:951; orchestrator.py:3787
- `_phase_playbook` — 2 read(s): orchestrator.py:1842; orchestrator.py:6397
- `_prior_disc_cap` — 2 read(s): orchestrator.py:1812; orchestrator.py:1813
- `_read_dir` — 3 read(s): orchestrator.py:617; orchestrator.py:618; orchestrator.py:619
- `_role_routing` — 1 read(s): orchestrator.py:5259
- `_routed_rounds` — 2 read(s): orchestrator.py:6339; orchestrator.py:6340
- `_routed_turn_timeout` — 1 read(s): orchestrator.py:6352
- `_session` — 3 read(s): orchestrator.py:637; orchestrator.py:687; orchestrator.py:1583
- `_session_cwd` — 2 read(s): orchestrator.py:723; orchestrator.py:724
- `_target_digest` — 3 read(s): orchestrator.py:1872; orchestrator.py:6454; orchestrator.py:6461
- `_turn_timeout` — 1 read(s): orchestrator.py:1353
- `_verify_context` — 1 read(s): orchestrator.py:1877
