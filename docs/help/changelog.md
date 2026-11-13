---
description: Notable changes to VOOL.
---

# Changelog

Entries are added as builds go out to testers. Newest first.

## 0.7.1-beta.2 for Windows — 2026-11-12

The first Windows download, `VOOL-Setup.exe`, for Windows 10 22H2 and Windows 11, 64-bit.
Windows needs 16 GB of RAM. With less, VOOL refuses to load its local model rather than freeze the PC, and chat answers that the model was blocked by the memory-safety gate. VOOL checks free memory when it loads the model (about 7 GB), so on a 16 GB PC with many apps open it may still refuse until you close some.

* **Installs for your account only**, with no administrator prompt. It is not code-signed yet:
  check it against `SHA256SUMS`, then at SmartScreen use **More info → Run anyway**.
* **You choose the folders VOOL may change**, in **Settings → Folders VOOL may change (Windows)**.
* **Offline by default.** VOOL works offline by default. Ollama's cloud models are refused. A
  cloud provider is used only after you add your own key in Settings. Saving an OpenRouter key
  turns cloud on; for other providers, also choose an Auto fallback model or pin a paid
  model in Settings. To turn cloud off again, type `cloud off` (it also clears a pinned paid
  model) or remove your key in Settings.
* **Online add-on scan.** The add-on store's **Scan with Eyebrow** step (**Download and scan** for an external add-on) is an online service: it downloads the add-on from its source (for an external add-on) and calls api.eyebrow.cc, and runs only after you add an Eyebrow key in Settings → API Keys and agree to the scan.
* **Windows limits.** VOOL runs Python scripts, pytest, unittest and compileall in its Windows
  sandbox. It refuses git, pip, npm, node, ruff and any other program, and does not start plugins
  or MCP servers or read PDF or 7z files. Use WSL2/Linux for those. The full list is on the
  [Windows v0.7.1-beta.2 release](https://github.com/Parad0x-Labs/VOOL-Windows-M6A/releases/tag/v0.7.1-beta.2).

## 0.7.0-beta — 2026-10-13

Downloads for Apple Silicon Macs, Intel Macs and Linux x86_64 are on the
[v0.7.0-beta release](https://github.com/Parad0x-Labs/vool/releases/tag/v0.7.0-beta).

* **Runs on more computers.** Separate downloads for Apple Silicon Macs, Intel Macs and
  Linux x86_64. The Mac app carries its own local-model runner.
* **Signed updates, starting next release.** This version carries one-press signed updates
  with rollback, and update checks stay off until you allow them. The first update offered
  through it will be 0.7.1; install 0.7.0 itself from the download.
* **Better memory.** VOOL recalls what you told it, forgets on request, and says so when
  there was nothing to forget.
* **Your rules.** Set rules for everything, one chat or one project. Ask "what are my
  rules", and remove one by its number.
* **Personal assistant fixes.** Reminders and calendar use your time zone, past reminder
  times are refused, all-day events land on the right day, and email connections check
  the server's certificate.
* **Chain data in chat.** Add your own Helius, Alchemy or Solscan key for read-only chain
  data in chat. With no key, no network call is made.
* **X.** Draft posts in chat, with a guided setup that checks your keys read-only.
* **Report a wrong answer.** Report and 👎 on every answer; you see the report text, with
  private details removed, before it is sent.
* **Skill Codex** is on the Add-ons page. Browsing is not open yet in this release.
* **More languages.** 16 languages besides English.
* **Crypto stays off by default.** Paying websites with x402 works on test networks only.
  The pay-per-call option in the model marketplace in Settings can pay from the Solana
  Mainnet wallet, one approval per call with the 0.1% fee shown; it has not yet been tested
  on a real chain.

The Mac apps are not notarized yet: see
[first open on a Mac](../getting-started/install.md#first-open-on-a-mac).
