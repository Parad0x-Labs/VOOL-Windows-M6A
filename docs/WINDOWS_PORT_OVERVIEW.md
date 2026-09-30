# VOOL Windows Port

> **Status: internal development and qualification.** This page describes the Windows-port source and its diagnostic qualification boundaries. It is not a claim that VOOL has a public signed Windows release.

## **At a glance**

| Item | Current value |
| --- | --- |
| Development branch | `windows-port` |
| Current source checkpoint | `88a7cd9f26c8863341a74a0530317b83ac4bbd33` |
| Public M6-A source checkpoint | `63a411f833da188292d40c083a716b9fe38eefb0` |
| Diagnostic provider mode | Local Only |
| API endpoint | `127.0.0.1:11435` |
| Local Ollama endpoint | `127.0.0.1:11434` |
| Qualification model | `qwen2.5:7b`, when the isolated run is provisioned for it |
| Release status | Not release-qualified or publicly signed |

The current checkpoint contains the accepted Windows runtime corrections after the M6-A source milestone. The working checkout may also contain uncommitted operator-only `.codex` material; that material is not a package input and must not be committed or published.

## **What is implemented**

- **Native runtime isolation:** the native host resolves its bundle from the running executable, and an explicit `VOOL_HOME` controls the isolated WebView2 profile.
- **Local-only routing:** diagnostic runs keep the local provider lane authoritative, disable remote fallback, and preserve the original boot decision separately from current provider truth.
- **Lifecycle control:** a staged bundle can be checked and launched through the fail-closed Windows lifecycle tooling, with explicit launch intent required.
- **Local model path:** bundled Ollama can register, certify, and serve an exact local model from an isolated model store.
- **Validation and finalization:** model attempts retain immutable evidence, retries receive fresh validation scope, and only an accepted attempt can commit an answer.
- **Memory admission:** exact current-model residency may be used as evidence without weakening the model estimate or the 1.5 GiB safety floor.
- **Confined plugin effects:** the Windows broker and workspace mutation path enforce exact approval, confinement, process ownership, and no-network boundaries where supported.
- **Diagnostic evidence:** lifecycle, provider, model, broker, and cleanup checks are designed to record identities and outcomes without copying normal-profile state.

## **Runtime shape**

| Layer | Responsibility | Boundary |
| --- | --- | --- |
| Native `VOOL.exe` | Owns the Windows process entry and locates the bundle | Runs from the staged bundle; does not assume a fixed install root |
| Bundle supervisor | Starts and owns the application runtime | Disposable run root and explicit child ownership |
| Embedded Python/API | Product logic, routing, receipts, skills, and lifecycle endpoints | Loopback API; isolated `VOOL_HOME` and workspace |
| WebView2 window host | Presents the desktop UI | Profile lives below the isolated runtime home when explicitly configured |
| Bundled Ollama | Local model serving | `127.0.0.1:11434`, isolated model store, Local Only policy |
| Firewall helper | Optional diagnostic containment | Exact staged executable paths, outbound block only, loopback preserved |

The normal diagnostic flow is:

```text
VOOL.exe
  -> bundle supervisor
      -> embedded Python/API runtime
          -> local Ollama (loopback only)
      -> WebView2-facing window host
```

The native host, supervisor, API runtime, window host, Ollama, and their owned children are treated as one run-scoped process tree. A normal close must release the two diagnostic ports and leave no unexpected relaunch.

## **Where to start**

- [Windows build and test](WINDOWS_BUILD_AND_TEST.md) — stage a bundle, inspect provenance, and run focused checks.
- [Windows qualification status](WINDOWS_QUALIFICATION_STATUS.md) — separate accepted evidence from remaining qualification blockers.
- [Windows security boundaries](WINDOWS_SECURITY_BOUNDARIES.md) — isolation, provider, firewall, and broker rules.
- [Windows capability matrix](WINDOWS_CAPABILITY_MATRIX.md) — historical/source capability detail; read its evidence dates carefully.
- [Windows one-click readiness](WINDOWS_ONE_CLICK_READINESS.md) — release-readiness boundary and open packaging work.

## **Reference-source note**

The separate public VOOL reference repository inspected during Windows documentation work identifies its current public line as `0.6.0-beta`; no exact `v0.7` tag or branch was available in the inspected refs. Do not infer a shipped v0.7 Windows parity target from an unlabelled `main` branch. Any v0.7 comparison needs an exact tag, commit, or release specification.

## **Scope and non-goals**

This port documentation does not authorize installer deployment, VM qualification, model downloads, Phantom interaction, remote provider use, or production-profile migration. Those activities require their own explicit, isolated qualification procedure and evidence.
