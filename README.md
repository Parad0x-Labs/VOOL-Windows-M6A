# VOOL for Windows

<p align="center">
  <img src="docs/assets/vool-flag-banner.png" alt="VOOL flag — your machine, your agent" width="100%" />
</p>

<p align="center"><strong>Your machine. Your models. An agent that gets things done, coming to Windows.</strong></p>

<p align="center">
  <a href="https://github.com/Parad0x-Labs/VOOL-Windows-M6A/actions/workflows/ci.yml"><img src="https://github.com/Parad0x-Labs/VOOL-Windows-M6A/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-eee8d5" alt="MIT license" /></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/base-0.6.0-eee8d5" alt="Based on VOOL 0.6.0" /></a>
  <a href="docs/WINDOWS_QUALIFICATION_STATUS.md"><img src="https://img.shields.io/badge/status-work_in_progress-eee8d5" alt="Status: work in progress" /></a>
</p>

> **Work in progress.** This is the Windows port of VOOL, under active development. There is no Windows download yet, and nothing here is a signed or release-qualified build. If you want VOOL today, use the macOS or Linux release.

**VOOL is a local-first personal agent from Parad0x Labs.** This repository brings it to Windows: a native Windows host, a local model on your own machine, memory, tools, receipts, and actions that wait for your approval.

