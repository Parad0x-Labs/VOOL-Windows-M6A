# VOOL Docs

Shortest accurate path through the repo. VOOL is a local-first AI agent runtime (beta) —
it runs on your machine, uses memory and tools, and can optionally reach further.

## Start here

- [../README.md](../README.md) — product overview, install, first run
- [INSTALL.md](INSTALL.md) — install options, manual launch, first local URLs
- [STATUS.md](STATUS.md) — what is real, what is weak, what is still beta
- [UPGRADE_NULLA_TO_VOOL.md](UPGRADE_NULLA_TO_VOOL.md) — upgrading from a NULLA install

## **Windows port**

- [WINDOWS_PORT_OVERVIEW.md](WINDOWS_PORT_OVERVIEW.md) — runtime shape, current source boundary, and starting points.
- [WINDOWS_BUILD_AND_TEST.md](WINDOWS_BUILD_AND_TEST.md) — supported stage-only build, isolated preflight, launcher, and validation commands.
- [WINDOWS_QUALIFICATION_STATUS.md](WINDOWS_QUALIFICATION_STATUS.md) — accepted evidence, open gates, and release limitations.
- [WINDOWS_SECURITY_BOUNDARIES.md](WINDOWS_SECURITY_BOUNDARIES.md) — provider, path, process, model, firewall, and broker boundaries.
- [WINDOWS_CAPABILITY_MATRIX.md](WINDOWS_CAPABILITY_MATRIX.md) — historical/source capability matrix; read status dates carefully.
- [WINDOWS_ONE_CLICK_READINESS.md](WINDOWS_ONE_CLICK_READINESS.md) — release-readiness boundary and remaining packaging work.

> **Reading note:** The Windows pages above are a current, sanitized guide to the port. The older capability and readiness matrices retain historical evidence and should not be used to infer that an unqualified row is release-ready.

## Understanding the system

- [RUNTIME_ARCHITECTURE_CONTRACT.md](RUNTIME_ARCHITECTURE_CONTRACT.md) — request flow, model selection, tools, permissions, money boundaries, storage, receipts
- [SYSTEM_SPINE.md](SYSTEM_SPINE.md) — the one-system architecture view
- [CONTROL_PLANE.md](CONTROL_PLANE.md) — canonical startup and runtime map
- [REPO_MAP.md](REPO_MAP.md) — what lives where in this repository
- [VOOL_IDENTITY_COMPATIBILITY_MAP.md](VOOL_IDENTITY_COMPATIBILITY_MAP.md) — what the NULLA→VOOL rename changed and what stayed frozen

## Operating it

- [CONFIGURATION.md](CONFIGURATION.md) — settings and environment variables
- [PROVIDERS.md](PROVIDERS.md) — BYOK, UsePod prepaid, wallet/x402 lanes
- [WALLET_WEB0_GUIDE.md](WALLET_WEB0_GUIDE.md) — optional crypto: custody, spending controls, fees
- [ERROR_BOOK.md](ERROR_BOOK.md) — every fault code, generated from the runtime catalog
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md) — common problems and fixes
- [SAFE_BUG_REPORTER.md](SAFE_BUG_REPORTER.md) — the redacted, preview-gated bug reporter

## Deeper references

- [AUTHORITY_MAP.md](AUTHORITY_MAP.md), [TOOL_PERMISSION_AUTHORITY.md](TOOL_PERMISSION_AUTHORITY.md), [SANDBOX_PROCESS_CONFINEMENT.md](SANDBOX_PROCESS_CONFINEMENT.md), [TRUST.md](TRUST.md), [SPEND_CAPS.md](SPEND_CAPS.md)
- Feature contracts: [BRAIN_HIVE_*.md](BRAIN_HIVE_ARCHITECTURE.md), [MEET_AND_GREET_*.md](MEET_AND_GREET_API_CONTRACT.md), [SEARCH_API_BYOK.md](SEARCH_API_BYOK.md), [x402-proof.md](x402-proof.md)
- Platform honesty: [APPLE_CAPABILITY_MATRIX.md](APPLE_CAPABILITY_MATRIX.md), [WINDOWS_CAPABILITY_MATRIX.md](WINDOWS_CAPABILITY_MATRIX.md)
- Legal: [LICENSING_MATRIX.md](LICENSING_MATRIX.md), [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)
