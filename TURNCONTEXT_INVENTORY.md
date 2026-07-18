# TurnContext migration inventory (V3 board 2.3a)

GENERATED — do not hand-edit. Regenerate with:
`python3 tests/test_cfg_key_inventory.py`. The companion
gate (tests/test_cfg_key_inventory.py) fails on any NEW
underscore key and on stale allowlist entries.

54 written keys · 85 write sites · 205 read sites across orchestrator.py, visualqa.py, urlfetch.py, uicrawl.py

| key | writes | reads | write sites |
|---|---|---|---|
| `_<dynamic>_%s_sessions` | 1 | 0 | orchestrator.py:1575 |
| `_agent_health` | 3 | 0 | orchestrator.py:4102; orchestrator.py:5142; orchestrator.py:7831 |
| `_agent_role_overrides` | 1 | 1 | orchestrator.py:7863 |
| `_app_dir` | 1 | 6 | orchestrator.py:7806 |
| `_autonomy` | 2 | 1 | orchestrator.py:7830; orchestrator.py:7854 |
| `_base_models` | 1 | 1 | orchestrator.py:7717 |
| `_base_resolved` | 1 | 1 | orchestrator.py:7718 |
| `_budget` | 1 | 4 | orchestrator.py:8030 |
| `_build_dir` | 1 | 15 | orchestrator.py:4755 |
| `_checked_any_agent_runnable` | 1 | 1 | orchestrator.py:7202 |
| `_claude_model_override` | 3 | 2 | orchestrator.py:1164; orchestrator.py:4928; orchestrator.py:5319 |
| `_claude_sessions` | 2 | 0 | orchestrator.py:5143; orchestrator.py:6413 |
| `_codex_sessions` | 2 | 0 | orchestrator.py:5144; orchestrator.py:6414 |
| `_completeness` | 1 | 1 | orchestrator.py:7844 |
| `_deadline` | 2 | 13 | orchestrator.py:8031; orchestrator.py:8041 |
| `_drop_prior_discussions` | 1 | 1 | orchestrator.py:4700 |
| `_explicit_app` | 1 | 2 | orchestrator.py:8985 |
| `_gemini_disabled_reason` | 1 | 5 | orchestrator.py:8534 |
| `_gemini_unavailable` | 1 | 2 | orchestrator.py:915 |
| `_health_key` | 6 | 2 | orchestrator.py:1260; orchestrator.py:4751; orchestrator.py:4872; orchestrator.py:5393; orchestrator.py:5589; orchestrator.py:6002 |
| `_installed_ollama_models` | 1 | 4 | orchestrator.py:3665 |
| `_iter_verify_toolchain_absent` | 1 | 2 | orchestrator.py:4602 |
| `_new_session_id` | 3 | 0 | orchestrator.py:672; orchestrator.py:1587; orchestrator.py:1599 |
| `_noted_indep_grader` | 1 | 1 | orchestrator.py:5911 |
| `_noted_local_active_limit` | 1 | 1 | orchestrator.py:3755 |
| `_noted_local_lane_skip` | 1 | 1 | orchestrator.py:3894 |
| `_noted_local_ram_gate` | 1 | 1 | orchestrator.py:3734 |
| `_noted_ollama_sprint_skip` | 1 | 1 | orchestrator.py:3702 |
| `_noted_ollama_uninstalled_skip` | 1 | 1 | orchestrator.py:3712 |
| `_original_prompt` | 1 | 1 | orchestrator.py:7820 |
| `_personalities` | 1 | 1 | orchestrator.py:7862 |
| `_phase_deadline` | 5 | 13 | orchestrator.py:6094; orchestrator.py:8032; orchestrator.py:8113; orchestrator.py:8115; orchestrator.py:8117 |
| `_phase_instructions` | 1 | 1 | orchestrator.py:5230 |
| `_phase_key` | 1 | 3 | orchestrator.py:5146 |
| `_prior_discussions` | 1 | 1 | orchestrator.py:8099 |
| `_resolved` | 5 | 17 | orchestrator.py:1169; orchestrator.py:5231; orchestrator.py:5274; orchestrator.py:7720; orchestrator.py:8514 |
| `_role_by_id` | 1 | 1 | orchestrator.py:7866 |
| `_role_routing` | 1 | 1 | orchestrator.py:5235 |
| `_roles` | 1 | 2 | orchestrator.py:7862 |
| `_round_multiplier` | 2 | 1 | orchestrator.py:7829; orchestrator.py:7843 |
| `_routed_rounds` | 1 | 2 | orchestrator.py:5226 |
| `_routed_turn_timeout` | 1 | 1 | orchestrator.py:5222 |
| `_routing` | 1 | 5 | orchestrator.py:5552 |
| `_session` | 6 | 3 | orchestrator.py:1249; orchestrator.py:1579; orchestrator.py:1584; orchestrator.py:1595; orchestrator.py:1598; orchestrator.py:5588 |
| `_sim_ctx` | 1 | 1 | visualqa.py:368 |
| `_state` | 1 | 2 | orchestrator.py:7807 |
| `_target_path` | 2 | 11 | orchestrator.py:7869; orchestrator.py:7877 |
| `_target_paths` | 1 | 5 | orchestrator.py:7874 |
| `_tech_stack_block` | 1 | 2 | orchestrator.py:7984 |
| `_url_context` | 2 | 5 | orchestrator.py:7792; orchestrator.py:8016 |
| `_warned_no_git_repo` | 1 | 1 | orchestrator.py:7194 |
| `_workflow_name` | 1 | 14 | orchestrator.py:7855 |
| `_workflow_target` | 1 | 11 | orchestrator.py:7856 |
| `_workflow_verify_spec` | 1 | 1 | orchestrator.py:7859 |

Read-only keys (written nowhere in the scanned files —
either dead reads or written via non-subscript paths;
verify before migrating):

- `_allow_writes` — 11 read(s): orchestrator.py:610; orchestrator.py:617; orchestrator.py:631; orchestrator.py:635
- `_knowledge` — 3 read(s): orchestrator.py:1862; orchestrator.py:6398; orchestrator.py:6400
- `_phase_exemplar` — 1 read(s): orchestrator.py:1847
- `_phase_playbook` — 2 read(s): orchestrator.py:1844; orchestrator.py:6386
- `_prior_disc_cap` — 2 read(s): orchestrator.py:1814; orchestrator.py:1815
- `_read_dir` — 3 read(s): orchestrator.py:617; orchestrator.py:618; orchestrator.py:619
- `_session_cwd` — 2 read(s): orchestrator.py:723; orchestrator.py:724
- `_target_digest` — 3 read(s): orchestrator.py:1874; orchestrator.py:6443; orchestrator.py:6450
- `_turn_timeout` — 1 read(s): orchestrator.py:1358
- `_verify_context` — 1 read(s): orchestrator.py:1879
