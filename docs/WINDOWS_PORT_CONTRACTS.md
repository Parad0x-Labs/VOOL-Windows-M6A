# Windows port contracts

Frozen at windows/v0.7.1-port e8f607d. Every file:function:line below was read at that commit. Most line numbers were later moved to the matching code at d6866a4; the sentences still describe the code as it was at e8f607d.
**VERIFIED** means checked by hand in source at e8f607d. **UNVERIFIED** means not confirmed.
**FALSE** means an earlier inventory claim (taken at 0ac9f35) that source contradicts.
This is a description of what exists today. Nothing here is a fix. The gaps in section 6 come from
reading the source and from reviews of this document; they do not prove that no other route exists.

This is Step 0 of the Windows port plan, which is kept outside this repository. The plan step
expected to close each gap (section 6): 1 -> Step 3; 2 -> Step 3; 3 -> unassigned;
4 -> unassigned; 5 -> Step 2; 6 -> unassigned; 7 -> Step 3; 8 -> unassigned; 9 -> unassigned;
10 -> closed by PR #18; 11 -> closed by PR #20 for the intents the gate covers, except repo.git
(gap 14) and workspace.apply_unified_diff (gap 15); 12 -> its Windows write gate part closed after
this freeze by PR #22, its missing loopback check unassigned; 13 -> unassigned; 14 -> unassigned;
15 -> unassigned.

## 1. Frozen SHAs

| What | SHA | Status |
|---|---|---|
| Port (windows/v0.7.1-port) | `e8f607df55294d33af9b13419ef1532024045346` | includes PR #15 (merged 19 Oct 2026 by fast-forward to `4a440b6`), PR #18 (merged 19 Oct 2026 by fast-forward to `c86d013`) and PR #20 (merged 19 Oct 2026 by fast-forward to `e8f607d`). Checked locally: `ee8ced6` (the first freeze), `4a440b6` and `c86d013` (the previous freeze) are ancestors, and every commit after `710f3f1` has one parent |
| Upstream v0.7.1-beta (Parad0x-Labs/vool) | `baf115c251d89be61eb382141d0529a7ee1bbb40` | VERIFIED: tag `v0.7.1-beta` re-fetched 18 Oct 2026 for the earlier freeze, `git rev-parse FETCH_HEAD`; GitHub's compare baf115c...e8f607d (19 Oct 2026) reports status "ahead", behind_by 0, merge base baf115c, so it is an ancestor of e8f607d |

## 2. Capability and authority-route inventory

Common to every HTTP POST: Host allowlist (core/web/api/app.py:_dispatch:107) and cross-origin
Origin refusal (app.py:_dispatch:144-146). VERIFIED. "Owner" means a loopback TCP peer
(core/request_trust.py:is_loopback_host:114). On HTTP chat, `owner_local` is loopback AND
surface != "channel" (core/web/api/service.py:7427-7430). VERIFIED. Any local process passes a
loopback check; request_trust.py:19 says so ("any local process is treated as the owner").

