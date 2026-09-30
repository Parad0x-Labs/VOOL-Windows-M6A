# Windows Security Boundaries

> **Principle:** Windows qualification is allowed to prove capability only inside explicit, disposable boundaries. A passing feature test does not authorize broader filesystem, network, credential, or provider access.

## **Provider and network policy**

- **Local Only is authoritative** for the diagnostic local-model path.
- **Remote fallback is disabled.** The current effective provider state must reflect the local backend after setup while retaining the immutable boot decision as historical evidence.
- **Web/tool access is disabled** for the local-model proof unless a separate test explicitly authorizes and contains it.
- **`OLLAMA_NO_CLOUD=true`** is required for the isolated local runtime.
- **Loopback is preserved.** The API uses `127.0.0.1:11435` and Ollama uses `127.0.0.1:11434`; preserving loopback is not permission to make external connections.
- **Credential and proxy variables are scrubbed** by the canonical diagnostic launcher. Receipts and documentation must not contain secret values.

## **Executable containment**

When firewall containment is used, it is run-scoped and exact:

| Rule property | Required behavior |
| --- | --- |
| Direction | Outbound only |
| Action | Block |
| Scope | Exact staged VOOL-owned executable paths |
| Rule group | Unique to the lifecycle run ID |
| Loopback | Preserved |
| Global defaults | Never changed |
| Cleanup | Remove only rules created by that run |

The helper must not block unrelated applications or use a broad image-name match. The normal non-administrator session verifies the installed rules and their program paths before a diagnostic launch.

## **Path and profile isolation**

Before launch, the preflight resolves and records:

- **Bundle root** and native `VOOL.exe`;
- **`VOOL_HOME`**;
- **Workspace**;
- **WebView2 user-data/profile**;
- **`TEMP` and `TMP`**;
- **Isolated Ollama model store**.

Every writable diagnostic path must be under the disposable run root. The preflight aborts if a path overlaps `C:\NULLA`, the operator's normal VOOL profile, or an inherited normal-profile location. Calculating paths must not create directories or migrate an existing WebView2 profile.

## **Process and lifecycle boundaries**

The native host, supervisor, API runtime, WebView2-facing window host, bundled Ollama, and owned helper processes form one run-scoped process tree. The launcher checks the tree before starting it; normal close must terminate owned descendants and release both loopback ports. A bounded post-close observation is required to detect unexpected relaunch.

The diagnostic launcher is explicit-action and fail-closed:

```text
check  -> validate only; no Popen; no runtime-directory creation
launch -> validate all guards, then start exactly one native entry point
```

Omitting the action or supplying an invalid action must never execute VOOL.

## **Local model boundary**

The local model must be selected from the staged runtime's actual resolved inventory, not from a historical default. A valid local-model proof binds the model tag and digest to:

- the current Ollama endpoint;
- the current Ollama process identity;
- the current diagnostic/runtime identity;
- fresh residency or successful model-call evidence;
- certification and memory-admission decisions.

If authoritative residency is unavailable and no fresh exact certificate exists, the governor retains the conservative full model estimate plus the 1.5 GiB safety floor. Readiness, registration, or historical certification alone is not residency proof.

## **Plugin and broker boundary**

Database and workspace operations are not equivalent authorities:

| Operation class | Boundary |
| --- | --- |
| Discovery/catalog and read-only inspection | Read-only product path and receipt |
| Policy-gated operation | Product policy must allow the requested scope |
| Mutation | Exact effect, target, and prior-state binding required |
| Protected execution | Explicit approval plus the Windows broker/confinement boundary |

For `workspace.write_file`, the approved operation binds the exact target, approved prior state/hash, new content/final hash, owner-bound approval identity, and one-time consumption. Replay, changed content, changed target, outside-workspace paths, UNC/device paths, junction/reparse escapes, and sibling canaries must fail closed.

The native broker uses the documented Windows confinement controls, including AppContainer identity and Job Object ownership where available. A broker receipt must identify the confinement type, filesystem grant classes, network policy, process result, and post-write verification without implying capabilities that were not granted.

## **Installer and user-state boundary**

The Windows installer is per-user and must not be treated as a clean-baseline creator. Uninstall behavior preserves user data such as models, memory, receipts, and wallet state; it does not erase that state to simulate a fresh machine. A manually removed installation does not restore clean-snapshot status.

Installer, VM, Phantom, release signing, and normal-profile migration are separate qualification surfaces. None is implied by a source-level or disposable-bundle test.

## **Explicit non-goals**

- **No blanket production firewall change.** Diagnostic containment must not alter global firewall defaults.
- **No cloud fallback under Local Only.** A failed local gate is a failure, not permission to switch providers.
- **No model-store sharing.** An isolated run copies required content into its own store; it does not reference the global store through links or hidden paths.
- **No secret collection.** Environment audits may record variable names and presence, but never secret values.
- **No Phantom-key access.** Wallet private keys are not exposed to the application or qualification process.
- **No release claim from configuration inspection alone.** Process ownership, network denial, AppContainer confinement, cleanup, and receipts must be exercised and evidenced.
