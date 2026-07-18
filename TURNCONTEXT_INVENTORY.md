# TurnContext migration inventory (V3 board 2.3a)

GENERATED — do not hand-edit. Regenerate with:
`python3 tests/test_cfg_key_inventory.py`. The companion
gate (tests/test_cfg_key_inventory.py) fails on any NEW
underscore key and on stale allowlist entries.

37 written keys · 49 write sites · 201 read sites across orchestrator.py, visualqa.py, urlfetch.py, uicrawl.py

| key | writes | reads | write sites |
|---|---|---|---|
| `_agent_role_overrides` | 1 | 1 | orchestrator.py:7879 |
| `_app_dir` | 1 | 6 | orchestrator.py:7821 |
| `_autonomy` | 2 | 1 | orchestrator.py:7846; orchestrator.py:7870 |
| `_base_models` | 1 | 1 | orchestrator.py:7732 |
| `_base_resolved` | 1 | 1 | orchestrator.py:7733 |
| `_budget` | 1 | 4 | orchestrator.py:8045 |
| `_checked_any_agent_runnable` | 1 | 1 | orchestrator.py:7217 |
| `_completeness` | 1 | 1 | orchestrator.py:7860 |
| `_deadline` | 2 | 13 | orchestrator.py:8046; orchestrator.py:8056 |
| `_explicit_app` | 1 | 2 | orchestrator.py:9000 |
| `_gemini_disabled_reason` | 1 | 5 | orchestrator.py:8549 |
| `_gemini_unavailable` | 1 | 2 | orchestrator.py:915 |
| `_installed_ollama_models` | 1 | 4 | orchestrator.py:3672 |
| `_iter_verify_toolchain_absent` | 1 | 2 | orchestrator.py:4613 |
| `_noted_local_active_limit` | 1 | 1 | orchestrator.py:3762 |
| `_noted_local_lane_skip` | 1 | 1 | orchestrator.py:3901 |
| `_noted_local_ram_gate` | 1 | 1 | orchestrator.py:3741 |
| `_noted_ollama_sprint_skip` | 1 | 1 | orchestrator.py:3709 |
| `_noted_ollama_uninstalled_skip` | 1 | 1 | orchestrator.py:3719 |
| `_original_prompt` | 1 | 1 | orchestrator.py:7835 |
| `_personalities` | 1 | 1 | orchestrator.py:7878 |
| `_phase_deadline` | 5 | 13 | orchestrator.py:6109; orchestrator.py:8047; orchestrator.py:8128; orchestrator.py:8130; orchestrator.py:8132 |
| `_prior_discussions` | 1 | 1 | orchestrator.py:8114 |
| `_resolved` | 4 | 14 | orchestrator.py:7735; orchestrator.py:7740; orchestrator.py:7744; orchestrator.py:8529 |
| `_role_by_id` | 1 | 1 | orchestrator.py:7882 |
| `_roles` | 1 | 2 | orchestrator.py:7878 |
| `_round_multiplier` | 2 | 1 | orchestrator.py:7845; orchestrator.py:7859 |
| `_sim_ctx` | 1 | 1 | visualqa.py:368 |
| `_state` | 1 | 2 | orchestrator.py:7822 |
| `_target_path` | 2 | 11 | orchestrator.py:7885; orchestrator.py:7892 |
| `_target_paths` | 1 | 5 | orchestrator.py:7889 |
| `_tech_stack_block` | 1 | 2 | orchestrator.py:7999 |
| `_url_context` | 2 | 5 | orchestrator.py:7807; orchestrator.py:8031 |
| `_warned_no_git_repo` | 1 | 1 | orchestrator.py:7209 |
| `_workflow_name` | 1 | 14 | orchestrator.py:7871 |
| `_workflow_target` | 1 | 11 | orchestrator.py:7872 |
| `_workflow_verify_spec` | 1 | 1 | orchestrator.py:7875 |

Read-only keys (written nowhere in the scanned files —
either dead reads or written via non-subscript paths;
verify before migrating):

- `_allow_writes` — 11 read(s): orchestrator.py:610; orchestrator.py:617; orchestrator.py:631; orchestrator.py:635
- `_build_dir` — 15 read(s): orchestrator.py:610; orchestrator.py:611; orchestrator.py:612; orchestrator.py:4577
- `_claude_model_override` — 2 read(s): orchestrator.py:678; orchestrator.py:1152
- `_drop_prior_discussions` — 1 read(s): orchestrator.py:1832
- `_health_key` — 2 read(s): orchestrator.py:1252; orchestrator.py:1381
- `_knowledge` — 3 read(s): orchestrator.py:1860; orchestrator.py:6413; orchestrator.py:6415
- `_noted_indep_grader` — 1 read(s): orchestrator.py:5925
- `_phase_exemplar` — 1 read(s): orchestrator.py:1845
- `_phase_instructions` — 1 read(s): orchestrator.py:1855
- `_phase_key` — 3 read(s): orchestrator.py:943; orchestrator.py:951; orchestrator.py:3787
- `_phase_playbook` — 2 read(s): orchestrator.py:1842; orchestrator.py:6401
- `_prior_disc_cap` — 2 read(s): orchestrator.py:1812; orchestrator.py:1813
- `_read_dir` — 3 read(s): orchestrator.py:617; orchestrator.py:618; orchestrator.py:619
- `_role_routing` — 1 read(s): orchestrator.py:5262
- `_routed_rounds` — 2 read(s): orchestrator.py:6343; orchestrator.py:6344
- `_routed_turn_timeout` — 1 read(s): orchestrator.py:6356
- `_routing` — 4 read(s): orchestrator.py:1095; orchestrator.py:1116; orchestrator.py:3788; orchestrator.py:5144
- `_session` — 3 read(s): orchestrator.py:637; orchestrator.py:687; orchestrator.py:1583
- `_session_cwd` — 2 read(s): orchestrator.py:723; orchestrator.py:724
- `_target_digest` — 3 read(s): orchestrator.py:1872; orchestrator.py:6458; orchestrator.py:6465
- `_turn_timeout` — 1 read(s): orchestrator.py:1353
- `_verify_context` — 1 read(s): orchestrator.py:1877
