# Windows port contracts (Step 0)

Frozen at port commit ee8ced6. Every file:function:line below was read at that commit.
**VERIFIED** means checked by hand in source at ee8ced6. **UNVERIFIED** means not confirmed.
**FALSE** means an earlier inventory claim (taken at 0ac9f35) that source contradicts.
This is a description of what exists today. Nothing here is a fix.

Plan step expected to close each gap (section 6): 1 -> Step 3; 2 -> Step 3; 3 -> unassigned;
4 -> unassigned; 5 -> Step 2; 6 -> unassigned; 7 -> Step 3; 8 -> unassigned; 9 -> unassigned;
10 -> unassigned.

## 1. Frozen SHAs

| What | SHA | Status |
|---|---|---|
| Port (windows/port-write-gate-order, PR #15) | `ee8ced6e2418362580826247eb600727161ce058` | parent is windows/v0.7.1-port `710f3f16181c050a5d4bf550ed28c5b3b05b7f4a` |
| Upstream v0.7.1-beta (Parad0x-Labs/vool) | `baf115c251d89be61eb382141d0529a7ee1bbb40` | VERIFIED: re-fetched tag `v0.7.1-beta`, `git rev-parse FETCH_HEAD`; it is an ancestor of ee8ced6 |

## 2. Capability and authority-route inventory

Common to every HTTP POST: Host allowlist (core/web/api/app.py:_dispatch:107) and cross-origin
Origin refusal (app.py:_dispatch:144-146). VERIFIED. "Owner" means a loopback TCP peer
(core/request_trust.py:is_loopback_host:114). On HTTP chat, `owner_local` is loopback AND
surface != "channel" (core/web/api/service.py:7371-7374). VERIFIED. Any local process passes a
loopback check; request_trust.py:19 says so ("any local process is treated as the owner").

| Route (effect) | Entry point | How authority is established | Test |
|---|---|---|---|
| Tool executor: runtime/operator/web/hive tools (files, processes, network) | core/tool_intent_executor.py:execute_tool_intent:239 -> _execute_tool_intent_impl:1058 | Windows write gate 1315, then mode controller `decide_tool_call` 1318 (core/mode_permission_policy.py:3569), then A6 reserve 1431 | tests/test_windows_write_gate_before_approval.py, tests/test_a6_effect_reconciliation.py |
| Fast paths and Blackbox rollback (files) | core/authorized_tool_execution.py:execute_authorized_runtime_tool:190 | Windows gate 232, then `authorize_runtime_tool` 237 when no decision is handed in | test_windows_write_gate_before_approval.py::test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision |
| Runtime dispatcher (files, network) | core/runtime_execution_tools.py:execute_runtime_tool:3597 -> _dispatch_runtime_tool:3099 | council seat fence first (containment.evaluate), Windows gate again at 3255 | tests/test_windows_write_gate.py::test_the_real_dispatch_refuses_a_workspace_write_and_writes_nothing |
| Operator lane typed turns (move, temp cleanup, calendar) | core/agent_runtime/turn_dispatch.py:352 -> core/local_operator_actions.py:dispatch_operator_action:70 | parsed approval words plus a stored pending action id; Windows gate at 100 | test_windows_write_gate.py::test_an_operator_move_is_refused_outside_flagged_folders |
| sandbox.run_command (process) | runtime_execution_tools.py:_run_command:7382 | mode controller, cwd check `sandbox_cwd_refusal`:7283, kernel sandbox. On Windows sandbox/job_runner.py:_no_kernel_isolation_message:775 says no backend exists, so it fails closed (UNVERIFIED end to end) | none found for the Windows refusal |
| Plugins and MCP servers (process) | core/plugin_executor.py:confined_argv:254 | Windows refuses bare and heuristic_only (`_WINDOWS_REFUSES_BARE`:64, 280) | tests/test_windows_never_runs_unconfined_children.py::test_windows_refuses_heuristic_only |
| Process agents (process) | core/agent_team/coordinator.py:_launch_process:260 | Windows refuses (`_WINDOWS_REFUSES_PROCESS_AGENTS`:48) | test_windows_never_runs_unconfined_children.py::test_windows_refuses_to_start_a_process_agent |
| vool-browser lane (process, network, downloads) | runtime_execution_tools.py:3158 -> core/vool_browser/sessions.py:open_session:345 | mode controller only; no Windows check (gap 8) | none for Windows |
| Media worker (process) | core/media_worker_client.py:NebulaWorkerClient._spawn:121 | none on Windows (gap 8) | none for Windows |
| Git on user repos (process) | core/safe_git.py:hardened_argv:125, used by execution/git_tools, workspace_tools, repoops/gitops, user_rules, project_archaeology | refuses repo-config programs and hooks | tests/test_git_tools_repository_config_cannot_run_programs.py, tests/test_archaeology_git_is_hardened.py |
| /api/commands/dispatch (updates, approvals, team control, settings) | service.py:dispatch_post:3025, branch 3056-3060 -> core/command_registry/api.py:handle_commands_dispatch:36 | NO peer check; body `approval_context`; principal defaults to "operator" (gap 1) | none for a non-loopback peer |
| /api/update/install, restart, rollback | service.py:3167, loopback 3171 | loopback plus server-made press | tests/updater/test_final_p1.py::TestNoUpdateFromProseRemoteOrForgery::test_remote_host_cannot_press_install_or_restart |
| /api/mode: bypass, grant_chat_workspace, resolve_approval | service.py:4427, loopback 4431; bypass confirmation id single use, 60 s (4447, 4483); grant 4621; resolve 4720 | loopback | grant: tests/test_coding_approval_workflow_repairs.py::test_the_grant_still_requires_a_pending_ask (loopback only) |
| /api/runtime/unresolved-effects/resolve (A6 truth) | service.py:4820, loopback 4833 | loopback | tests/test_a6_resolution_surface.py::test_resolve_is_owner_local_only |
| /api/repoops/authorize-push, authorize-forge-action (network, remote refs) | service.py:5428 (loopback 5433), 5448 (loopback 5454) -> core/web/api/repoops_api.py:authorize_push:84, authorize_forge_action:208 | loopback, minted authorization in a reserved trust key | tests/repoops/test_repoops_adversarial.py::test_a_push_without_an_authorization_is_refused_and_the_remote_does_not_move; peer test UNVERIFIED |
| /api/plugins/lifecycle | service.py:5503, loopback 5507 | loopback | UNVERIFIED |
| /api/wallet/* POST (money) | service.py:4243-4246 -> core/web/api/wallet_api.py:handle_wallet_post:236 | loopback 239, then for trusted doors core/wallet/caller_binding.py:refusal_reason:91 | tests/wallet/test_crypto_pilot_caller_binding.py::test_a_page_on_another_localhost_port_is_refused_at_every_trusted_door |
| /api/cloud/usepod/* incl. spend-approval (money) | service.py loopback 5004 -> _usepod_owner_action:3923 (spend branch 4021) | loopback; grant minted through the operator approval store | UNVERIFIED |
| pay.x402 tool (money) | core/execution/payment_tools.py:execute_payment_tool:27 | refuses; the spend brake is not consulted for the answer | tests/test_payment_tools.py::test_a_granted_spend_brake_opens_nothing |
| wallet.spend tool (money) | runtime_execution_tools.py:_wallet_spend:2718 | retired: always a typed refusal | UNVERIFIED |
| Brake /startx402 (money unfreeze) | core/vool_agent_brake.py:maybe_handle_agent_brake:109 | owner_local (133, 184) plus OS consent `_default_unfreeze_consent`:102 | tests/test_vool_agent_brake.py::test_startx402_denied_consent_keeps_spending_frozen |
| Mobile companion (/api/mobile/*) | service.py:4248 -> core/web/api/mobile_companion_api.py:handle_mobile_companion_post:1430 | single-use pairing code (sha256 compare, 586), Ed25519 device signatures (`_verify_ed25519`:438, used 609, 771) | tests/mobile/test_mobile_companion_api.py::test_start_is_owner_local_only |

Inventory claims corrected here:
- FALSE: "/api/update/* no test found". The test named in the table exists.
- FALSE: "/api/mode grant_chat_workspace no test found". tests/test_coding_approval_workflow_repairs.py
  drives it through dispatch_post (helper at line 222). A non-loopback refusal test is UNVERIFIED.
- FALSE: "mobile companion: HMAC-signed pairing grant". Verification is Ed25519 device signatures
  plus a hashed single-use code. No HMAC signing of a grant was found.
- UNVERIFIED: team-lane model turns (core/agent_team) arrive over loopback. core/agent_team/model_agent.py:156 posts
  to `base_url + "/api/chat"`; where base_url comes from was not traced.
- VERIFIED, narrowed: `turn_author` (request_trust.py:TURN_AUTHOR_KEY) is read at three places
  (web/api/runtime.py:2162, user_rules.py:1803, standing_instructions.py:632), all memory-like writes.

## 3. Invariant -> enforcement -> test

Each enforcement point was read at ee8ced6. Each test file and function exists (checked with ls/grep).

| Invariant | Enforcement (file:function) | Named test |
|---|---|---|
| Windows: user-file writes only inside VOOL_WINDOWS_DISPOSABLE_WORKSPACES | core/windows_write_gate.py:refusal:66 (`_GATED`:26) | tests/test_windows_write_gate.py::test_no_flagged_folder_refuses_every_user_filesystem_write |
| ...refused before the permission decision and before A6 (new at ee8ced6) | core/tool_intent_executor.py:_windows_write_refusal:886 (called 1315); core/runtime_execution_tools.py:windows_write_refusal:7325, windows_write_refusal_before_decision:7366; core/local_operator_actions.py:windows_write_refusal:55 | tests/test_windows_write_gate_before_approval.py::test_a_write_outside_the_flagged_folders_is_refused_before_approval_and_a6, ::test_an_operator_move_outside_the_flagged_folders_is_refused_before_approval |
| ...fast paths gated before the boundary decides | core/authorized_tool_execution.py:execute_authorized_runtime_tool:232 | test_windows_write_gate_before_approval.py::test_a_fast_path_write_is_gated_before_the_boundary_takes_its_decision |
| ...inside a flagged folder the write still reaches approval | same | test_windows_write_gate_before_approval.py::test_inside_a_flagged_workspace_the_write_still_reaches_approval |
| ...a council seat is gated on its re-rooted workspace | runtime_execution_tools.py:execute_runtime_tool:3694-3703 then _dispatch_runtime_tool:3255 | test_windows_write_gate_before_approval.py::test_a_council_seat_is_gated_on_the_workspace_it_is_re_rooted_to |
| ...a step is gated by its inner intent | windows_write_gate.py:refusal (`_STEPS`:42) | test_windows_write_gate.py::test_a_step_is_gated_by_its_inner_intent |
| Junctions and name-surrogate links are links | core/fs_links.py:is_link_stat:26, is_link:33; core/execution/artifacts.py:_open_pinned_directory:116 | tests/test_windows_junction_boundary.py::test_a_reviewed_write_through_a_junction_is_refused_and_writes_nothing |
| Git never runs a program named by repo config, hooks or env | core/safe_git.py:executable_local_config:89, active_hooks:107, hardened_argv:125 | tests/test_git_tools_repository_config_cannot_run_programs.py::test_fsmonitor_in_repository_config_is_refused_and_never_runs; tests/test_archaeology_git_is_hardened.py::test_every_archaeology_git_call_is_hardened_and_a_plain_repository_still_compares |
| No unconfined plugins, MCP servers or process agents on Windows | core/plugin_executor.py:confined_argv:254; core/agent_team/coordinator.py:_launch_process:260 | tests/test_windows_never_runs_unconfined_children.py::test_windows_still_refuses_the_default_mode, ::test_windows_refuses_to_start_a_process_agent |
| File locks work without fcntl | core/os_file_lock.py:lock_exclusive:37 | tests/test_os_file_lock.py::test_a_held_lock_excludes_another_process_until_released |
| A liveness probe never signals | core/process_liveness.py:pid_alive:18 | tests/test_process_liveness_never_kills.py::test_a_live_process_reads_alive_and_survives_the_probe |
| Installer and stop touch only VOOL processes | installer/bundle/vool.iss (process stop, 95-102); installer/vool_stop.py:kill_listener:100 | tests/test_windows_installer_ownership.py::test_the_installer_never_stops_a_process_just_because_it_owns_the_api_port; tests/test_windows_stop_spares_foreign_listeners.py::test_the_stop_leaves_a_foreign_listener_running |
| Ledger names are ones Windows can create | core/execution/artifacts.py (`_WINDOWS_LEDGER_NAMES`:759) | tests/test_windows_mutation_ledger_names.py::test_a_key_windows_cannot_use_gets_a_digest_name_inside_the_ledger_dir |
| Window attaches only to the supervisor's API process | installer/bundle/bundle_supervisor.py; installer/bundle/native_runtime_supervisor.py (`vouching_pid`:82, env 125) | tests/test_windows_window_attaches_to_supervised_api.py::test_the_window_is_told_the_exact_api_process_this_supervisor_started; tests/test_windows_window_vouched_attach.py::test_it_attaches_only_to_the_process_the_supervisor_started |
| Uninstall removes only program files | installer/bundle/vool.iss [UninstallDelete]:84-88 | tests/test_windows_uninstall_removes_program_files.py::test_every_uninstall_delete_stays_inside_the_program_folder |
| Supervisor state stays out of the program folder | installer/bundle/bundle_supervisor.py:main:431 (VOOL_HOME default 435) | tests/test_windows_supervisor_keeps_state_out_of_the_program_folder.py::test_a_direct_stop_runs_its_preflight_in_the_native_home |
| The model is never an effect-truth source | core/runtime_continuity.py:resolve_unresolved_effect:4160 (refusal 4175) | tests/test_a6_effect_reconciliation.py::ModelCannotResolve::test_model_source_resolution_is_refused |

## 4. Receipt states

| State | Status literals today (all VERIFIED present in source) |
|---|---|
| refused (never dispatched) | `windows_filesystem_authority_unavailable`, `sandbox_unavailable`, `blocked_by_policy`, `blocked_by_mode`, `blocked_by_hard_link`, `cwd_outside_workspace`, `confinement_unavailable`, `refused_repository_config`, `permission_denied`, `user_action_required`, `simulate_only`, `pending_approval` (waiting, not executed). A blocked retry: `unknown_reconciliation_required` with `blocked_pending_reconciliation: True`, `executed: False` (tool_intent_executor.py:_a6_unknown_blocked_execution:986) |
| committed | `executed`; A6 row `applied`; contacts `committed`; Blackbox `rollback_committed` |
| committed-but-cleanup-failed | **No distinct state today.** VERIFIED. See below |
| outcome_unknown | `unknown_reconciliation_required` / mode `reconciliation_pending` (core/effect_reconciliation.py:33-34); A6 row `unknown`, reason `post_dispatch_exception` or `crash_recovery_dispatch_lease_expired`; operator lane `outcome_unproven` |

What a "committed, then cleanup or recording failed" case collapses into, by path (VERIFIED in code):
- A handler raises after the A6 claim: runtime_execution_tools.py:_dispatch_runtime_tool:3547-3557
  re-raises as `EffectOutcomeUnknown(reason="post_dispatch_exception")`; the executor
  (tool_intent_executor.py:1479-1490) writes A6 `unknown`. Result: **outcome_unknown**.
- A handler raises with no A6 claim (no checkpoint_id, or an intent that is not reserved):
  the same handler returns `ok=False, status="error"` (runtime_execution_tools.py:3562).
  Result: a **plain failure**, even if the effect landed.
- A handler returns `ok=False` after it changed files: the executor ack
  (tool_intent_executor.py:1534-1545) maps every non-unknown failure to A6 `failed_safe_to_retry`
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

1. Reserve. tool_intent_executor.py:_a6_reserve_before_dispatch:928 runs only when
   checkpoint_id is present and `_a6_reservation_required`:820 says so (943). It blocks on an
   active row (946-951), then calls core/runtime_continuity.py:reserve_logical_effect:3882:
   BEGIN IMMEDIATE (3915), lazy lease sweep (3919-3943), active-row check, insert. Backed by the
   partial unique index `ux_unresolved_active_effect` (storage/migrations.py:1243). Active states:
   prepared, dispatched, unknown (runtime_continuity.py:3673). VERIFIED.
2. Claim. runtime_continuity.py:mark_effect_dispatched:4002 is a PREPARED->DISPATCHED
   compare-and-set; a lost claim is treated as blocked (tool_intent_executor.py:1443-1453). VERIFIED.
3. Exception. runtime_execution_tools.py:3547-3557 -> `EffectOutcomeUnknown`
   (effect_reconciliation.py:61) -> executor classify `unknown` (tool_intent_executor.py:1479-1490). VERIFIED.
4. Lease expiry. Lease is 900 s (runtime_continuity.py:3685). runtime_continuity.py:
   sweep_stale_unresolved_effects:3811 and the inline sweep in reserve: stale PREPARED ->
   `expired_pre_dispatch`, stale DISPATCHED -> `unknown` with `crash_recovery_dispatch_lease_expired`. VERIFIED.
5. Unknown blocks identical redispatch while prepared, dispatched or unknown (step 1). VERIFIED.
6. Resolution. runtime_continuity.py:resolve_unresolved_effect:4160; sources mechanical, provider,
   user (`_VALID_RESOLUTION_SOURCES`:3687); "model" refused (4175). Mechanical resolvers exist only
   for workspace.write_file and machine.write_file (effect_reconciliation.py:219-220). User source via
   /api/runtime/unresolved-effects/resolve (section 2). Fall-through lanes resolve mechanically
   (tool_intent_executor.py:_dispatch_with_claim:1625, 1664). VERIFIED.

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
2. **VERIFIED. Fixed at ee8ced6** for the executor (tool_intent_executor.py:1315, before
   `decide_tool_call` 1318 and A6 reserve 1431), the operator lane (same call, operator branch 895)
   and the fast paths (authorized_tool_execution.py:232). Residual by design: a council seat.
   `windows_write_refusal_before_decision` (runtime_execution_tools.py:7366-7379) returns None when
   `council.containment.binding_for_context` finds a seat, because only `execute_runtime_tool` re-roots
   the seat onto its own workspace (3694-3703). For a seat the decision and A6 reserve/claim happen
   first; the gate runs at dispatch (3255). If the seat's workspace is not flagged, the refusal comes
   back `ok=False` and the A6 row is classified `failed_safe_to_retry` / `dispatch_ack` (1534-1545).
   Only the allowed seat case is tested. UNVERIFIED: other `decide_tool_call` callers
   (core/semantic/admission.py:124, core/live_data_plan.py:863) do not ask the Windows predicate first;
   whether they can carry a gated write intent was not traced.
3. **VERIFIED (upstream design, unchanged from baf115c2: `git diff` empty for core/os_consent_gate.py).**
   Without Windows Hello, `_consent_windows` (os_consent_gate.py:117) falls back to
   `_try_windows_credential_prompt`:176, CredUI with `CREDUIWIN_GENERIC`; the entry is never verified
   (presence only). `VOOL_WALLET_SKIP_CONSENT_GATE=yes` returns True with a warning
   (`_env_bypass_enabled`:59, used 74-80).
4. **VERIFIED.** `spend_authorization.require_spend_authorized` (core/spend_authorization.py:138) has
   no production caller: a repo-wide grep finds only tests. The pay.x402 surface refuses before it
   would matter (tests/test_payment_tools.py::test_a_granted_spend_brake_opens_nothing).
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
   with no Windows confinement check. `vool-browser.*` is dispatched at runtime_execution_tools.py:3158,
   before the Windows gate at 3255. core/vool_browser/engine.py:find_engine_binary:145 accepts any
   executable named by `VOOL_BROWSER_ENGINE`; sessions.py:_own_engine:139 launches it via Playwright with
   `accept_downloads=True` and no Windows check. UNVERIFIED: whether nebula, Playwright or a Chromium
   binary is present in the Windows bundle.
9. **VERIFIED, and the inventory was incomplete.** project_archaeology was fixed in 95f3a51. Raw
   `git` without safe_git remains in core/repoops/identity.py:25, bootstrap.py:50, localgit.py:102,
   local_lifecycle.py:177, and also repoops/archaeology.py (imports identity). None of these is
   imported by production code; only scripts/*_demo.py import them. Other raw git calls exist:
   core/council/api.py:_workspace_sha:78 runs `git rev-parse HEAD` in a council workspace;
   core/agent_runtime/continuity_gate.py:209 in a `repo_dir`; the rest target VOOL's own checkout
   (runtime_execution_tools.py:549, web/api/runtime.py:181, build_provenance.py:27, kernel/repl.py:5136,
   proof_manifest.py:12-14). UNVERIFIED: whether `rev-parse` there can run a repo-configured program.
10. **VERIFIED in source, impact UNVERIFIED.** The installer's process stop (installer/bundle/vool.iss:102)
    also matches any python whose command line contains `vool_api_server.py` or `vool_window.py`,
    not only processes under the install folder. A VOOL running from a source checkout would be
    stopped too.
