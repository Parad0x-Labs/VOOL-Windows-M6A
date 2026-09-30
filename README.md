# VOOL for Windows

> **Windows-port repository.** A local-first personal agent runtime with a native Windows host, local model serving, memory, tools, receipts, and security-gated effects.

> **Status: internal development and qualification.** This repository does not claim a public signed Windows release.

<p align="center">
  <img src="./docs/assets/github-header-vool-local.png" alt="VOOL for Windows" width="100%" />
</p>

## **What this repository is**

This checkout is dedicated to the Windows runtime and its qualification tooling. It contains the source, native host, staged-bundle pipeline, local Ollama path, WebView2 integration, lifecycle controls, and Windows broker coverage needed to develop and evaluate VOOL on Windows.

VOOL runs locally by default. The Windows diagnostic path is designed to keep the API and local model server on loopback, isolate writable runtime state, and make provider, model, approval, and process evidence inspectable.

## **Current state**

| Area | Status |
| --- | --- |
| Branch | `windows-port` |
| Runtime | Native Windows host plus bundled Python/API and WebView2-facing process |
| Local API | `127.0.0.1:11435` |
| Local model server | Bundled Ollama on `127.0.0.1:11434` |
| Diagnostic provider mode | Local Only; remote fallback disabled |
| Qualification model | `qwen2.5:7b` when provisioned in an isolated model store |
| M6-A source milestone | Accepted and published |
| M6-B source corrections | Accepted in source; installed qualification still requires a clean-baseline proof |
| Public release | Not release-qualified or publicly signed |

## **Runtime architecture**

```text
native VOOL.exe
    |
    +--> bundle supervisor
            |
            +--> embedded Python/API runtime
            |       |
            |       +--> loopback API :11435
            |       +--> bundled Ollama :11434
            |
            +--> WebView2-facing window host
```

The supported diagnostic process tree includes:

- **Native host:** locates its own bundle and starts the Windows runtime.
- **Supervisor:** owns the application children and records lifecycle evidence.
- **Python/API runtime:** serves the product path, receipts, skills, provider state, and local endpoints.
- **WebView2-facing process:** presents the desktop UI with an isolated user-data path when `VOOL_HOME` is explicit.
- **Bundled Ollama:** serves the exact local model from the isolated model store.

## **Build and stage**

There is no public Windows installer claim on this page. For development and qualification, use the existing stage-only bundle pipeline:

```powershell
$stage = "C:\vool-build\bundle-<run-id>"

powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\installer\bundle\build_bundle.ps1 `
  -Stage $stage `
  -OllamaExe "<path-to-ollama.exe>" `
  -WebView2Installer "<path-to-WebView2-installer.exe>" `
  -WebView2Sha256 "<64-hex-sha256>" `
  -BuildMode Internal
```

`-IncludeModel` is intentionally not part of the normal staging command. Provision required model data into a separate isolated model store only after the bundle identity has been recorded and the local-model gate has passed.

Keep all generated output, profiles, model stores, logs, and databases outside the checkout. Record the bundle manifest and SHA-256 values for the native host, Python entrypoints, Ollama, and other lifecycle executables.

For complete prerequisites, provenance checks, and packaging notes, read [Windows Build and Test](docs/WINDOWS_BUILD_AND_TEST.md).

## **Safe diagnostic path**

Resolve the disposable paths before launching anything:

```powershell
python tools\windows_lifecycle_preflight.py `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"
```

The preflight must confirm:

- **`launch_ready=true`;**
- native `VOOL.exe` exists at the staged bundle path;
- `VOOL_HOME`, workspace, WebView2, `TEMP`, `TMP`, and Ollama model store are disposable;
- API and Ollama remain on `127.0.0.1:11435` and `127.0.0.1:11434`;
- both ports are free;
- no writable path overlaps `C:\NULLA` or the normal VOOL profile.

Launch only through the explicit-action diagnostic launcher:

