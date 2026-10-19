# Windows port contracts (Step 0)

Frozen at windows/v0.7.1-port e8f607d. Every file:function:line below was read at that commit.
**VERIFIED** means checked by hand in source at e8f607d. **UNVERIFIED** means not confirmed.
**FALSE** means an earlier inventory claim (taken at 0ac9f35) that source contradicts.
This is a description of what exists today. Nothing here is a fix.

Plan step expected to close each gap (section 6): 1 -> Step 3; 2 -> Step 3; 3 -> unassigned;
4 -> unassigned; 5 -> Step 2; 6 -> unassigned; 7 -> Step 3; 8 -> unassigned; 9 -> unassigned;
10 -> closed by PR #18; 11 -> closed by PR #20.

## 1. Frozen SHAs

| What | SHA | Status |
|---|---|---|
| Port (windows/v0.7.1-port) | `e8f607df55294d33af9b13419ef1532024045346` | includes PR #15 (merged 19 Oct 2026 by fast-forward to `4a440b6`), PR #18 (merged 19 Oct 2026 by fast-forward to `c86d013`) and PR #20 (merged 19 Oct 2026 by fast-forward to `e8f607d`). Checked locally: `ee8ced6` (the first freeze), `4a440b6` and `c86d013` (the previous freeze) are ancestors, and every commit after `710f3f1` has one parent |
| Upstream v0.7.1-beta (Parad0x-Labs/vool) | `baf115c251d89be61eb382141d0529a7ee1bbb40` | VERIFIED: tag `v0.7.1-beta` re-fetched 18 Oct 2026 for the earlier freeze, `git rev-parse FETCH_HEAD`; GitHub's compare baf115c...e8f607d (19 Oct 2026) reports status "ahead", behind_by 0, merge base baf115c, so it is an ancestor of e8f607d |

## 2. Capability and authority-route inventory

Common to every HTTP POST: Host allowlist (core/web/api/app.py:_dispatch:107) and cross-origin
Origin refusal (app.py:_dispatch:144-146). VERIFIED. "Owner" means a loopback TCP peer
(core/request_trust.py:is_loopback_host:114). On HTTP chat, `owner_local` is loopback AND
surface != "channel" (core/web/api/service.py:7371-7374). VERIFIED. Any local process passes a
loopback check; request_trust.py:19 says so ("any local process is treated as the owner").

