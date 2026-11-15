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

## 0.7.1-beta — 2026-10-14

Downloads for Apple Silicon Macs, Intel Macs and Linux x86_64 are on the
[v0.7.1-beta release](https://github.com/Parad0x-Labs/vool/releases/tag/v0.7.1-beta).
Updating from 0.7.0: download 0.7.1 and install it over 0.7.0. Your chats and settings
live in your profile, outside the app.

* **Private chat.** Say "private chat", "don't record this" or "off the record", and
  nothing from that chat is saved: no transcript, no memory, no summary, no log. A small
  ghost marks its messages. Say "turn recording back on" to end it. Private chat commands
  work in English only.
* **Secrets stay out.** PINs, card numbers and passwords you type are kept out of memory
  and out of later prompts sent to a model.
* **Forget, the way you say it.** "Forget what I told you about X" removes it from VOOL's
  memory. After you forget something in a chat, VOOL won't take that same fact back in
  that chat; start a new chat to tell it again. The original chat messages stay in VOOL's
  local activity records for now.
* **Answers as files.** Word, spreadsheet, slides, PDF, Markdown, CSV, JSON, YAML, XML,
  HTML, ZIP or tar.gz. VOOL checks that each file opens before offering it.
* **Follow-ups keep the task going.** A message sent while VOOL is working joins the
  running task instead of restarting it.
* **Scanned PDFs** are read with the Mac's built-in text recognition (Macs only).
* **No surprise tool fees.** VOOL refuses a cloud model that runs paid tools at the
  provider without reporting the cost.
* **Custom endpoints and keys** go where the endpoint expects them, and their models are
  listed.
* **Intel Macs.** Local-model certification allows for slower processors. Long local
  answers can still time out there, so use a cloud model on Intel for now.
* **Crypto, off by default.** Arbitrum One joins the supported chains. Settings → Crypto →
  Payment channels has an on/off switch for each way the wallet can pay a website. Websites
  that ask for x402 payment in USDC on Base or Base Sepolia can be paid from your Crypto
  Pilot wallet, one PIN approval within your limits. No live mainnet payment has been made
  with this yet. Solana pay-kit is not included in the downloads.

## 0.7.0-beta — 2026-10-13

Downloads for Apple Silicon Macs, Intel Macs and Linux x86_64 are on the
[v0.7.0-beta release](https://github.com/Parad0x-Labs/vool/releases/tag/v0.7.0-beta).

* **Runs on more computers.** Separate downloads for Apple Silicon Macs, Intel Macs and
  Linux x86_64. The Mac app carries its own local-model runner.
* **Installed by download.** Install 0.7.0 from the release download.
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
* **Crypto stays off by default.** Paying websites with x402 started on test networks.
  The pay-per-call option in the model marketplace in Settings can pay from the Solana
  Mainnet wallet, one approval per call with the 0.1% fee shown; it has not yet been tested
  on a real chain.

The Mac apps are not notarized yet: see
[first open on a Mac](../getting-started/install.md#first-open-on-a-mac).