```powershell
python tools\windows_lifecycle_launcher.py check `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"

python tools\windows_lifecycle_launcher.py launch `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"
```

`check` is read-only and never starts VOOL. `launch` fails closed unless all isolation, Local Only, credential-scrubbing, proxy, path, and port guards pass.

## **Security boundaries**

- **Local Only:** remote fallback is disabled for the diagnostic local-model path.
- **No cloud model traffic:** the isolated runtime requires `OLLAMA_NO_CLOUD=true`.
- **Loopback only:** the API and Ollama endpoints are bound to the documented loopback ports.
- **Disposable state:** bundle, profile, workspace, WebView2, temporary directories, and model store remain under the run root.
- **Exact firewall scope:** optional diagnostic rules are outbound blocks for exact staged executable paths, preserve loopback, change no global defaults, and clean up by run ID.
- **Approval-gated effects:** database and workspace mutations require exact policy/approval/broker handling; replay, changed targets, and path escapes fail closed.
- **No secret publication:** receipts and documentation record identities and outcomes, never ambient secret values.
- **No wallet-key access:** Phantom private keys are not exposed by this Windows project.

## **Validation**

Run the focused lifecycle checks:

```powershell
python -m pytest `
  tests\test_windows_lifecycle_launcher.py `
  tests\test_windows_lifecycle_preflight.py `
  tests\test_windows_lifecycle_firewall_helpers.py
```

Run broker/native-control regressions when those paths are in scope:

```powershell
python -m pytest `
  tests\test_windows_plugin_broker.py `
  tests\test_windows_workspace_broker.py `
  tests\test_windows_host_native_control.py
```

Run static checks:

```powershell
python -m ruff check .
git diff --check
```

Always report known baseline failures separately. A focused green test does not establish installed-machine, signing, VM, or release qualification.

## **Qualification boundary**

Accepted Windows evidence currently covers native path isolation, lifecycle cleanup, Local Only provider truth, local Ollama authorship, attempt-safe model finalization, residency-aware memory admission, confined plugin/database operations, and `workspace_write_v1`.

Remaining release boundaries include:

- **Clean installed qualification:** the baseline must be proven clean before VOOL is installed or launched.
- **Installer and upgrade qualification:** source/build evidence is not installer or update evidence.
- **Broker publication:** the Windows broker remains an internal security artifact until packaging and signing are separately qualified.
- **Network-enabled plugins:** intentionally unsupported in this diagnostic scope.
- **Known open gaps:** invalid native `/api/skills` database projection, the Windows POSIX permission-bit test assumption, and the unrelated served-chat plugin-family expansion failure.

See [Windows Qualification Status](docs/WINDOWS_QUALIFICATION_STATUS.md) for the evidence matrix and current blockers.

## **Documentation**

| Document | Purpose |
| --- | --- |
| [Windows Port Overview](docs/WINDOWS_PORT_OVERVIEW.md) | Runtime shape and Windows scope |
| [Windows Build and Test](docs/WINDOWS_BUILD_AND_TEST.md) | Stage-only build, preflight, launcher, and tests |
| [Windows Qualification Status](docs/WINDOWS_QUALIFICATION_STATUS.md) | Accepted evidence and remaining gates |
| [Windows Security Boundaries](docs/WINDOWS_SECURITY_BOUNDARIES.md) | Isolation, provider, firewall, model, and broker rules |
| [Windows Capability Matrix](docs/WINDOWS_CAPABILITY_MATRIX.md) | Historical/source capability detail |
| [Windows One-Click Readiness](docs/WINDOWS_ONE_CLICK_READINESS.md) | Packaging and release-readiness boundary |
| [Security policy](SECURITY.md) | Reporting and security expectations |
| [Contributing](CONTRIBUTING.md) | Development workflow |

> **Reading note:** The capability and readiness documents contain historical evidence. Always check the evidence date and status before treating a row as current or release-ready.

## **License**

MIT — see [LICENSE](LICENSE).