**[Where it stands](#where-it-stands) · [Try the diagnostic build](#try-the-diagnostic-build) · [Documentation](#documentation) · [Qualification status](docs/WINDOWS_QUALIFICATION_STATUS.md)**

## What the Windows build has today

### A native Windows home

`VOOL.exe` finds its own bundle and starts everything it needs: a supervisor that owns the app's processes, the Python runtime behind the API, and a WebView2 window for the desktop UI. When the window closes, the processes and ports it used are released.

### Local by default

The diagnostic path runs on **Local Only**: a bundled Ollama serves your model on your machine, and remote fallback is off. The API and the model server listen only on loopback (`127.0.0.1:11435` and `127.0.0.1:11434`), and the isolated runtime blocks Ollama's cloud models.

### Actions that ask first

Changes to your workspace and databases go through a Windows broker. Each one needs an exact approval tied to what it will change. A replayed approval, a changed target, or a path that escapes the workspace is refused.

### Results you can check

Receipts record what was asked, what ran, which model answered, and what came back. They record identities and outcomes, never secret values.

## Where it stands

| Area | Status |
| --- | --- |
| Native host, path isolation and lifecycle cleanup | Working, tested live on Windows |
| Local model answers through bundled Ollama (`qwen2.5:7b`) | Working, tested live on Windows |
| Local Only provider mode | Working, tested live on Windows |
| Database plugin and workspace write broker | Working in disposable test setups |
| M6-A source milestone | Frozen source checkpoint |
| M6-B source corrections | In source; installed check on a clean machine still to do |
| Clean installed qualification | Planned, not done yet |
| Installer, updates and code signing | Planned, not released yet |
| Plugins with network access | Not supported in this scope |
| Public Windows download | Planned, not released yet |

Known open gaps: the native `/api/skills` database projection, a test that assumes POSIX permission bits, and an unrelated served-chat plugin-family failure. The full evidence matrix is in [Windows Qualification Status](docs/WINDOWS_QUALIFICATION_STATUS.md).

```text
VOOL.exe (native host)
    |
    +--> supervisor
            |
            +--> Python/API runtime ---> loopback API      127.0.0.1:11435
            |                       \--> bundled Ollama    127.0.0.1:11434
            |
            +--> WebView2 window
```

## Try the diagnostic build

This path is for development and testing. It builds a staged bundle and runs it in throwaway folders, so it never touches your normal VOOL profile.

<details>
<summary><strong>1. Stage a bundle</strong></summary>

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

`-IncludeModel` is left out on purpose. Put model data in a separate, isolated model store only after you have recorded the bundle's identity and the local-model check has passed.

Keep all output, profiles, model stores, logs and databases outside the checkout. Record the bundle manifest and the SHA-256 of the native host, the Python entry points, Ollama and the other lifecycle executables.

Prerequisites and packaging notes: [Windows Build and Test](docs/WINDOWS_BUILD_AND_TEST.md).

</details>

<details>
<summary><strong>2. Check the run folders</strong></summary>

```powershell
python tools\windows_lifecycle_preflight.py `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"
```

The preflight must confirm:

- `launch_ready=true`;
- the native `VOOL.exe` exists in the staged bundle;
- `VOOL_HOME`, the workspace, WebView2, `TEMP`, `TMP` and the Ollama model store are all disposable;
- the API and Ollama stay on `127.0.0.1:11435` and `127.0.0.1:11434`, and both ports are free;
- no writable path overlaps your normal VOOL profile.

</details>

<details>
<summary><strong>3. Launch</strong></summary>

```powershell
python tools\windows_lifecycle_launcher.py check `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"

python tools\windows_lifecycle_launcher.py launch `
  --bundle-root "<run-root>\bundle" `
  --run-root "<run-root>"
```

`check` only reads and never starts VOOL. `launch` refuses to start unless every isolation, Local Only, credential-scrubbing, proxy, path and port check passes.

</details>

## Boundaries

- **Local Only:** remote fallback is off for the diagnostic local-model path.
- **No cloud model traffic:** the isolated runtime requires `OLLAMA_NO_CLOUD=true`.
- **Loopback only:** the API and Ollama bind to the loopback ports above.
- **Throwaway state:** bundle, profile, workspace, WebView2, temp folders and model store all stay under the run folder.
- **Narrow firewall rules:** optional diagnostic rules block outbound traffic for the exact staged executables only, keep loopback working, change no global defaults, and are cleaned up by run ID.
- **Approval before changes:** database and workspace changes need an exact approval through the broker; replays, changed targets and path escapes are refused.
- **No secrets in records:** receipts and docs record identities and outcomes, never secret values.
- **No wallet keys:** this Windows project does not read or expose wallet private keys.

More detail: [Windows Security Boundaries](docs/WINDOWS_SECURITY_BOUNDARIES.md).

## Run the tests

Lifecycle checks:

```powershell
python -m pytest `
  tests\test_windows_lifecycle_launcher.py `
  tests\test_windows_lifecycle_preflight.py `
  tests\test_windows_lifecycle_firewall_helpers.py
```

Broker and native-control checks, when you touch those paths:

```powershell
python -m pytest `
  tests\test_windows_plugin_broker.py `
  tests\test_windows_workspace_broker.py `
  tests\test_windows_host_native_control.py
```

Static checks:

```powershell
python -m ruff check .
git diff --check
```

Report known baseline failures separately. A green focused test does not prove an installed machine, signing, a VM or a release.

## Documentation

| Read | For |
| --- | --- |
| [Windows Port Overview](docs/WINDOWS_PORT_OVERVIEW.md) | How the Windows runtime is shaped and what it covers |
| [Windows Build and Test](docs/WINDOWS_BUILD_AND_TEST.md) | Staging, preflight, launcher and tests |
| [Windows Qualification Status](docs/WINDOWS_QUALIFICATION_STATUS.md) | What is proven and what is still open |
| [Windows Security Boundaries](docs/WINDOWS_SECURITY_BOUNDARIES.md) | Isolation, provider, firewall, model and broker rules |
| [Windows Capability Matrix](docs/WINDOWS_CAPABILITY_MATRIX.md) | Capability detail, including older evidence |
| [Windows One-Click Readiness](docs/WINDOWS_ONE_CLICK_READINESS.md) | Packaging and release-readiness boundary |
| [Security policy](SECURITY.md) · [Contributing](CONTRIBUTING.md) | Reporting issues and the development workflow |

The capability and readiness pages include older evidence. Check each row's date and status before treating it as current.

## License

MIT. See [LICENSE](LICENSE).

Built by **[Parad0x Labs](https://github.com/Parad0x-Labs)**.
