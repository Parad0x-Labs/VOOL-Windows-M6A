# Windows Build and Test

> **Purpose:** reproduce the Windows-port source checks and create a disposable staged bundle without installing VOOL or touching normal user state.

## **Safety rules**

- **Use the authoritative checkout only.** Confirm the branch, commit, and tracked status before building.
- **Keep generated output outside the repository.** Use a disposable run directory for stages, profiles, logs, model stores, and test databases.
- **Do not reuse normal state.** A diagnostic run must not resolve to `C:\NULLA`, the default `%LOCALAPPDATA%\VOOL` profile, or the operator's global Ollama model store.
- **Do not launch by accident.** `windows_lifecycle_launcher.py check` is read-only. Execution requires the explicit `launch` action and must pass every isolation guard first.
- **Treat manifests as evidence.** Record the source identity, dirty state, build inputs, and executable hashes for every staged bundle.

## **Prerequisites**

Use the versions and hashes declared by the checkout's build inputs. The native broker qualification uses the pinned portable SDK when it is present:

```text
build-inputs/dotnet-sdk-10.0.401/dotnet.exe
```

Use that executable explicitly rather than installing a global SDK or changing the machine-wide `PATH`. The Python runtime, test runner, Ollama executable, and WebView2 installer must likewise be supplied from the approved build inputs for the run.

## **Stage a Windows bundle**

The existing bundle pipeline is the supported stage-only path. It produces the native host, embedded Python/runtime payload, application source, supervisor, WebView2 input, and bundled Ollama components expected by the native lifecycle.

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

`-IncludeModel` is intentionally not part of the normal staging command. Provision a required model into a separate isolated model store only after the bundle identity has been recorded and the local-model gate has passed.

For the package/installer-oriented path, the existing script is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\installer\build_windows_package.ps1 `
  -OutputDir "C:\vool-build\dist-<run-id>" `
  -PackageVersion "<version>"
```

Staging is not installation. An installer build or package archive does not prove that a clean Windows installation, first launch, update, or uninstall is qualified.

## **Verify bundle provenance**

Record the source and manifest identity before any launch:

```powershell
git branch --show-current
git rev-parse HEAD
git status --short --branch
Get-Content <stage>\bundle_manifest.json
Get-FileHash <stage>\bundle_manifest.json -Algorithm SHA256
Get-FileHash <stage>\native\VOOL.exe -Algorithm SHA256
Get-FileHash <stage>\ollama\ollama.exe -Algorithm SHA256
Get-FileHash <stage>\python\python.exe -Algorithm SHA256
Get-FileHash <stage>\python\pythonw.exe -Algorithm SHA256
```

The manifest must identify the source checkpoint used to build the stage. A dirty source tree is allowed only for an explicitly internal diagnostic build and must be visible in the manifest; release mode has stricter cleanliness and authority checks.

## **Run the read-only preflight**

Resolve all disposable paths without creating directories or starting VOOL:

```powershell
python tools\windows_lifecycle_preflight.py `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"
```

The preflight must show:

- **`launch_ready=true`;**
- native `VOOL.exe` at `<bundle-root>\native\VOOL.exe`;
- `VOOL_HOME`, workspace, WebView2, `TEMP`, `TMP`, and the model store under the disposable run root;
- API `127.0.0.1:11435` and Ollama `127.0.0.1:11434`;
- neither port already occupied;
- no writable path overlapping `C:\NULLA` or the normal VOOL profile;
- `directories_created=false`.

## **Use the canonical lifecycle launcher**

The only supported diagnostic entry point is explicit-action CLI usage:

```powershell
python tools\windows_lifecycle_launcher.py check `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"

python tools\windows_lifecycle_launcher.py launch `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"
```

`check` must be read-only and must not call `Popen` or create runtime directories. `launch` re-runs the fail-closed checks, forces Local Only, disables web and remote fallback, sets `OLLAMA_NO_CLOUD=true`, scrubs credentials/proxies, and starts the native host only after all guards pass.

## **Focused validation**

Run the focused Windows lifecycle and firewall checks first:

```powershell
python -m pytest `
  tests\test_windows_lifecycle_launcher.py `
  tests\test_windows_lifecycle_preflight.py `
  tests\test_windows_lifecycle_firewall_helpers.py
```

Then run the Windows broker and native-control coverage relevant to the change:

```powershell
python -m pytest `
  tests\test_windows_plugin_broker.py `
  tests\test_windows_workspace_broker.py `
  tests\test_windows_host_native_control.py
```

The repository's broader plugin, provider, local-model, response/finalization, API, and native test groups should be run cumulatively for changes that touch those paths. Known baseline failures must be reported as existing failures rather than silently relabelled as green.

Static and whitespace checks:

```powershell
python -m ruff check .
git diff --check
```

Native broker qualification uses the pinned SDK explicitly, for example:

```powershell
& .\build-inputs\dotnet-sdk-10.0.401\dotnet.exe --version
```

Use process-local environment variables such as `DOTNET_ROOT` only for the build/test command when the SDK layout requires them. Do not install a global SDK as part of a repository check.

## **Cleanup and evidence**

Use only the run-scoped firewall cleanup helper for rules created by that run. Verify that ports are released, owned processes are gone, and no unexpected relaunch occurs. Preserve the manifest, hashes, logs, receipts, and failure evidence needed to reproduce the result; do not copy runtime profiles, model blobs, credentials, caches, or VM disks into the repository.
