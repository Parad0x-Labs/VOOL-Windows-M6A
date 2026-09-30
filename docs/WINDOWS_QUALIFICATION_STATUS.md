# Windows Qualification Status

> **Status:** internal evidence snapshot. This page separates source acceptance, live diagnostic evidence, and release qualification. It must not be read as a signed-release claim.

## **Evidence matrix**

| Area | Current status | Boundary |
| --- | --- | --- |
| Native bundle/path isolation | **Accepted / source-tested** | Native host resolves the running bundle; explicit `VOOL_HOME` isolates WebView2. |
| Diagnostic lifecycle | **Accepted / live Windows-tested** | Staged bundle launch/close cycles proved owned process cleanup and release of ports 11434/11435. |
| Local-model authorship | **Accepted / live Windows-tested** | An ordinary isolated turn was served by `ollama-local` with `qwen2.5:7b`; evidence required the actual Ollama call and zero web/cloud calls. |
| Provider truth | **Accepted / source-tested and live-checked** | Immutable boot decision is retained separately from the current effective local-provider decision. |
| Model validation/finalization | **Accepted / regression-tested** | Failed attempts remain visible; only an accepted attempt can commit the final model answer. |
| Sentence-range semantics | **Accepted / regression-tested** | Ranges such as `2-3` and `2–3` are treated as minimum/maximum bounds; exact counts remain exact. |
| Residency-aware admission | **Accepted / regression-tested** | Authoritative current residency or a fresh, identity-bound certificate can avoid double-charging an already resident model. |
| Database plugin parity | **Accepted functionally / disposable Windows-tested** | Discovery, read-only operations, denial, approval, broker receipts, and persistence were exercised in disposable state. |
| Workspace mutation broker | **Accepted / native and served proof** | `workspace_write_v1` proved exact approval, hash binding, replay denial, confinement, and cleanup in the accepted harness/served path. |
| M6-A source/package milestone | **Frozen / published source checkpoint** | The M6-A source checkpoint is `63a411f833da188292d40c083a716b9fe38eefb0`. |
| M6-B source corrections | **Accepted in source; installed qualification pending** | Current source includes the readiness/model-selection corrections, but clean-baseline provenance must be resolved before an installed M6-B claim. |
| Clean Windows qualification baseline | **Pending** | A VM is not called clean unless its absence inventory was captured before VOOL was ever installed or launched. |
| Public Windows release | **Not qualified** | Signing, packaging, clean-machine evidence, and release acceptance remain separate gates. |

## **What is proven**

The Windows-port source has a real isolated diagnostic path rather than a documentation-only claim. The accepted evidence covers native path resolution, explicit lifecycle launch, loopback API/Ollama routing, Local Only provider behavior, local model authorship, attempt-safe response finalization, plugin/broker boundaries, and workspace mutation controls.

The evidence standard is intentionally strict:

- **Source identity is recorded.** A live result must bind to the source/build manifest and executable hashes used for that run.
- **Actual routes are observed.** Model authorship requires a real local Ollama `/api/chat`, not a UI success indicator or a deterministic fast path.
- **Isolation is explicit.** `VOOL_HOME`, workspace, WebView2, temporary directories, and model stores are disposable and checked before launch.
- **Failures remain evidence.** Retry failures, denials, and blocked environment gates are not erased to make a summary look green.

## **Open qualification boundaries**

- **Installed M6-B qualification is not complete.** The prior qualification clone contained pre-existing VOOL state, so its clean-baseline provenance was invalid until a fresh, audited baseline is established.
- **No public signed-release claim.** Internal source and diagnostic bundles do not establish release signing, installer support, upgrade safety, or clean-machine support.
- **The Windows broker is not release-qualified as a published artifact.** The native security path has focused build/harness evidence, but packaging/signing and release integration remain separate work.
- **Network-enabled plugin execution remains intentionally unsupported.** Local database and workspace paths are not evidence that a plugin may receive unrestricted network access.
- **Known open gaps remain visible:** invalid native `/api/skills` database projection, the Windows POSIX permission-bit test assumption, and the unrelated served-chat plugin-family expansion failure.
- **Phantom is out of scope.** No wallet private keys are exposed, and Phantom surfaces are not part of this Windows qualification claim.

## **Evidence handling**

Qualification reports should record request/turn IDs, provider/lane, exact model identity, certification and memory decisions, actual endpoint calls, receipts, and cleanup. Keep disposable logs, profiles, model stores, VM disks, installers, credentials, and caches outside the source tree. Publish only sanitized summaries and stable source/build identities.

## **Next gate**

Before any release or broader parity claim, establish a genuinely clean Windows baseline, capture the absence inventory, create the canonical clean snapshot, and repeat the installed qualification using the existing installer without reusing contaminated state.