| Route (effect) | Entry point | How authority is established | Test |
|---|---|---|---|
| Tool executor: runtime/operator/web/hive tools (files, processes, network) | core/tool_intent_executor.py:execute_tool_intent:239 -> _execute_tool_intent_impl:1062 | Windows write gate 1355, then mode controller `decide_tool_call` 1358 (core/mode_permission_policy.py:3597), then A6 reserve 1471 | tests/test_windows_write_gate_before_approval.py, tests/test_a6_effect_reconciliation.py |
| Fast paths and Blackbox rollback (files) | core/authorized_tool_execution.py:execute_authorized_runtime_tool:193 | Windows gate 254, then `authorize_runtime_tool` 259 when no decision is handed in | test_windows_write_gate_before_approval.py::test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision |
| Runtime dispatcher (files, network) | core/runtime_execution_tools.py:execute_runtime_tool:3614 -> _dispatch_runtime_tool:3116 | agent-team write gate (3670) and council seat fence (containment.evaluate, 3693) first, Windows gate again at 3272 | tests/test_windows_write_gate.py::test_the_real_dispatch_refuses_a_workspace_write_and_writes_nothing |
| Operator lane typed turns (move, temp cleanup, calendar, notes) | core/agent_runtime/turn_dispatch.py:352 -> core/local_operator_actions.py:dispatch_operator_action:70 | parsed approval words plus a stored pending action: the one whose id is typed, or with no id the session's latest (core/operator/approvals.py:load_pending_action:105, 124-134); Windows gate at 107, which refuses every temp cleanup (PR #20) and a move approval that names no paths; notes never ask it (gap 13) | test_windows_write_gate.py::test_an_operator_move_is_refused_outside_flagged_folders |
| sandbox.run_command (process) | runtime_execution_tools.py:_run_command:7436 | mode controller, cwd check `sandbox_cwd_refusal`:7333, kernel sandbox. On Windows sandbox/job_runner.py:_no_kernel_isolation_message:775 says no backend exists, so it fails closed (UNVERIFIED end to end) | the message only: tests/test_job_runner.py::JobRunnerTests::test_windows_missing_backend_error_names_heuristic_only_and_wsl2; none for the refusal end to end |
| Plugins and MCP servers (process) | core/plugin_executor.py:confined_argv:254 | Windows refuses bare and heuristic_only (`_WINDOWS_REFUSES_BARE`:64, 280) | tests/test_windows_never_runs_unconfined_children.py::test_windows_refuses_heuristic_only |
| Process agents (process) | core/agent_team/coordinator.py:_launch_process:260 | Windows refuses (`_WINDOWS_REFUSES_PROCESS_AGENTS`:48) | test_windows_never_runs_unconfined_children.py::test_windows_refuses_to_start_a_process_agent |
| vool-browser lane (process, network, downloads) | runtime_execution_tools.py:3175 -> core/vool_browser/sessions.py:open_session:346 | mode controller only; no Windows check (gap 8) | none for Windows |
| Media worker (process) | core/media_worker_client.py:NebulaWorkerClient._spawn:121 | none on Windows (gap 8) | none for Windows |
| Media Studio editor export over HTTP (files) | POST or DELETE (core/web/api/app.py:136) -> service.py:_dispatch_post_inner:4217, branch 4236-4239 -> core/web/api/media_editor_api.py:handle_media_editor_post:282 (export 307-328) -> core/media_studio_service.py:export_project:248 | the policy flag `media.editor_export_enabled` only (media_editor_api.py:310, default True); no loopback check (gap 12, still open) and no Windows write gate (gap 12; PR #22 added the gate after this freeze) | none for Windows |
| Git on user repos (process) | core/safe_git.py:hardened_argv:775 (also named `read_only_argv`, 794), used by execution/git_tools, repoops/gitops, user_rules, project_archaeology; workspace_tools builds its `git apply` from `hardening_args`:110 | refuses repo-config programs and hooks; on Windows the write gate checks repo.git (gap 14) and workspace.apply_unified_diff's `git apply` (gap 15) on the chat's workspace, not on the repository git changes | tests/test_git_tools_repository_config_cannot_run_programs.py, tests/test_archaeology_git_is_hardened.py |
| /api/commands/dispatch (updates, approvals, team control, settings) | service.py:dispatch_post:3028, branch 3059-3063 -> core/command_registry/api.py:handle_commands_dispatch:36 | NO peer check; body `approval_context`; principal defaults to "operator" (gap 1) | none for a non-loopback peer |
| /api/update/install, restart, rollback | service.py:3170, loopback 3174 | loopback plus server-made press | tests/updater/test_final_p1.py::TestNoUpdateFromProseRemoteOrForgery::test_remote_host_cannot_press_install_or_restart |
| /api/mode: bypass, grant_chat_workspace, resolve_approval | service.py:4446, loopback 4450; bypass confirmation id single use, 60 s (4466, 4502); grant 4644; resolve 4747 | loopback | grant: tests/test_coding_approval_workflow_repairs.py::test_the_grant_still_requires_a_pending_ask (loopback only); a non-loopback peer, shown with a mode change: tests/test_mode_permission_policy.py::test_mode_endpoint_is_loopback_only_and_bypass_cannot_be_set_without_grant |
| /api/runtime/unresolved-effects/resolve (A6 truth) | service.py:4847, loopback 4860 | loopback | tests/test_a6_resolution_surface.py::test_resolve_is_owner_local_only |
| /api/repoops/authorize-push, authorize-forge-action (network, remote refs) | service.py:5467 (loopback 5472), 5487 (loopback 5493) -> core/web/api/repoops_api.py:authorize_push:84, authorize_forge_action:208 | loopback, minted authorization in a reserved trust key | tests/repoops/test_repoops_adversarial.py::test_a_push_without_an_authorization_is_refused_and_the_remote_does_not_move; peer: tests/repoops/test_repoops_served.py::test_authorize_push_is_owner_local_only (authorize-push only; an authorize-forge-action peer test is UNVERIFIED) |
| /api/plugins/lifecycle | service.py:5542, loopback 5546 | loopback | tests/repoops/test_repoops_served.py::test_the_lifecycle_console_is_owner_local_only |
| /api/wallet/* POST (money) | service.py:4250-4260 -> core/web/api/wallet_api.py:handle_wallet_post:236 | loopback 239, then for trusted doors core/wallet/caller_binding.py:refusal_reason:91 | tests/wallet/test_crypto_pilot_caller_binding.py::test_a_page_on_another_localhost_port_is_refused_at_every_trusted_door |
| /api/cloud/usepod/* incl. spend-approval (money) | service.py loopback 5031 -> _usepod_owner_action:3937 (spend branch 4035) | loopback; grant minted through the operator approval store | the shared loopback check, shown on refresh: tests/usepod/test_usepod_registration.py::test_owner_actions_hold_the_post_guard_suite (case not-owner-local); spend-approval from a non-loopback peer UNVERIFIED |
| pay.x402 tool (money) | core/execution/payment_tools.py:execute_payment_tool:27 | refuses; the spend brake is not consulted for the answer | tests/test_payment_tools.py::PayX402RetiredTests::test_a_granted_spend_brake_opens_nothing |
| wallet.spend tool (money) | runtime_execution_tools.py:_wallet_spend:2735 | retired: always a typed refusal | tests/test_wallet_spend.py::test_handler_refuses_typed_regardless_of_approval_flags |
| Brake /startx402 (money unfreeze) | core/vool_agent_brake.py:maybe_handle_agent_brake:109 | owner_local (133, 184) plus OS consent `_default_unfreeze_consent`:102 | tests/test_vool_agent_brake.py::test_startx402_denied_consent_keeps_spending_frozen |
| Mobile companion (/api/mobile/*) | service.py:4261 -> core/web/api/mobile_companion_api.py:handle_mobile_companion_post:1430 | single-use pairing code (sha256 compare, 586), Ed25519 device signatures (`_verify_ed25519`:438, used 609, 771), and per request the Device Link grant check (`registry.authorize` 740: the grant's Ed25519 self-signature, then HMAC-SHA256 under the grant's secret; core/device_link/protocol.py:GrantRegistry.authorize:337, 346, 366-370) | tests/mobile/test_mobile_companion_api.py::TestPairing::test_start_is_owner_local_only |

Inventory claims corrected here:
- FALSE: "/api/update/* no test found". The test named in the table exists.
- FALSE: "/api/mode grant_chat_workspace no test found". tests/test_coding_approval_workflow_repairs.py
  drives it through dispatch_post (helper at line 222). A non-loopback grant_chat_workspace test is
  UNVERIFIED; the endpoint's loopback check (service.py:4450) is tested with a mode change (see the table).
- FALSE as worded: "mobile companion: HMAC-signed pairing grant". The grant is signed with the
  desktop's Ed25519 authority key (core/device_link/protocol.py:issue_grant:161, signature 206), not
  with an HMAC. HMAC-SHA256 under the grant's bearer secret authenticates each request
  (GrantRegistry.authorize:337, check 366-370, called at mobile_companion_api.py:740). Pairing also
  needs a hashed single-use code (586) and an Ed25519 proof of possession (609).
- UNVERIFIED: team-lane model turns (core/agent_team) arrive over loopback. core/agent_team/model_agent.py:156 posts
  to `base_url + "/api/chat"`; where base_url comes from was not traced.
- VERIFIED, narrowed: `turn_author` (request_trust.py:TURN_AUTHOR_KEY) is read through
  `turn_is_owner_authored` (request_trust.py:171) at three places (web/api/runtime.py:2222,
  user_rules.py:1851, standing_instructions.py:637), all memory-like writes. It is also read directly
  at core/presentation_selection.py:702, where a non-owner turn only turns off reply reshaping.

## 3. Invariant -> enforcement -> test

Each enforcement point was read at e8f607d (line numbers moved to d6866a4). Each test file and function exists there (checked with grep).

| Invariant | Enforcement (file:function) | Named test |
|---|---|---|
| Windows: user-file writes only inside VOOL_WINDOWS_DISPOSABLE_WORKSPACES | core/windows_write_gate.py:refusal:294 (`_GATED`:35); each keyed path is checked where its writer puts it (`_landing`:86), and a temp cleanup is refused outright (`_ALWAYS_REFUSED`:55) (gap 11, closed). At e8f607d the Media Studio editor's HTTP export (gap 12; PR #22 added the gate after this freeze) and operator notes (gap 13) never ask it, and it checks repo.git (gap 14) and workspace.apply_unified_diff (gap 15) on the chat's workspace, not on the repository git changes | tests/test_windows_write_gate.py::test_no_flagged_folder_refuses_every_user_filesystem_write; tests/test_machine_writes_are_checked_where_they_land.py::test_a_flagged_chat_workspace_no_longer_lets_a_write_into_the_real_documents_folder, ::test_windows_refuses_a_temp_cleanup_even_from_a_flagged_folder |
| ...refused before the permission decision and before A6 (PR #15), except a council seat, and a repo.step, code.task.step or code.task.rollback from a chat working in a flagged folder on a session or task folder that is not flagged (gap 2) | core/tool_intent_executor.py:_windows_write_refusal:886 (called 1355); core/runtime_execution_tools.py:windows_write_refusal:7375, windows_write_refusal_before_decision:7420; core/local_operator_actions.py:windows_write_refusal:55 | tests/test_windows_write_gate_before_approval.py::test_a_write_outside_the_flagged_folders_is_refused_before_approval_and_a6, ::test_an_operator_move_outside_the_flagged_folders_is_refused_before_approval |
| ...fast paths gated before the boundary decides | core/authorized_tool_execution.py:execute_authorized_runtime_tool:254 | test_windows_write_gate_before_approval.py::test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision |
| ...inside a flagged folder the write still reaches approval | same | test_windows_write_gate_before_approval.py::test_inside_a_flagged_workspace_the_write_still_reaches_approval |
| ...a council seat is gated on its re-rooted workspace | runtime_execution_tools.py:execute_runtime_tool:3737-3746 then _dispatch_runtime_tool:3272 | test_windows_write_gate_before_approval.py::test_a_council_seat_is_gated_on_the_workspace_it_is_re_rooted_to |
| ...a step is gated by its inner intent | windows_write_gate.py:refusal (`_STEPS`:51), on the chat's workspace (311-320); a repo.step's inner call is gated again on its session folder at dispatch (core/repoops/plane.py:_execute:3172-3174 with _session_context:458-465, gate at runtime_execution_tools.py:3272; gap 14), and a code.task.step's on its task's folder (core/code_assistant/task_runtime.py:_task_context:1485, `_dispatch`:3542; gap 2); for a patch that check has gap 15's limit | test_windows_write_gate.py::test_a_step_is_gated_by_its_inner_intent |
| Junctions and name-surrogate links are links | core/fs_links.py:is_link_stat:26, is_link:33; core/execution/artifacts.py:_open_pinned_directory:116 | tests/test_windows_junction_boundary.py::test_a_reviewed_write_through_a_junction_is_refused_and_writes_nothing |
| Git never runs a program named by repo config, hooks or env | core/safe_git.py:executable_local_config:129, active_hooks:757, hardened_argv:775 | tests/test_git_tools_repository_config_cannot_run_programs.py::test_fsmonitor_in_repository_config_is_refused_and_never_runs; tests/test_archaeology_git_is_hardened.py::test_every_archaeology_git_call_is_hardened_and_a_plain_repository_still_compares |
| No unconfined plugins, MCP servers or process agents on Windows | core/plugin_executor.py:confined_argv:254; core/agent_team/coordinator.py:_launch_process:260 | tests/test_windows_never_runs_unconfined_children.py::test_windows_still_refuses_the_default_mode, ::test_windows_refuses_to_start_a_process_agent |
| File locks work without fcntl | core/os_file_lock.py:lock_exclusive:37 | tests/test_os_file_lock.py::test_a_held_lock_excludes_another_process_until_released |
| A liveness probe never signals | core/process_liveness.py:pid_alive:18 | tests/test_process_liveness_never_kills.py::test_a_live_process_reads_alive_and_survives_the_probe |
| Installer, Stop and supervisor stop only this installation's VOOL processes (gap 10) | installer/bundle/vool.iss:StopNulla:91 (filter 112; run from PrepareToInstall 121 and InitializeUninstall 127); installer/vool_stop.py:kill_listener:114 (predicate 132-137), kill_api_by_pidfile:148 (predicate 164-169); installer/bundle/bundle_supervisor.py:_clear_stale_api_processes:641 (match 662-664) | tests/test_windows_installer_ownership.py::test_the_installer_never_stops_a_process_just_because_it_owns_the_api_port, ::test_the_installer_still_stops_vools_own_python_processes; tests/test_bundle_tooling.py::test_bundle_supervisor_clears_stale_api_processes_before_bundle_start; Windows only: tests/test_windows_installer_stops_only_this_install.py::test_every_bundle_stop_stops_only_this_install_for_real; tests/test_windows_stop_spares_foreign_listeners.py::test_the_stop_leaves_a_foreign_listener_running |
| Ledger names are ones Windows can create | core/execution/artifacts.py (`_WINDOWS_LEDGER_NAMES`:759) | tests/test_windows_mutation_ledger_names.py::test_a_key_windows_cannot_use_gets_a_digest_name_inside_the_ledger_dir |
| Window attaches only to the supervisor's API process | installer/bundle/bundle_supervisor.py; installer/bundle/native_runtime_supervisor.py (`vouching_pid`:82, env 125) | tests/test_windows_window_attaches_to_supervised_api.py::test_the_window_is_told_the_exact_api_process_this_supervisor_started; tests/test_windows_window_vouched_attach.py::test_it_attaches_only_to_the_process_the_supervisor_started |
| Uninstall removes only program files | installer/bundle/vool.iss [UninstallDelete]:84-88 | tests/test_windows_uninstall_removes_program_files.py::test_every_uninstall_delete_stays_inside_the_program_folder |
| Supervisor state stays out of the program folder | installer/bundle/bundle_supervisor.py:main:846 (VOOL_HOME default 850) | tests/test_windows_supervisor_keeps_state_out_of_the_program_folder.py::test_a_direct_stop_runs_its_preflight_in_the_native_home |
| The model is never an effect-truth source | core/runtime_continuity.py:resolve_unresolved_effect:4160 (refusal 4175) | tests/test_a6_effect_reconciliation.py::ModelCannotResolve::test_model_source_resolution_is_refused |

## 4. Receipt states

| State | Status literals today (all VERIFIED present in source) |
|---|---|
| refused (never dispatched) | `windows_filesystem_authority_unavailable`, `sandbox_unavailable`, `blocked_by_policy`, `blocked_by_mode`, `blocked_by_hard_link`, `cwd_outside_workspace`, `confinement_unavailable`, `refused_repository_config`, `permission_denied`, `user_action_required`, `simulate_only`, `pending_approval` (waiting, not executed). A blocked retry: `unknown_reconciliation_required` with `blocked_pending_reconciliation: True`, `executed: False` (tool_intent_executor.py:_a6_unknown_blocked_execution:990) |
| committed | `executed`; A6 row `applied`; contacts `committed`; Blackbox `rollback_committed` |
| committed-but-cleanup-failed | **No distinct state today.** VERIFIED. See below |
| outcome_unknown | `unknown_reconciliation_required` / mode `reconciliation_pending` (core/effect_reconciliation.py:33-34); A6 row `unknown`, reason `post_dispatch_exception` or `crash_recovery_dispatch_lease_expired`; operator lane `outcome_unproven` |

What a "committed, then cleanup or recording failed" case collapses into, by path (VERIFIED in code):
- A handler raises after the A6 claim: runtime_execution_tools.py:_dispatch_runtime_tool:3564-3575
  re-raises as `EffectOutcomeUnknown(reason="post_dispatch_exception")`; the executor
  (tool_intent_executor.py:1519-1530) writes A6 `unknown`. Result: **outcome_unknown**.
- A handler raises with no A6 claim (no checkpoint_id, or an intent that is not reserved):
  the same handler returns `ok=False, status="error"` (runtime_execution_tools.py:3576-3581).
  Result: a **plain failure**, even if the effect landed.
- A handler returns `ok=False` after it changed files: the executor ack
  (tool_intent_executor.py:1574-1586) maps every non-unknown failure to A6 `failed_safe_to_retry`
  (reason `dispatch_ack`). Example: an approved multi-file patch whose later write fails and whose
  compensation also fails returns `ok=False`, `status` `write_failed`/`destination_changed`/
  `stale_base`, `restore_complete: False`, non-empty `changed_paths`
  (core/execution/workspace_tools.py:_apply_protected_unified_diff:889-900,
  _compensate_protected_writes:679). If that result crosses the executor ack with a claim, it is
  recorded **failed_safe_to_retry**. Whether this path reaches the ack with a claim is UNVERIFIED.
  Other patches that changed files did reach it with a claim and were recorded
  **failed_safe_to_retry**: one git applied in a subfolder of a repository while VOOL answered
  `no_change`, and one GNU patch applied in part while VOOL answered `stale_base` (gap 15).
- Closest existing equivalents: operator lane `effect_unrecorded` (effect verified, receipt not
  recorded; core/operator/approvals.py:55, 167, release:343-349) and the "moved aside ... preserved
  for recovery" error (core/execution/artifacts.py:pinned_remove_created_file:288, raised 346-359).

## 5. Crash-after-mutation reconciliation (A6)

1. Reserve. tool_intent_executor.py:_a6_reserve_before_dispatch:932 runs only when
   checkpoint_id is present and `_a6_reservation_required`:820 says so (947). It blocks on an
   active row (950-956), then calls core/runtime_continuity.py:reserve_logical_effect:3882:
   BEGIN IMMEDIATE (3915), lazy lease sweep (3919-3943), active-row check, insert. Backed by the
   partial unique index `ux_unresolved_active_effect` (storage/migrations.py:1243). Active states:
   prepared, dispatched, unknown (runtime_continuity.py:3673). VERIFIED.
2. Claim. runtime_continuity.py:mark_effect_dispatched:4002 is a PREPARED->DISPATCHED
   compare-and-set; a lost claim is treated as blocked (tool_intent_executor.py:1483-1493). VERIFIED.
3. Exception. runtime_execution_tools.py:3564-3575 -> `EffectOutcomeUnknown`
   (effect_reconciliation.py:61) -> executor classify `unknown` (tool_intent_executor.py:1519-1530). VERIFIED.
4. Lease expiry. Lease is 900 s (runtime_continuity.py:3685). runtime_continuity.py:
   sweep_stale_unresolved_effects:3811 and the inline sweep in reserve: stale PREPARED ->
   `expired_pre_dispatch`, stale DISPATCHED -> `unknown` with `crash_recovery_dispatch_lease_expired`. VERIFIED.
5. Unknown blocks identical redispatch while prepared, dispatched or unknown (step 1). VERIFIED.
6. Resolution. runtime_continuity.py:resolve_unresolved_effect:4160; sources mechanical, provider,
   user (`_VALID_RESOLUTION_SOURCES`:3687); "model" refused (4175). Resolvers are registered for
   workspace.write_file and machine.write_file (effect_reconciliation.py:219-220), repo.push
   (core/repoops/plane.py:4490, run at import 4612) and wallet.pay (core/wallet/reconciliation.py:88).
   User source via /api/runtime/unresolved-effects/resolve (section 2). Fall-through lanes resolve
   mechanically (tool_intent_executor.py:_dispatch_with_claim:1665, resolve 1684-1689; the variant at
   1704 is the one used when there is no claim). VERIFIED.

Tests (exist): tests/test_a6_effect_reconciliation.py::R3R4AmbiguousBecomesUnknownAndBlocksRetry::test_mutate_then_raise_records_durable_unknown_and_blocks_fresh_retry,
::R5UnknownSurvivesRestart::test_durable_unknown_still_blocks_after_restart,
::CrashWindows::test_c2_crash_before_dispatch_expires_and_allows_fresh_dispatch,
::CrashWindows::test_c3_c4_lost_dispatch_becomes_unknown_and_blocks,
::R10ConcurrentReserve::test_concurrent_identical_effects_cannot_double_dispatch,
::R17ResultFailureIsFailedSafeToRetry::test_invalid_arguments_result_classifies_failed_safe_to_retry;
tests/test_a6_reservation_gap_convergence.py::FallThroughReservationGap::test_all_eight_gap_shapes_reserve_claim_and_reconcile;
tests/test_a6_resolution_surface.py::test_user_resolution_releases_the_block.

## 6. Gaps (listed, not fixed)

1. **VERIFIED.** `/api/commands/dispatch` (service.py:3059-3063) calls `handle_commands_dispatch(body)`
   with no peer check, unlike /api/update/* (3174) and /api/mode (4450). `approval_context` comes from
   the body (command_registry/api.py:44, 50). `ExecutionContext.principal` defaults to "operator"
   (command_registry/execute.py:46) and the API projection sets no `extra`. `_gate_two_press`
   (command_registry/groups/convergence.py:55-59) passes on any non-empty `gesture` or `authority_token`;
   it gates update.apply, update.restart, update.rollback. Correction to the inventory (FALSE in part):
   `approvals.resolve` is gated by `_gate_resolve` (groups/approvals.py:48-51, principal == operator),
   not by two-press; it still passes because the principal defaults to operator. Also
   `delegated_routes._peer` (groups/delegated_routes.py:55-56) returns "127.0.0.1" when no client_host
   is passed, so `_gate_owner_local` passes on this projection for any peer. Host and Origin checks
   still apply. No test covers a non-loopback peer here.
2. **VERIFIED. Fixed by PR #15 (merged)** for the executor (tool_intent_executor.py:1355, before
   `decide_tool_call` 1358 and A6 reserve 1471), the operator lane (same call, operator branch 900)
   and the fast paths (authorized_tool_execution.py:254). PR #15 also leaves a call the gate never
   refuses (off Windows, or an ungated intent) as it was: `may_refuse` (windows_write_gate.py:282)
   returns first at tool_intent_executor.py:898 and runtime_execution_tools.py:7390
   (test_windows_write_gate_before_approval.py::test_a_call_the_gate_never_refuses_is_handled_exactly_as_before_the_early_gate).
   Residual by design: a council seat.
   `windows_write_refusal_before_decision` (runtime_execution_tools.py:7420-7433) returns None when
   `council.containment.binding_for_context` finds a seat, because only `execute_runtime_tool` re-roots
   the seat onto its own workspace (3737-3746). For a seat the decision and A6 reserve/claim happen
   first; the gate runs at dispatch (3272). If the seat's workspace is not flagged, the refusal comes
   back `ok=False` and the A6 row is classified `failed_safe_to_retry` / `dispatch_ack` (1574-1586).
   Only the allowed seat case is tested. A second residual: a repo.step, code.task.step or
   code.task.rollback from a chat working in a flagged folder, on a repo session or code task whose
   folder is not flagged. The early gate checks a step's inner intent (windows_write_gate.py:311-320),
   and code.task.rollback itself (`_GATED`:41, the root only, 330), on the chat's workspace
   (runtime_execution_tools.py:7399), so the decision runs first; the call then runs in the session's
   or task's folder, where the gate refuses it. A repo.step's inner call runs there through
   plane.py:_execute:3172-3174 (gap 14) and a code.task.step's through
   core/code_assistant/task_runtime.py:_task_context:1485 and `_dispatch`:3542, both refused by the
   dispatch-time gate (3272); a code.task.rollback restores through `rollback_turn` with the task's
   folder (task_runtime.py:3772-3782; core/blackbox/operator.py:35, 40-46), refused by the fast-path
   gate (core/authorized_tool_execution.py:254). A code task keeps the folder it was opened in
   (task_runtime.py:open:1651, 1673) and checks only the calling session (`_require_owned`:1451,
   1456-1457), and an HTTP chat turn names its own workspace (service.py:7479-7487). None of the
   three is A6-reserved (core/effect_reconciliation.py:a6_reserved_intent:53), so no A6 row is made.
   In the runs (a repo session's diagnosis and Gate 0 verdict, or a code task's stage, set directly,
   since a real diagnosis or reproduction needs a command run), Manual mode asked to approve
   `Run repo.step` naming no folder, `Run workspace.write_file through code.task.step` naming the file
   in the task's folder, or `Run code.task.rollback` naming none; once allowed, each was refused and
   wrote nothing. Auto mode refused a step after its decision and asked to approve the rollback
   first. From a chat working in the unflagged folder each was refused before any decision. No test
   covers these: the executor-level step cases (tests/test_windows_write_gate_before_approval.py:
   56-57) name a task or session that does not exist, and both are refused from a chat working in an
   unflagged folder (63-80). The code.task.step case is also run from a chat working in the flagged
   folder (101, 108-121); there it reaches the approval ask after one decision and stops, so that test
   pins the decision this residual takes first without reaching the refusal. UNVERIFIED: other
   `decide_tool_call` callers
   (core/semantic/admission.py:124, core/live_data_plan.py:863) do not ask the Windows predicate first;
   whether they can carry a gated write intent was not traced. The remaining callers decide fixed
   intents the gate does not cover: core/effect_gateway.py:2215 (`web.research`) and, through
   `authorize_runtime_tool`, core/agent_runtime/fast_paths_media.py:117 (`image.generate`) and
   core/agent_runtime/turn_frontdoor.py:260 (`web.fetch`).
3. **VERIFIED (upstream design, unchanged from baf115c2: `git diff` was empty for core/os_consent_gate.py
   at the earlier freeze, and the file is unchanged from ee8ced6 to e8f607d).**
   Without Windows Hello, `_consent_windows` (os_consent_gate.py:117) falls back to
   `_try_windows_credential_prompt`:176, CredUI with `CREDUIWIN_GENERIC`; the entry is never verified
   (presence only). `VOOL_WALLET_SKIP_CONSENT_GATE=yes` returns True with a warning
   (`_env_bypass_enabled`:59, used 74-80).
4. **VERIFIED.** `spend_authorization.require_spend_authorized` (core/spend_authorization.py:138) has
   no production caller: a repo-wide grep finds only tests. The pay.x402 surface refuses before it
   would matter (tests/test_payment_tools.py::PayX402RetiredTests::test_a_granted_spend_brake_opens_nothing).
5. **VERIFIED.** No file outside tests sets `VOOL_WALLET_UI_CAPABILITY_SHA256`
   (core/wallet/caller_binding.py:23), so `mode()`:68 is `unbound`. Trusted doors then rest on
   Host/Origin/Sec-Fetch-Site/content-type checks only (`refusal_reason`:91-115); a non-browser local
   client that sends no Origin passes.
6. **VERIFIED.** "Committed but cleanup failed" is not a distinct state (section 4). Depending on the
   path it becomes outcome_unknown, a plain `error`, or A6 `failed_safe_to_retry`.
7. **VERIFIED in the files named, UNVERIFIED upstream of them.** Typed operator approvals ("approve
   cleanup <id>", core/operator/handlers.py:346; parser core/operator/parser.py:166, 335) dispatch at
   core/agent_runtime/turn_dispatch.py:352. No `owner_local` / `request_is_owner_local` reference exists
   in turn_dispatch.py, operator/parser.py, operator/handlers.py or local_operator_actions.py. The
   inventory path core/turn_dispatch.py is FALSE (the file is core/agent_runtime/turn_dispatch.py).
   Whether a non-owner turn is filtered before this lane was not traced. On Windows the write gate now
   refuses `approve cleanup <id>` (it refuses every temp cleanup) and an `approve move <id>` that names
   no paths, before either handler runs (core/local_operator_actions.py:107; gap 11).
8. **VERIFIED in source.** core/media_worker_client.py:_spawn:121-136 starts `python -m nebula.worker`
   with no Windows confinement check. `vool-browser.*` is dispatched at runtime_execution_tools.py:3175,
   before the Windows gate at 3272. core/vool_browser/engine.py:find_engine_binary:145 accepts any
   executable named by `VOOL_BROWSER_ENGINE`; sessions.py:_own_engine:139 launches it via Playwright with
   `accept_downloads=True` and no Windows check. UNVERIFIED: whether nebula, Playwright or a Chromium
   binary is present in the Windows bundle.
9. **VERIFIED, and the inventory was incomplete.** project_archaeology was fixed in 95f3a51. Raw
   `git` without safe_git remains in core/repoops/identity.py:25, bootstrap.py:50, localgit.py:102,
   local_lifecycle.py:75, and also repoops/archaeology.py (imports identity). None of these is
   imported by production code; only scripts/*_demo.py import them. Other raw git calls exist:
   core/council/api.py:_workspace_sha:78 runs `git rev-parse HEAD` in a council workspace;
   core/agent_runtime/continuity_gate.py:209 in a `repo_dir`; the rest target VOOL's own checkout
   (runtime_execution_tools.py:562, web/api/runtime.py:185, build_provenance.py:27, kernel/repl.py:5136,
   proof_manifest.py:12-14). UNVERIFIED: whether `rev-parse` there can run a repo-configured program.
10. **Closed by PR #18. VERIFIED in source.** The three stops PR #18 changed now match only one
    installation's Python, named by its root folder. Setup
    (installer/bundle/vool.iss:StopNulla:91-115, filter 112) stops a python.exe or pythonw.exe whose
    exe is exactly `{app}\python\python.exe` or `{app}\python\pythonw.exe`, or whose command line runs
    `{app}\app\apps\vool_api_server.py` or `{app}\vool_window.py` as Python's script argument. `{app}`
    is quoted for PowerShell, curly quotes included (105-110). The stop runs from PrepareToInstall
    (121-125), once Setup knows `{app}`, and from InitializeUninstall (127-131). The Stop module
    (installer/vool_stop.py) takes the root from --project-root (main 389, passed on at hard_stop
    194-197). kill_listener:114 tree-kills the port's listener, and kill_api_by_pidfile:148 the
    pidfile's pid, only when it is a python.exe or pythonw.exe whose exe lies under the root, or that
    runs `<root>\apps\vool_api_server.py` as its script, or that runs `-m apps.vool_api_server` with a
    parent exe under the root (a checkout's venv launcher) (predicates 132-137, 164-169). The bundle
    supervisor's stale-API cleanup (installer/bundle/bundle_supervisor.py:_clear_stale_api_processes:641,
    called at 632, just before the API's `_start` at 635) stops a python.exe or pythonw.exe that runs
    `<root>\app\apps\vool_api_server.py` as its script (662-664), but never its own PowerShell or its
    own ancestors (654-661). Tests that run everywhere:
    tests/test_windows_installer_ownership.py::test_the_installer_still_stops_vools_own_python_processes;
    tests/test_windows_installer_setup_initializes.py::test_install_and_uninstall_still_stop_vool_before_touching_files;
    tests/test_windows_stop_root_quoting.py::test_installer_stop_keeps_an_install_folder_with_any_quote_whole,
    ::test_stop_button_keeps_a_root_with_any_quote_whole, ::test_supervisor_cleanup_keeps_a_root_with_any_quote_whole;
    tests/test_vool_stop.py::test_pidfile_kill_refuses_a_non_python_pid;
    tests/test_bundle_tooling.py::test_bundle_supervisor_clears_stale_api_processes_before_bundle_start.
    Windows only (real PowerShell): tests/test_windows_installer_stops_only_this_install.py::test_installer_stops_only_its_installation_processes,
    ::test_stop_button_stops_only_its_installation_api, ::test_source_checkout_module_listener_requires_its_venv_parent,
    ::test_supervisor_stops_only_its_installation_api_and_excludes_ancestors, and with real processes
    ::test_every_bundle_stop_stops_only_this_install_for_real,
    ::test_a_checkout_api_started_through_its_venv_launcher_is_stopped_for_real;
    tests/test_windows_stop_spares_foreign_listeners.py::test_the_stop_leaves_a_foreign_listener_running.
    Known residuals, not fixed (VERIFIED in source, impact UNVERIFIED):
    every matcher compares one spelling of the root as text (Setup's `{app}`, the Stop module's
    --project-root, the supervisor's --root after `resolve()`, bundle_supervisor.py:193, 860), so a VOOL
    process started through a junction, a subst drive or an 8.3 short name of the same folder can be
    missed. It is then left running and nothing outside the install is stopped (fails safe). No test
    covers it. Setup's filter takes only python.exe and pythonw.exe (vool.iss:112), so it does not stop
    `{app}\ollama\ollama.exe`, which the supervisor starts (bundle_supervisor.py:625). Start_VOOL.bat:95
    still stops every python.exe or pythonw.exe whose command line contains `apps.vool_api_server` or
    `vool_api_server.py`, from any folder, when the API on 11435 fails its health check (89-94).
11. **Closed by PR #20 for the intents the gate covers. At e8f607d the Media Studio editor's HTTP
    export (gap 12, gated after this freeze by PR #22) and operator notes (gap 13) never ask the gate,
    and it checks repo.git (gap 14) and workspace.apply_unified_diff (gap 15) on the wrong folder.
    VERIFIED in source; the new tests passed on Windows at e8f607d (Windows Release Gauntlet run
    39221752745).** At
    c86d013 the gate joined every relative keyed target onto the chat workspace, while the writers
    place the same arguments elsewhere. With the chat workspace inside a flagged folder,
    `machine.write_file` with path `Documents\x.txt` passed the gate and was written to the user's
    real Documents folder, and the Manual-mode approval ask named `<workspace>\Documents\x.txt`.
    Now `refusal` (windows_write_gate.py:294) places each keyed path where its writer puts it, read
    as that writer receives it (`_landing`:86). machine.write_file and machine.ensure_directory go
    through `anchor_machine_path` (runtime_execution_tools.py:4635), which their writers use too
    (`_resolve_safe_machine_directory`:4658, through `_resolve_machine_directory`:4624, called 5689
    and 5623): an alias such as "documents" is that folder in the user's home, a `~` path expands
    there, and another relative path is taken from the home. machine.move_path goes through
    core/machine_file_ops.py:_canonicalize:94, from the process's working directory. media.export
    goes through `export_destination` (core/media_studio_service.py:238, used by `export_project` at
    270), unstripped and made absolute so the separate media worker writes that same file, together
    with the folder that holds the output, where the export also writes its stage files.
    operator.move_path is checked after `$VAR`, `%VAR%` (Windows) and `~` are expanded, as its handler
    expands them (core/operator/storage.py:131-132), and only when the result is absolute. A path the
    gate cannot place, a path holding a NUL (`_inside`, 68) and a keyed intent with no rule are
    refused. Windows refuses operator.cleanup_temp_files outright (`_ALWAYS_REFUSED`:55, checked at
    323): a cleanup also empties TMPDIR, TMP, TEMP and gettempdir()
    (core/operator/storage.py:candidate_cleanup_roots:136), and its approval empties the plan a disk
    scan stored earlier. The approval ask shows a machine write's path where `anchor_machine_path`
    puts it (core/mode_permission_policy.py:_machine_path_display:3078, from
    `_affected_resources_display`:3038). The writers' own checks still apply on top: the machine
    writers accept only Desktop, Downloads and Documents in the home (runtime_execution_tools.py:4663-4667),
    machine.move_path needs both paths in those folders or the workspace (machine_file_ops.py:182-191)
    plus an OS consent (197-206), and media.export's writer has no root check of its own. Tests,
    which tools/windows_fresh_host_gauntlet.ps1:185 also runs on Windows:
    tests/test_machine_writes_are_checked_where_they_land.py::test_a_flagged_chat_workspace_no_longer_lets_a_write_into_the_real_documents_folder,
    ::test_an_export_writes_only_where_the_gate_checked, ::test_windows_refuses_a_temp_cleanup_even_from_a_flagged_folder,
    ::test_the_approval_ask_names_the_file_a_machine_write_changes.
    The gauntlet runs on a PR into windows/v0.7.1-port only when dispatched. On PR #20's heads, run
    39214703029 at adff1d5 failed 2 of the 201 tests in its Windows runtime regression step (the NUL
    cases, fixed in 6e17623), and run 39221752745 at e8f607d passed.
    Known residuals, not fixed (VERIFIED in source, impact UNVERIFIED): the gate checks paths, not
    handles. On Windows it judges by its text a path `realpath` cannot resolve, such as a DOS device
    name inside a flagged folder or a path over 32,767 characters. Temp cleanup, and an operator move
    approved by its id alone (which carries no paths), are refused on Windows. A move approved with
    paths typed is checked on the typed paths, but it runs the paths its stored preview holds
    (core/operator/handlers.py:558-590), and with no id typed it takes the session's latest preview
    (core/operator/approvals.py:124-134). Nothing checks the stored paths against the flagged folders
    at approval: the handler's own check (handlers.py:562, core/operator/storage.py:validate_move_scope:
    232-252) accepts any path in the user's home, the temp folder or VOOL's home folder (the parent of
    its data folder), unless it matches the policy's `filesystem.deny_paths` (`operator_safe_path`:
    209-229; deny check 222, roots 224-228). The preview passed the gate when it was made (the gate at
    core/local_operator_actions.py:107 runs before handlers.py:507 stores it), so the move can still
    land outside the flagged folders if they changed in between (a restart with another
    VOOL_WINDOWS_DISPOSABLE_WORKSPACES), if the preview was stored by a build without the gate, or if
    a link inside a flagged folder was retargeted in between. The
    flight recorder (core/blackbox/coverage/recorder.py:declared_paths_for:206-215) still joins a
    relative path onto the workspace. For a machine write given a relative or `~` path (a
    declared-path capability, core/blackbox/coverage/registry.py:58-63, 85), it snapshots and journals
    that workspace path (442-452, 705-741), never the file in the user's home that the write changes,
    so the Blackbox keeps no before-image of that file, yet the record says the effect can be rolled
    back (741). The same paths feed its high-risk check (`_high_risk_gate`, 328-331). The approval ask
    shows no path for machine.move_path or media.export, and shows an absolute Windows path for a
    workspace write as `<workspace>/C:\...`.
12. **The missing Windows write gate was closed after this freeze by PR #22 (merged 19 Oct 2026 by
    fast-forward to `1c26068`, checked with `git ls-remote`). PR #22 adds no peer check, so the
    missing loopback check below stays open. Below is the route as it stood at e8f607d. VERIFIED in
    source; run at e8f607d on Linux with the gate switched on, not on Windows.** The
    Media Studio editor's export, `POST /media-editor/export` (or a DELETE there: core/web/api/app.py:136
    hands both methods to the same dispatcher; core/web/api/service.py:_dispatch_post_inner:4217,
    branch 4236-4239 -> core/web/api/media_editor_api.py:handle_media_editor_post:282, export 307-328
    -> core/media_studio_service.py:export_project:248),
    writes the request's `output_path`, a stage file beside it for each edit, and the folders that
    hold them (VOOL creates those itself, 284 and 325), without asking the Windows write gate. Only
    the policy flag `media.editor_export_enabled` (media_editor_api.py:310, default True) and the Host and Origin checks
    every POST gets apply; there is no loopback check. The same export through the `media.export`
    tool is gated (gap 11). In the runs (the gate switched on by patching `_IS_WINDOWS`, one flagged
    folder, a stand-in media worker), the tool was refused with
    `windows_filesystem_authority_unavailable` and wrote nothing, while this route answered 200
    `exported` to a POST and to a DELETE and asked the worker to write a stage file and the output
    outside the flagged folder; for the POST, VOOL first created that folder itself. An export needs a project, and `open_project` (100) makes one only after the
    media worker probes the source (106), so this matters wherever the worker runs on Windows (gap 8).
    The action is also registered as the command `media.editor.export`
    (core/command_registry/groups/delegated_routes.py:505, handler `_media_editor_action`:455), but
    that handler passes `client_host` (461), which handle_media_editor_post does not accept, so
    through /api/commands/dispatch it fails with a TypeError (500) and writes nothing (same run). No
    test covers either route on Windows.
13. **VERIFIED in source; run at e8f607d on Linux with the gate switched on, not on Windows.** Operator
    notes never ask the Windows write gate. A typed save, archive or restore note, and the
    `operator.save_note` tool, write Markdown in the notes folder (core/operator/notes.py:create_note:153,
    written at 165; archive_note:340, written at 302, then the original note is deleted at 310;
    restore_note:402, written at 302, then the archived file is deleted at 310). Approving a calendar
    event proposed from a note rewrites that note (core/operator/calendar_provider.py:_finalize_verified:
    1754 -> _link_note:1502-1508 -> notes.py:link_note_to_event:483, written at 512). A save naming Apple
    Notes, and an Apple note append, rename or delete, keep a delivery journal and lock files there
    (notes.py:921-941, saved at 1233-1237), and a save that cannot reach the Notes app writes a fallback
    note (`_fallback_note_for_operation`:1697-1710). The operator lane asks the gate only for temp
    cleanup and moves (core/local_operator_actions.py:61); operator.save_note is not in `_GATED`
    (windows_write_gate.py:35), so `may_refuse` is False for it, and tests/test_windows_write_gate.py:37-39
    asserts the gate allows it. The gate's docstring counts notes among VOOL's private stores
    (windows_write_gate.py:16-17).
    The notes folder is `notes/` in the process's workspace (`_workspace_root`:43-58), not the chat's:
    `resolve_workspace_root` (core/runtime_paths.py:268-300) takes VOOL_WORKSPACE_ROOT when set
    (275-277); else `VOOL_HOME\workspace` when VOOL_PROJECT_ROOT is set (289-290) or an isolated runtime
    runs from its own source folder (296-297); else VOOL's working directory. On Windows the
    source-checkout launchers set VOOL_PROJECT_ROOT (Start_VOOL.bat:8, Talk_To_VOOL.bat:7,
    vool_background.vbs:10), so their notes go to `VOOL_HOME\workspace\notes`. The installed app does
    not: its launcher (installer/bundle/vool-launch.cmd, copied as VOOL.cmd by
    installer/bundle/build_bundle.ps1:150) sets only VOOL_HOME, to `%LOCALAPPDATA%\VOOL` unless one is
    already set (vool-launch.cmd:13-15), a home Windows does not count as isolated
    (runtime_paths.py:runtime_home_is_isolated:174-177), and the supervisor starts the API in `{app}\app`
    (installer/bundle/bundle_supervisor.py:638), which is PROJECT_ROOT (runtime_paths.py:14). So the
    installed app's notes go to `{app}\app\notes`, which uninstall deletes (installer/bundle/vool.iss:88).
    Either way notes land in VOOL's own folders, unless VOOL_WORKSPACE_ROOT names another folder or VOOL
    is started from another working directory; then they land there, ungated, though
    core/execution/constants.py:2409-2411 calls a note write a workspace mutation and the gate refuses
    `workspace.write_file` into an unflagged workspace.
    `_confined`:61 and `notes_root`:76 refuse `notes/` itself and each note file in it when it resolves
    outside the workspace (`_note_path`:102, `_iter_note_files`:136), but not `notes/.archive`:
    archive_note:210 and restore_note:225 use it as is, and link_note_to_event checks its stored path
    against the workspace only (494-499). If `notes/.archive` is a link to another folder, an archive writes
    through it (core/execution/artifacts.py:atomic_write_text:39 makes its temp file in the target's
    folder, 87, and renames it into place, 99) and a restore deletes the matched file it leads to (243).
    On Windows any user can make such a link as a junction (core/fs_links.py:3-6); no VOOL code makes one.
    In the runs (the gate switched on, one flagged folder, VOOL_WORKSPACE_ROOT outside it):
    `workspace.write_file` was refused and wrote nothing, while a typed save note returned `executed`
    and wrote `<workspace>/notes/<slug>.md`; approving a calendar event proposed from a note (a stand-in
    calendar server) returned `executed` and rewrote the note; and with `notes/.archive` a symbolic link
    to an outside folder (standing in for a junction), a typed archive moved the note into that folder,
    and a typed restore of "Budget" copied `outside/user-doc.md` (front matter `title: Budget`) into
    `notes/` and deleted it. The installed app's notes folder was traced in source, not run. No test runs
    a note with the Windows gate on, and none puts a link at `notes/.archive`.
14. **VERIFIED in source; run at e8f607d on Linux with the gate switched on, not on Windows.** The gate
    checks `repo.git` on the chat's workspace, not on the repository git changes. Its `_GATED` entry is
    empty (windows_write_gate.py:42), which means the target is the workspace itself (33), so `refusal`
    checks only the workspace root it is given (330): the chat's, from the executor
    (tool_intent_executor.py:1355) and from the dispatcher (runtime_execution_tools.py:3272, root from
    `_workspace_root`:4465). RepoOpsRuntime.git (core/repoops/plane.py:3240) loads the session its
    arguments name (`_require`:499-508, from VOOL's session journal) and runs git in the folder that
    session was opened on (`_runner`:696-699). Neither `_require` nor `_guard`:653 checks which chat or
    workspace calls; a code task checks the calling session but not its workspace
    (core/code_assistant/task_runtime.py:_require_owned:1451, 1456-1457; gap 2). Opening,
    inspecting and binding a session only read and are not gated (core/repoops/contracts.py:87, 108,
    131), and an HTTP chat turn names its own workspace in the request (service.py:7479-7487). Every
    repo.git operation writes (core/repoops/gitops.py): a new branch it switches to (362), a commit that
    stages every change in the repository with `git add -A` (376; repo.git names no paths,
    plane.py:3302, and its schema takes none, contracts.py:309-322) and commits it (385), a
    cherry-pick, revert or merge (398, 406, 417), a tag (428), and `restore`, which aborts a merge,
    cherry-pick, revert or rebase in progress (443). Git acts on the whole repository, so a session
    opened in a subfolder of a repository changes files outside that subfolder. In the runs
    (one flagged folder, a repository outside it, Auto and Manual mode): repo.git merge from a chat
    working in the repository was refused;
    the same session from a chat working in the flagged folder asked for approval, naming no affected
    folder, and once allowed merged in the outside repository. With only a subfolder of a repository
    flagged, an allowed merge from a chat working there changed a file at the repository's top level,
    and an allowed commit committed a tracked edit and an untracked file at the top level.
    repo.step writes through its inner intent, and its two folders are checked at different points.
    The early gate and the dispatch-time gate for the step itself check its inner intent on the chat's
    workspace (windows_write_gate.py:refusal:311-320, root from runtime_execution_tools.py:7399;
    tool_intent_executor.py:1355, runtime_execution_tools.py:3272). The step then dispatches its inner
    call with the session folder as the workspace (plane.py:_execute:3172-3174,
    _session_context:458-465), and the dispatch-time gate checks that folder. So a step writes only
    when both are flagged, and then as far as its inner intent reaches. That intent can be
    `workspace.apply_unified_diff` (plane.py:REPAIR_MUTATION_INTENTS:84-86), its inner call carries no
    reviewed destinations (runtime_execution_tools.py:3503, `_approved_destinations_for`:4505), and a
    session can be opened on a subfolder of a repository (plane.py:open:712, 723-735;
    gitops.py:is_repo:264-266), so a step has gap 15's reach. In the runs (the session's diagnosis and
    Gate 0 write verdict set directly, since a real diagnosis needs a command run): from a chat working
    in the repository, a repo.step write was refused with no permission decision; from a chat working
    in the flagged folder, Manual mode asked to approve `Run repo.step`, naming no affected folder, and
    once allowed the inner write was refused and wrote nothing (Auto mode refused it after its
    decision; gap 2); with the session opened inside the flagged folder, a chat working outside it was
    refused and a chat working in it wrote the file; and with the session opened on a flagged
    subfolder of a repository and a chat working there, a repo.step carrying gap 15's rename patch
    (Manual mode with the ask allowed, and Auto mode) removed a file at the repository's top and put
    its content in the flagged folder, answering `no_change` (engine `git_apply`).
    repo.push is not in `_GATED`; it pushes from the session folder after an owner-local authorization
    (plane.py:push:3720, git run at 3987-3988). No test runs repo.git with a folder flagged
    (tests/test_windows_write_gate.py:31 flags none).
15. **VERIFIED in source; run at e8f607d on Linux with the gate switched on, not on Windows.** The gate
    checks `workspace.apply_unified_diff` on the chat's workspace only, while `git apply` can move a
    file into it from outside. The intent's `_GATED` entry is empty (windows_write_gate.py:39), so
    `refusal` checks only the workspace root (330). VOOL checks each `---`/`+++` path of the patch by
    joining it onto the workspace (core/execution/workspace_tools.py:`_extract_patch_paths`:945, regex
    40; `resolve_workspace_path`:66, called at 377) and does not read `rename` or `copy` lines. A patch
    with no reviewed destinations (apply_unified_diff_workspace:340, reviewed branch 363) then goes to
    up to three engines in turn, each tried only if the one before did not apply it. First
    `git -C <workspace> apply` (437-444), when git is on the PATH and the workspace is in a git
    repository whose own config defines no program-running key (421-429;
    core/safe_git.py:executable_local_config:129): outside a repository that config read fails
    (safe_git.py:183, caught at 428-429), and a repository whose config sets, for example,
    `core.editor` or `core.hooksPath` skips git too (426-427). When the workspace is inside git's work
    tree, git reads a git-format patch's paths from the top of that work tree. That top is the folder
    holding `.git`, unless the repository's own config sets `core.worktree`. VOOL does not count that
    key, or `core.bare`, as program-running (safe_git.py: `_EXECUTABLE_KEY_SUFFIXES`:46-63,
    `_EXECUTABLE_KEY_PATTERNS`:70-82), so git still runs, and the top can then be any folder that
    contains the workspace, even when `.git` is inside the flagged folder. When `core.worktree` names a
    folder that does not contain the workspace, or that config sets `core.bare` to true, git reads the
    paths from the workspace itself. git places the paths of a patch with no `diff --git` line inside
    the workspace instead: VOOL passes no `-p` (437-438), and in the runs git dropped a leading
    `a/scratch/` or `scratch/` that named the flagged subfolder (with `-p1` added, the same
    `a/scratch/keep.txt` changed `scratch/scratch/keep.txt`). Second, VOOL's own engine (449-454), which
    joins each path onto the workspace and refuses one outside it (`_apply_unified_diff_python`:988,
    1008). It writes or removes each file as soon as it renders it (1065-1069), so in a patch with more
    than one file where a later file's `---` line directly follows the previous file's last hunk line, a
    later file that does not match (1047-1048, 1051-1053) or has no hunk (1060-1061) leaves the earlier files
    changed; VOOL then answers `stale_base` or `invalid_patch_syntax`
    (`_classify_patch_apply_failure`:959, 972-975) with no mutation record (472-479, before 547). It
    reads every line after a hunk header as part of that hunk, without counting, until the next `@@ ` or
    `--- ` line (1037-1040): a `+` line is added to the file (1055-1056), a space or `-` line must match the
    file's next line (1046-1054), a `\ No newline at end of file` line is skipped (1041-1043), and any other
    line, such as a `diff --git` line, an `Index:` line or a blank line, is refused before that hunk's
    file is written (1057-1058), so a patch with such a line before every file after the first changes
    nothing through it. Third, GNU `patch -d <workspace>` when `patch` is on the PATH (455-471); whether
    a Windows install has it is UNVERIFIED. VOOL strips the patch (347), so its last line loses its
    newline. git rejects the patch when that line is a hunk line (as corrupt), the `rename to` or
    `copy to` line of a last section that renames or copies with no hunk (its header lacks filename
    information), or the `new mode` line of a last section that only changes a mode (a patch with only
    garbage); such a patch goes to the other two engines. git still applied patches whose last line was
    a `\ No newline at end of file` line, the version line under the `-- ` signature that ends
    `git format-patch` output, a line of plain text after the last hunk, or a closing code fence.
    In the runs (one flagged folder that is a subfolder of a repository; Manual mode with the ask
    allowed, and Auto mode with no ask), a rename from a file at the repository's top into the flagged
    folder, in a patch ending with a `\ No newline at end of file` line, was applied by git: the file
    outside the flagged folder was removed and its content landed inside. This happened with and without
    `---`/`+++` lines for the rename, and with the file tracked or untracked. VOOL answered `no_change`
    (ok False, engine `git_apply`) and wrote no mutation record (525-545, before 547). The Manual ask
    named no file (`affected_resources` empty, no diff preview), as for any patch. git skipped a section
    deleting the outside file, and a copy left it in place (its content was copied in). In Auto mode git
    also applied the rename, and VOOL answered `no_change`, when the patch ended with a `-- ` signature,
    a line of plain text or a closing code fence, and when it was a whole `git format-patch` output; in
    that output VOOL's pattern (regex 40) crossed the line break after the `---` that ends the commit
    message and took the diffstat line below it as a path. The same rename ending in a hunk line was not
    applied: git rejected it as corrupt, and VOOL's engine and GNU patch, joining its paths onto the
    workspace, could not place it. Run directly with VOOL's git arguments (437-438), git 2.43.0 also
    rejected a stripped patch whose last section was a rename or a copy with no hunk, alone or after an
    edit as `git diff -M` or `git diff -C -C` writes it, or a section that only changed a mode, alone or
    after an edit; through VOOL, the copy and mode patches after an edit then failed in VOOL's engine
    and GNU patch as well (`stale_base`, nothing changed). With `core.editor` or `core.hooksPath` set in
    the repository's own config, git did not run and the outside file stayed.
    VOOL's own writes can make any flagged folder such a repository, because nothing refuses a `.git`
    path inside the workspace. The gate's entries for `workspace.ensure_directory`,
    `workspace.write_file` and `workspace.replace_in_file` are empty (windows_write_gate.py:36-38), so
    `refusal` checks only the workspace root (330); each of these writers refuses only a path outside
    the workspace or in VOOL's own state or running code
    (core/runtime_execution_tools.py:`_resolve_workspace_path`:4470;
    core/execution/workspace_tools.py:`resolve_workspace_path`:66;
    core/runtime_state_protection.py:refusal:117), with no rule for `.git`. Since `.git` is inside the
    flagged folder, a gate that checked each path where it lands would allow these writes too. In a
    flagged folder that was in no repository, Auto mode ran with no ask a `workspace.ensure_directory`
    of `.git/objects` and of `.git/refs`, and a `workspace.write_file` of `.git/HEAD` and of a
    `.git/config` setting `worktree` to the user's home folder two levels above; Manual mode asked for
    each, naming its `.git` path. git then took that home as the top, and a rename naming a file in its
    `Documents` folder moved it into the flagged folder, with VOOL answering `no_change` (engine
    `git_apply`), in Auto mode and in Manual mode with the asks allowed. With `worktree` set to a folder
    that does not contain the flagged folder, git looked up the rename's paths inside the flagged
    folder, not in that other folder (which held a `Documents/secret.txt` of its own), and applied
    nothing. In a flagged folder that was already a repository's top, `workspace.replace_in_file` added
    the key to `.git/config` with no ask in Auto mode, while a `workspace.write_file` of the whole file
    asked; with the key set, the same rename moved the file in (Auto mode, and Manual mode with the ask
    allowed).
    The `no_change` comes from where VOOL looks: it reads each `---`/`+++` path, less a leading `a/` or
    `b/` (`_extract_patch_paths`:952-953), joined onto the workspace before and after (403, 484-487) and
    compares the two (512), while git places a git-format patch's paths from the top of the work tree
    and drops a leading folder name from a plain patch's, so the files git changed are not the ones VOOL
    compared. The same happens to an edit that stays inside the flagged folder. A patch changing the
    flagged folder's `keep.txt`, named `scratch/keep.txt` from the repository's top, with or without a
    `diff --git` line, and ending in a line git accepts, was applied by git, and VOOL answered
    `no_change` with no mutation record (Manual mode with the ask allowed, and Auto mode); the rename
    runs' own edit of `keep.txt` landed the same way. The executor's ack then recorded the A6 row
    `failed_safe_to_retry` (reason `dispatch_ack`; tool_intent_executor.py:1574-1586), though its
    comment says every ordinary failure result was produced before any physical mutation could be
    attributed to it (1571-1573). `workspace.rollback_last_change` then answered `no_tracked_change`,
    or, after an earlier tracked write in the same session, rolled back that write and left the patched
    file changed (runtime_execution_tools.py:_rollback_last_change:6867, 6890). The same edit ending in
    a hunk line was not applied (`stale_base`), and a git-format patch naming `keep.txt` from the
    workspace was not applied either and also answered `no_change`. A plain patch naming `keep.txt` from
    the workspace and ending in a line git accepts was applied by git and answered `executed`; one
    naming `other/x.txt`, a file at the repository's top, was looked up inside the workspace, was not
    applied, and answered `stale_base` (Auto mode).
    With git off the PATH, GNU patch 2.7.6 refused renames and copies from `../` and into or out of a
    linked folder, and looked up a name from the repository's top inside the workspace; it still applied
    the patch's other hunks, leaving a changed file and a `.orig` copy in the flagged folder while VOOL
    answered `stale_base` with no mutation record (472-479), and the executor's ack recorded that run
    `failed_safe_to_retry`. VOOL's own engine leaves the same kind of result. A plain two-file patch
    whose second file did not match left the first file changed, with `stale_base` and no mutation
    record, both with neither git nor patch on the PATH and with git in a repository (git applied
    nothing); a second file with no hunk did the same with `invalid_patch_syntax`; with GNU patch on the
    PATH, patch then also ran and added a `.rej` file for the already changed first file. With neither
    on the PATH, the same two files with a `diff --git` line before each changed nothing. An ordinary
    patch ending in a hunk line applied through VOOL's own engine. The same rename from a chat working
    at the repository's top was refused. repo.step reaches the same writer (gap 14).
    The code-task path that passes reviewed destinations uses VOOL's own engine and refuses renames and
    copies (`_apply_protected_unified_diff`:730, `_GIT_UNSUPPORTED_HEADERS`:576). An approval migrated
    from an older runtime's code-task journal is bound from that journal's own recorded evidence: a
    read's hash, the resulting hash of an earlier VOOL write, or a hash its approved arguments named
    (core/code_assistant/journal_schema.py:upgrade_payload:181, 217-221, 279, 283-287;
    `mutation_target_paths`:105-111). It records no destinations; it is let through
    (task_runtime.py:_destination_drift:4252-4268) and dispatched without them
    (`_run_mutation`:3624-3631), so its patch goes to the three engines above. In a run with the
    recorded schema-2 journal (tests/fixtures/code_task_journals/v2_e064/approved_without_hash) changed
    so that it approved the rename patch after a read of `scratch/keep.txt`, the path the patch's
    `---`/`+++` lines name, joined onto the task's folder (a file made for the run; git edited the
    flagged folder's own `keep.txt`), git applied it (Auto mode, and Manual mode with the ask allowed):
    the outside file was removed, its content landed in the flagged folder, and VOOL answered
    `no_change` (engine `git_apply`). Whether an older runtime ever wrote an approved patch proposal is
    UNVERIFIED. No test runs a patch with a folder flagged.