| Route (effect) | Entry point | How authority is established | Test |
|---|---|---|---|
| Tool executor: runtime/operator/web/hive tools (files, processes, network) | core/tool_intent_executor.py:execute_tool_intent:239 -> _execute_tool_intent_impl:1062 | Windows write gate 1319, then mode controller `decide_tool_call` 1322 (core/mode_permission_policy.py:3586), then A6 reserve 1435 | tests/test_windows_write_gate_before_approval.py, tests/test_a6_effect_reconciliation.py |
| Fast paths and Blackbox rollback (files) | core/authorized_tool_execution.py:execute_authorized_runtime_tool:190 | Windows gate 232, then `authorize_runtime_tool` 237 when no decision is handed in | test_windows_write_gate_before_approval.py::test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision |
| Runtime dispatcher (files, network) | core/runtime_execution_tools.py:execute_runtime_tool:3610 -> _dispatch_runtime_tool:3112 | agent-team write gate (3640) and council seat fence (containment.evaluate, 3663) first, Windows gate again at 3268 | tests/test_windows_write_gate.py::test_the_real_dispatch_refuses_a_workspace_write_and_writes_nothing |
| Operator lane typed turns (move, temp cleanup, calendar) | core/agent_runtime/turn_dispatch.py:352 -> core/local_operator_actions.py:dispatch_operator_action:70 | parsed approval words plus a stored pending action id; Windows gate at 100, which refuses every temp cleanup (PR #20) | test_windows_write_gate.py::test_an_operator_move_is_refused_outside_flagged_folders |
| sandbox.run_command (process) | runtime_execution_tools.py:_run_command:7399 | mode controller, cwd check `sandbox_cwd_refusal`:7296, kernel sandbox. On Windows sandbox/job_runner.py:_no_kernel_isolation_message:775 says no backend exists, so it fails closed (UNVERIFIED end to end) | the message only: tests/test_job_runner.py::JobRunnerTests::test_windows_missing_backend_error_names_heuristic_only_and_wsl2; none for the refusal end to end |
| Plugins and MCP servers (process) | core/plugin_executor.py:confined_argv:254 | Windows refuses bare and heuristic_only (`_WINDOWS_REFUSES_BARE`:64, 280) | tests/test_windows_never_runs_unconfined_children.py::test_windows_refuses_heuristic_only |
| Process agents (process) | core/agent_team/coordinator.py:_launch_process:260 | Windows refuses (`_WINDOWS_REFUSES_PROCESS_AGENTS`:48) | test_windows_never_runs_unconfined_children.py::test_windows_refuses_to_start_a_process_agent |
| vool-browser lane (process, network, downloads) | runtime_execution_tools.py:3171 -> core/vool_browser/sessions.py:open_session:346 | mode controller only; no Windows check (gap 8) | none for Windows |
| Media worker (process) | core/media_worker_client.py:NebulaWorkerClient._spawn:121 | none on Windows (gap 8) | none for Windows |
| Git on user repos (process) | core/safe_git.py:hardened_argv:125 (also named `read_only_argv`, 134), used by execution/git_tools, repoops/gitops, user_rules, project_archaeology; workspace_tools builds its `git apply` from `hardening_args`:78 | refuses repo-config programs and hooks | tests/test_git_tools_repository_config_cannot_run_programs.py, tests/test_archaeology_git_is_hardened.py |
| /api/commands/dispatch (updates, approvals, team control, settings) | service.py:dispatch_post:3025, branch 3056-3060 -> core/command_registry/api.py:handle_commands_dispatch:36 | NO peer check; body `approval_context`; principal defaults to "operator" (gap 1) | none for a non-loopback peer |
| /api/update/install, restart, rollback | service.py:3167, loopback 3171 | loopback plus server-made press | tests/updater/test_final_p1.py::TestNoUpdateFromProseRemoteOrForgery::test_remote_host_cannot_press_install_or_restart |
| /api/mode: bypass, grant_chat_workspace, resolve_approval | service.py:4427, loopback 4431; bypass confirmation id single use, 60 s (4447, 4483); grant 4621; resolve 4720 | loopback | grant: tests/test_coding_approval_workflow_repairs.py::test_the_grant_still_requires_a_pending_ask (loopback only); a non-loopback peer, shown with a mode change: tests/test_mode_permission_policy.py::test_mode_endpoint_is_loopback_only_and_bypass_cannot_be_set_without_grant |
| /api/runtime/unresolved-effects/resolve (A6 truth) | service.py:4820, loopback 4833 | loopback | tests/test_a6_resolution_surface.py::test_resolve_is_owner_local_only |
| /api/repoops/authorize-push, authorize-forge-action (network, remote refs) | service.py:5428 (loopback 5433), 5448 (loopback 5454) -> core/web/api/repoops_api.py:authorize_push:84, authorize_forge_action:208 | loopback, minted authorization in a reserved trust key | tests/repoops/test_repoops_adversarial.py::test_a_push_without_an_authorization_is_refused_and_the_remote_does_not_move; peer: tests/repoops/test_repoops_served.py::test_authorize_push_is_owner_local_only (authorize-push only; an authorize-forge-action peer test is UNVERIFIED) |
| /api/plugins/lifecycle | service.py:5503, loopback 5507 | loopback | tests/repoops/test_repoops_served.py::test_the_lifecycle_console_is_owner_local_only |
| /api/wallet/* POST (money) | service.py:4243-4246 -> core/web/api/wallet_api.py:handle_wallet_post:236 | loopback 239, then for trusted doors core/wallet/caller_binding.py:refusal_reason:91 | tests/wallet/test_crypto_pilot_caller_binding.py::test_a_page_on_another_localhost_port_is_refused_at_every_trusted_door |
| /api/cloud/usepod/* incl. spend-approval (money) | service.py loopback 5004 -> _usepod_owner_action:3923 (spend branch 4021) | loopback; grant minted through the operator approval store | the shared loopback check, shown on refresh: tests/usepod/test_usepod_registration.py::test_owner_actions_hold_the_post_guard_suite (case not-owner-local); spend-approval from a non-loopback peer UNVERIFIED |
| pay.x402 tool (money) | core/execution/payment_tools.py:execute_payment_tool:27 | refuses; the spend brake is not consulted for the answer | tests/test_payment_tools.py::PayX402RetiredTests::test_a_granted_spend_brake_opens_nothing |
| wallet.spend tool (money) | runtime_execution_tools.py:_wallet_spend:2731 | retired: always a typed refusal | tests/test_wallet_spend.py::test_handler_refuses_typed_regardless_of_approval_flags |
| Brake /startx402 (money unfreeze) | core/vool_agent_brake.py:maybe_handle_agent_brake:109 | owner_local (133, 184) plus OS consent `_default_unfreeze_consent`:102 | tests/test_vool_agent_brake.py::test_startx402_denied_consent_keeps_spending_frozen |
| Mobile companion (/api/mobile/*) | service.py:4248 -> core/web/api/mobile_companion_api.py:handle_mobile_companion_post:1430 | single-use pairing code (sha256 compare, 586), Ed25519 device signatures (`_verify_ed25519`:438, used 609, 771), and per request the Device Link grant check (`registry.authorize` 740: the grant's Ed25519 self-signature, then HMAC-SHA256 under the grant's secret; core/device_link/protocol.py:GrantRegistry.authorize:337, 346, 366-370) | tests/mobile/test_mobile_companion_api.py::TestPairing::test_start_is_owner_local_only |

Inventory claims corrected here:
- FALSE: "/api/update/* no test found". The test named in the table exists.
- FALSE: "/api/mode grant_chat_workspace no test found". tests/test_coding_approval_workflow_repairs.py
  drives it through dispatch_post (helper at line 222). A non-loopback grant_chat_workspace test is
  UNVERIFIED; the endpoint's loopback check (service.py:4431) is tested with a mode change (see the table).
- FALSE as worded: "mobile companion: HMAC-signed pairing grant". The grant is signed with the
  desktop's Ed25519 authority key (core/device_link/protocol.py:issue_grant:161, signature 206), not
  with an HMAC. HMAC-SHA256 under the grant's bearer secret authenticates each request
  (GrantRegistry.authorize:337, check 366-370, called at mobile_companion_api.py:740). Pairing also
  needs a hashed single-use code (586) and an Ed25519 proof of possession (609).
- UNVERIFIED: team-lane model turns (core/agent_team) arrive over loopback. core/agent_team/model_agent.py:156 posts
  to `base_url + "/api/chat"`; where base_url comes from was not traced.
- VERIFIED, narrowed: `turn_author` (request_trust.py:TURN_AUTHOR_KEY) is read through
  `turn_is_owner_authored` (request_trust.py:171) at three places (web/api/runtime.py:2162,
  user_rules.py:1803, standing_instructions.py:632), all memory-like writes. It is also read directly
  at core/presentation_selection.py:702, where a non-owner turn only turns off reply reshaping.

## 3. Invariant -> enforcement -> test

Each enforcement point was read at e8f607d. Each test file and function exists there (checked with grep).

| Invariant | Enforcement (file:function) | Named test |
|---|---|---|
| Windows: user-file writes only inside VOOL_WINDOWS_DISPOSABLE_WORKSPACES | core/windows_write_gate.py:refusal:131 (`_GATED`:28); each keyed path is checked where its writer puts it (`_landing`:79), and a temp cleanup is refused outright (`_ALWAYS_REFUSED`:48) (gap 11, closed) | tests/test_windows_write_gate.py::test_no_flagged_folder_refuses_every_user_filesystem_write; tests/test_machine_writes_are_checked_where_they_land.py::test_a_flagged_chat_workspace_no_longer_lets_a_write_into_the_real_documents_folder, ::test_windows_refuses_a_temp_cleanup_even_from_a_flagged_folder |
| ...refused before the permission decision and before A6 (PR #15) | core/tool_intent_executor.py:_windows_write_refusal:886 (called 1319); core/runtime_execution_tools.py:windows_write_refusal:7338, windows_write_refusal_before_decision:7383; core/local_operator_actions.py:windows_write_refusal:55 | tests/test_windows_write_gate_before_approval.py::test_a_write_outside_the_flagged_folders_is_refused_before_approval_and_a6, ::test_an_operator_move_outside_the_flagged_folders_is_refused_before_approval |
| ...fast paths gated before the boundary decides | core/authorized_tool_execution.py:execute_authorized_runtime_tool:232 | test_windows_write_gate_before_approval.py::test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision |
| ...inside a flagged folder the write still reaches approval | same | test_windows_write_gate_before_approval.py::test_inside_a_flagged_workspace_the_write_still_reaches_approval |
| ...a council seat is gated on its re-rooted workspace | runtime_execution_tools.py:execute_runtime_tool:3707-3716 then _dispatch_runtime_tool:3268 | test_windows_write_gate_before_approval.py::test_a_council_seat_is_gated_on_the_workspace_it_is_re_rooted_to |
| ...a step is gated by its inner intent | windows_write_gate.py:refusal (`_STEPS`:44) | test_windows_write_gate.py::test_a_step_is_gated_by_its_inner_intent |
| Junctions and name-surrogate links are links | core/fs_links.py:is_link_stat:26, is_link:33; core/execution/artifacts.py:_open_pinned_directory:116 | tests/test_windows_junction_boundary.py::test_a_reviewed_write_through_a_junction_is_refused_and_writes_nothing |
| Git never runs a program named by repo config, hooks or env | core/safe_git.py:executable_local_config:89, active_hooks:107, hardened_argv:125 | tests/test_git_tools_repository_config_cannot_run_programs.py::test_fsmonitor_in_repository_config_is_refused_and_never_runs; tests/test_archaeology_git_is_hardened.py::test_every_archaeology_git_call_is_hardened_and_a_plain_repository_still_compares |
| No unconfined plugins, MCP servers or process agents on Windows | core/plugin_executor.py:confined_argv:254; core/agent_team/coordinator.py:_launch_process:260 | tests/test_windows_never_runs_unconfined_children.py::test_windows_still_refuses_the_default_mode, ::test_windows_refuses_to_start_a_process_agent |
| File locks work without fcntl | core/os_file_lock.py:lock_exclusive:37 | tests/test_os_file_lock.py::test_a_held_lock_excludes_another_process_until_released |
| A liveness probe never signals | core/process_liveness.py:pid_alive:18 | tests/test_process_liveness_never_kills.py::test_a_live_process_reads_alive_and_survives_the_probe |
| Installer, Stop and supervisor stop only this installation's VOOL processes (gap 10) | installer/bundle/vool.iss:StopNulla:91 (filter 112; run from PrepareToInstall 121 and InitializeUninstall 127); installer/vool_stop.py:kill_listener:114 (predicate 132-137), kill_api_by_pidfile:148 (predicate 164-169); installer/bundle/bundle_supervisor.py:_clear_stale_api_processes:325 (match 346-348) | tests/test_windows_installer_ownership.py::test_the_installer_never_stops_a_process_just_because_it_owns_the_api_port, ::test_the_installer_still_stops_vools_own_python_processes; tests/test_bundle_tooling.py::test_bundle_supervisor_clears_stale_api_processes_before_bundle_start; Windows only: tests/test_windows_installer_stops_only_this_install.py::test_every_bundle_stop_stops_only_this_install_for_real; tests/test_windows_stop_spares_foreign_listeners.py::test_the_stop_leaves_a_foreign_listener_running |
| Ledger names are ones Windows can create | core/execution/artifacts.py (`_WINDOWS_LEDGER_NAMES`:759) | tests/test_windows_mutation_ledger_names.py::test_a_key_windows_cannot_use_gets_a_digest_name_inside_the_ledger_dir |
| Window attaches only to the supervisor's API process | installer/bundle/bundle_supervisor.py; installer/bundle/native_runtime_supervisor.py (`vouching_pid`:82, env 125) | tests/test_windows_window_attaches_to_supervised_api.py::test_the_window_is_told_the_exact_api_process_this_supervisor_started; tests/test_windows_window_vouched_attach.py::test_it_attaches_only_to_the_process_the_supervisor_started |
| Uninstall removes only program files | installer/bundle/vool.iss [UninstallDelete]:84-88 | tests/test_windows_uninstall_removes_program_files.py::test_every_uninstall_delete_stays_inside_the_program_folder |
| Supervisor state stays out of the program folder | installer/bundle/bundle_supervisor.py:main:456 (VOOL_HOME default 460) | tests/test_windows_supervisor_keeps_state_out_of_the_program_folder.py::test_a_direct_stop_runs_its_preflight_in_the_native_home |
| The model is never an effect-truth source | core/runtime_continuity.py:resolve_unresolved_effect:4160 (refusal 4175) | tests/test_a6_effect_reconciliation.py::ModelCannotResolve::test_model_source_resolution_is_refused |

## 4. Receipt states

| State | Status literals today (all VERIFIED present in source) |
|---|---|
| refused (never dispatched) | `windows_filesystem_authority_unavailable`, `sandbox_unavailable`, `blocked_by_policy`, `blocked_by_mode`, `blocked_by_hard_link`, `cwd_outside_workspace`, `confinement_unavailable`, `refused_repository_config`, `permission_denied`, `user_action_required`, `simulate_only`, `pending_approval` (waiting, not executed). A blocked retry: `unknown_reconciliation_required` with `blocked_pending_reconciliation: True`, `executed: False` (tool_intent_executor.py:_a6_unknown_blocked_execution:990) |
| committed | `executed`; A6 row `applied`; contacts `committed`; Blackbox `rollback_committed` |
| committed-but-cleanup-failed | **No distinct state today.** VERIFIED. See below |
| outcome_unknown | `unknown_reconciliation_required` / mode `reconciliation_pending` (core/effect_reconciliation.py:33-34); A6 row `unknown`, reason `post_dispatch_exception` or `crash_recovery_dispatch_lease_expired`; operator lane `outcome_unproven` |

What a "committed, then cleanup or recording failed" case collapses into, by path (VERIFIED in code):
- A handler raises after the A6 claim: runtime_execution_tools.py:_dispatch_runtime_tool:3560-3570
  re-raises as `EffectOutcomeUnknown(reason="post_dispatch_exception")`; the executor
  (tool_intent_executor.py:1483-1494) writes A6 `unknown`. Result: **outcome_unknown**.
- A handler raises with no A6 claim (no checkpoint_id, or an intent that is not reserved):
  the same handler returns `ok=False, status="error"` (runtime_execution_tools.py:3575).
  Result: a **plain failure**, even if the effect landed.
- A handler returns `ok=False` after it changed files: the executor ack
  (tool_intent_executor.py:1538-1549) maps every non-unknown failure to A6 `failed_safe_to_retry`
  (reason `dispatch_ack`). Example: an approved multi-file patch whose later write fails and whose
  compensation also fails returns `ok=False`, `status` `write_failed`/`destination_changed`/
  `stale_base`, `restore_complete: False`, non-empty `changed_paths`
  (core/execution/workspace_tools.py:_apply_protected_unified_diff:754-764,
  _compensate_protected_writes:596). If that result crosses the executor ack with a claim, it is
  recorded **failed_safe_to_retry**. Whether this path reaches the ack with a claim is UNVERIFIED.
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
   compare-and-set; a lost claim is treated as blocked (tool_intent_executor.py:1447-1457). VERIFIED.
3. Exception. runtime_execution_tools.py:3560-3570 -> `EffectOutcomeUnknown`
   (effect_reconciliation.py:61) -> executor classify `unknown` (tool_intent_executor.py:1483-1494). VERIFIED.
4. Lease expiry. Lease is 900 s (runtime_continuity.py:3685). runtime_continuity.py:
   sweep_stale_unresolved_effects:3811 and the inline sweep in reserve: stale PREPARED ->
   `expired_pre_dispatch`, stale DISPATCHED -> `unknown` with `crash_recovery_dispatch_lease_expired`. VERIFIED.
5. Unknown blocks identical redispatch while prepared, dispatched or unknown (step 1). VERIFIED.
6. Resolution. runtime_continuity.py:resolve_unresolved_effect:4160; sources mechanical, provider,
   user (`_VALID_RESOLUTION_SOURCES`:3687); "model" refused (4175). Resolvers are registered for
   workspace.write_file and machine.write_file (effect_reconciliation.py:219-220), repo.push
   (core/repoops/plane.py:4463, run at import 4585) and wallet.pay (core/wallet/reconciliation.py:88).
   User source via /api/runtime/unresolved-effects/resolve (section 2). Fall-through lanes resolve
   mechanically (tool_intent_executor.py:_dispatch_with_claim:1629, resolve 1648-1653; the variant at
   1668 is the one used when there is no claim). VERIFIED.

Tests (exist): tests/test_a6_effect_reconciliation.py::R3R4AmbiguousBecomesUnknownAndBlocksRetry::test_mutate_then_raise_records_durable_unknown_and_blocks_fresh_retry,
::R5UnknownSurvivesRestart::test_durable_unknown_still_blocks_after_restart,
::CrashWindows::test_c2_crash_before_dispatch_expires_and_allows_fresh_dispatch,
::CrashWindows::test_c3_c4_lost_dispatch_becomes_unknown_and_blocks,
::R10ConcurrentReserve::test_concurrent_identical_effects_cannot_double_dispatch,
::R17ResultFailureIsFailedSafeToRetry::test_invalid_arguments_result_classifies_failed_safe_to_retry;
tests/test_a6_reservation_gap_convergence.py::FallThroughReservationGap::test_all_eight_gap_shapes_reserve_claim_and_reconcile;
tests/test_a6_resolution_surface.py::test_user_resolution_releases_the_block.

## 6. Gaps (listed, not fixed)

1. **VERIFIED.** `/api/commands/dispatch` (service.py:3056-3060) calls `handle_commands_dispatch(body)`
   with no peer check, unlike /api/update/* (3171) and /api/mode (4431). `approval_context` comes from
   the body (command_registry/api.py:44, 50). `ExecutionContext.principal` defaults to "operator"
   (command_registry/execute.py:45) and the API projection sets no `extra`. `_gate_two_press`
   (command_registry/groups/convergence.py:55-59) passes on any non-empty `gesture` or `authority_token`;
   it gates update.apply, update.restart, update.rollback. Correction to the inventory (FALSE in part):
   `approvals.resolve` is gated by `_gate_resolve` (groups/approvals.py:48-51, principal == operator),
   not by two-press; it still passes because the principal defaults to operator. Also
   `delegated_routes._peer` (groups/delegated_routes.py:55-56) returns "127.0.0.1" when no client_host
   is passed, so `_gate_owner_local` passes on this projection for any peer. Host and Origin checks
   still apply. No test covers a non-loopback peer here.
2. **VERIFIED. Fixed by PR #15 (merged)** for the executor (tool_intent_executor.py:1319, before
   `decide_tool_call` 1322 and A6 reserve 1435), the operator lane (same call, operator branch 900)
   and the fast paths (authorized_tool_execution.py:232). PR #15 also leaves a call the gate never
   refuses (off Windows, or an ungated intent) as it was: `may_refuse` (windows_write_gate.py:119)
   returns first at tool_intent_executor.py:898 and runtime_execution_tools.py:7353
   (test_windows_write_gate_before_approval.py::test_a_call_the_gate_never_refuses_is_handled_exactly_as_before_the_early_gate).
   Residual by design: a council seat.
   `windows_write_refusal_before_decision` (runtime_execution_tools.py:7383-7396) returns None when
   `council.containment.binding_for_context` finds a seat, because only `execute_runtime_tool` re-roots
   the seat onto its own workspace (3707-3716). For a seat the decision and A6 reserve/claim happen
   first; the gate runs at dispatch (3268). If the seat's workspace is not flagged, the refusal comes
   back `ok=False` and the A6 row is classified `failed_safe_to_retry` / `dispatch_ack` (1538-1549).
   Only the allowed seat case is tested. UNVERIFIED: other `decide_tool_call` callers
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
   cleanup <id>", core/operator/handlers.py:330; parser core/operator/parser.py:166, 335) dispatch at
   core/agent_runtime/turn_dispatch.py:352. No `owner_local` / `request_is_owner_local` reference exists
   in turn_dispatch.py, operator/parser.py, operator/handlers.py or local_operator_actions.py. The
   inventory path core/turn_dispatch.py is FALSE (the file is core/agent_runtime/turn_dispatch.py).
   Whether a non-owner turn is filtered before this lane was not traced.
8. **VERIFIED in source.** core/media_worker_client.py:_spawn:121-136 starts `python -m nebula.worker`
   with no Windows confinement check. `vool-browser.*` is dispatched at runtime_execution_tools.py:3171,
   before the Windows gate at 3268. core/vool_browser/engine.py:find_engine_binary:145 accepts any
   executable named by `VOOL_BROWSER_ENGINE`; sessions.py:_own_engine:139 launches it via Playwright with
   `accept_downloads=True` and no Windows check. UNVERIFIED: whether nebula, Playwright or a Chromium
   binary is present in the Windows bundle.
9. **VERIFIED, and the inventory was incomplete.** project_archaeology was fixed in 95f3a51. Raw
   `git` without safe_git remains in core/repoops/identity.py:25, bootstrap.py:50, localgit.py:102,
   local_lifecycle.py:177, and also repoops/archaeology.py (imports identity). None of these is
   imported by production code; only scripts/*_demo.py import them. Other raw git calls exist:
   core/council/api.py:_workspace_sha:78 runs `git rev-parse HEAD` in a council workspace;
   core/agent_runtime/continuity_gate.py:209 in a `repo_dir`; the rest target VOOL's own checkout
   (runtime_execution_tools.py:562, web/api/runtime.py:181, build_provenance.py:27, kernel/repl.py:5136,
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
    supervisor's stale-API cleanup (installer/bundle/bundle_supervisor.py:_clear_stale_api_processes:325,
    called at 318, just before the API's `_start` at 319) stops a python.exe or pythonw.exe that runs
    `<root>\app\apps\vool_api_server.py` as its script (346-348), but never its own PowerShell or its
    own ancestors (338-345). Tests that run everywhere:
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
    --project-root, the supervisor's --root after `resolve()`, bundle_supervisor.py:46, 470), so a VOOL
    process started through a junction, a subst drive or an 8.3 short name of the same folder can be
    missed. It is then left running and nothing outside the install is stopped (fails safe). No test
    covers it. Setup's filter takes only python.exe and pythonw.exe (vool.iss:112), so it does not stop
    `{app}\ollama\ollama.exe`, which the supervisor starts (bundle_supervisor.py:306). Start_VOOL.bat:95
    still stops every python.exe or pythonw.exe whose command line contains `apps.vool_api_server` or
    `vool_api_server.py`, from any folder, when the API on 11435 fails its health check (89-94).
11. **Closed by PR #20. VERIFIED in source; the new tests also run on Windows (the gauntlet).** At
    c86d013 the gate joined every relative keyed target onto the chat workspace, while the writers
    place the same arguments elsewhere. With the chat workspace inside a flagged folder,
    `machine.write_file` with path `Documents\x.txt` passed the gate and was written to the user's
    real Documents folder, and the Manual-mode approval ask named `<workspace>\Documents\x.txt`.
    Now `refusal` (windows_write_gate.py:131) places each keyed path where its writer puts it, read
    as that writer receives it (`_landing`:79). machine.write_file and machine.ensure_directory go
    through `anchor_machine_path` (runtime_execution_tools.py:4602), which their writers use too
    (`_resolve_safe_machine_directory`:4621, through `_resolve_machine_directory`:4591, called 5652
    and 5586): an alias such as "documents" is that folder in the user's home, a `~` path expands
    there, and another relative path is taken from the home. machine.move_path goes through
    core/machine_file_ops.py:_canonicalize:93, from the process's working directory. media.export
    goes through `export_destination` (core/media_studio_service.py:228, used by `export_project` at
    257), unstripped and made absolute so the separate media worker writes that same file, together
    with the folder that holds the output, where the export also writes its stage files.
    operator.move_path is checked after `$VAR`, `%VAR%` (Windows) and `~` are expanded, as its handler
    expands them (core/operator/storage.py:131-132), and only when the result is absolute. A path the
    gate cannot place, a path holding a NUL (`_inside`, 64) and a keyed intent with no rule are
    refused. Windows refuses operator.cleanup_temp_files outright (`_ALWAYS_REFUSED`:48, checked at
    151): a cleanup also empties TMPDIR, TMP, TEMP and gettempdir()
    (core/operator/storage.py:candidate_cleanup_roots:136), and its approval empties the plan a disk
    scan stored earlier. The approval ask shows a machine write's path where `anchor_machine_path`
    puts it (core/mode_permission_policy.py:_machine_path_display:3070, from
    `_affected_resources_display`:3030). The writers' own checks still apply on top: the machine
    writers accept only Desktop, Downloads and Documents in the home (runtime_execution_tools.py:4626-4630),
    machine.move_path needs both paths in those folders or the workspace (machine_file_ops.py:176-185)
    plus an OS consent (191-200), and media.export's writer has no root check of its own. Tests, also
    run on Windows by tools/windows_fresh_host_gauntlet.ps1:
    tests/test_machine_writes_are_checked_where_they_land.py::test_a_flagged_chat_workspace_no_longer_lets_a_write_into_the_real_documents_folder,
    ::test_an_export_writes_only_where_the_gate_checked, ::test_windows_refuses_a_temp_cleanup_even_from_a_flagged_folder,
    ::test_the_approval_ask_names_the_file_a_machine_write_changes.
    Known residuals, not fixed (VERIFIED in source, impact UNVERIFIED): the gate checks paths, not
    handles. On Windows it judges by its text a path `realpath` cannot resolve, such as a DOS device
    name inside a flagged folder or a path over 32,767 characters. Temp cleanup, and an operator move
    approved by its id alone (which carries no paths), are refused on Windows. The flight recorder
    (core/blackbox/coverage/recorder.py:declared_paths_for:206) still joins a relative path onto the
    workspace, so it can journal a file the write never touched. The approval ask shows no path for
    machine.move_path or media.export, and shows an absolute Windows path for a workspace write as
    `<workspace>/C:\...`.
