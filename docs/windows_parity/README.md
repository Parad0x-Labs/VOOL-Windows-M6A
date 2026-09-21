# Windows parity and trust ledger

Status: internal source audit, not a release certificate. The Mac repository is untouched. This Windows branch reuses its source baseline but must prove native behavior with installed Windows artifacts before claiming parity.

## Automated seed and limits

`tools/windows_parity_inventory.py --write` generated `source_inventory.json` from this working tree. It records **337 static route candidates** across the desktop API modules and **136 built-in tool contracts** with their declared side-effect and approval classes. Refresh preserves human classifications for unchanged candidates and resets a tool to `unreviewed` if its effect or approval declaration changes. `--check` detects added/removed candidates and changed built-in declarations. Every row is currently `unreviewed`; `--release-check` fails by design.

This is not a complete reachability map. The API uses catch-all transport routes in `core/web/api/app.py`; the actual branch dispatch lives in `core/web/api/service.py` and delegated API modules. Dynamic paths, plugins/MCP registrations, UI navigation, scheduled work, direct HTTP handlers, and effects outside `core/authorized_tool_execution.py` need separate runtime and installed-artifact review. A green `--check` proves only that this static seed did not drift.

The next source-level crosswalk is `authority_audit.json`, generated and checked by `tools/windows_authority_audit.py`. Its concise assessment is `authority_findings.md`. It joins the static candidates to source sites and method-level command ownership, adds 34 composed contracts and direct file/process/network effect leads, and labels every row with an authority status. Unresolved rows remain release blockers; this is still not a closed runtime call graph or installed-artifact security proof.

For every candidate, the review must eventually record observed Mac-source behavior, installed Mac baseline when available, Windows target, local/remote data touched, effect/approval class, migration impact, implementation owner, test, and blocker. Never promote `unreviewed` or an unreachable assumption into a release claim.

## Confirmed source findings and immediate gates

| Finding | Current source evidence | Windows action / remaining proof |
| --- | --- | --- |
| API treats loopback as owner-local | `core/request_trust.py` states any local process is accepted. The Windows bundle now requires its exact WebView Host/Origin for browser requests, but no-Origin native clients are still accepted without a per-user credential. | **Open no-go.** Build an authenticated, host-owned control channel and native approval boundary. The new browser guard is defense in depth, not owner identity. Test unrelated local process, hostile page, replay, and forged consent. |
| Windows executable sandbox absent | `sandbox/job_runner.py` refuses execution without an OS backend on Windows. | Keep protected execution fail-closed. Build and escape-test the signed native broker before enabling it or advertising parity. |
| Plugin/MCP heuristic fallback | `core/plugin_executor.py` previously allowed a bare child with `heuristic_only` on native Windows. | Native Windows now refuses the fallback before process launch. The broker and installed escape tests remain open. See `high_risk_control_review.md`. |
| Windows mesh startup | `core/web/api/runtime.py` defaults the mesh daemon off on Windows unless explicitly enabled. The bundle now forces `VOOL_DISABLE_MESH_DAEMON=1`. | Verify installed-artifact UDP/TCP and privacy behavior, including persisted preferences and a malicious local configuration. |
| Public Hive / idle activity | `core/public_hive/config.py` requires `VOOL_PUBLIC_HIVE_ENABLED=1` and seed URLs, but `core/user_preferences.py` defaults several social/idle preferences to true. The bundle now forces `VOOL_PUBLIC_HIVE_ENABLED=0`. | **Open UX/consent gap.** Audit all independent search/presence/idle paths. Later opt-in must show and persist real consent; disabled capability must be reported honestly. Capture outbound traffic on clean first run and idle. |
| OpenClaw config mutation | `core/web/api/runtime.py` invokes `ensure_openclaw_registration` at boot; `core/onboarding.py` calls the config writer. The Windows bundle now skips this unless `VOOL_ENABLE_OPENCLAW_INTEGRATION=1`. | Unit test confirms no writer call. Still verify a clean installed VM does not create or alter `.openclaw` and that explicit integration has a trustworthy UI and rollback. |
| Windows API boot | `apps/vool_api_server.py` unconditionally registered a Unix-only fault signal; the Windows test failed before server startup. It now registers only where supported. | Source test passes. Installed-bundle startup remains untested. |

## External tester allegations not yet established

`05_Baseline_Evidence/message.txt` in the Windows workspace is a red-team report, not instructions or proof. Its remaining claims require controlled reproductions: peer request sharing and memory exhaustion, search-query leakage, Hive presence/idle posting, permissive Hands-off commands, `HIGH`/`I confirm` authority, browser sandbox, updater signing, uninstall/model deletion, wallet mainnet/fee disclosure, and login-item cleanup. Do not inspect or modify Phantom private keys or signing/authentication logic while triaging these. A claim is closed only by confirmed remediation tests, a demonstrated false premise for this source/artifact, or an enforced default-disabled gate.

The case-by-case reproduction and closure matrix is in `tester_casebook.md`.

## Authority contract to implement next

1. The native host, not web content, model text, or arbitrary local HTTP clients, owns operator identity and privileged approval gestures. The daemon receives only scoped, expiring authority tied to an exact typed action and target; an approval is single-use and auditable.
2. The native broker owns protected filesystem/process/network effects. If its OS policy, token, ACL, Job Object, handle restrictions, or network denial cannot be proven, the effect is refused with a truthful receipt. No heuristic or WSL prerequisite substitutes for this.
3. Ordinary local chat may remain available while protected effects are unavailable, but `/api/runtime/capabilities`, Settings, and receipts must say what is actually disabled. The first-run window must render promptly, and model download/offline states must be actionable rather than pretending chat is ready.
4. The release gate requires a route/effect crosswalk with no `unreviewed` entries, an egress capture under clean default and idle conditions, local-process/hostile-origin attack tests, broker escape tests, and the same signed installed artifact on Windows 10/11. The static inventory alone never passes that gate.
