---
description: How VOOL protects your files and API keys — workspace boundaries, credential storage, and what a plugin can reach.
---

# Security model

## Boundaries

* **The workspace** bounds file access. Outside it, the runtime cannot read or write.
* **The credential store** holds keys. Keys are not written into config files or logs.
* **Tool confirmation** bounds changes. In Manual mode every change asks first; in Auto mode
  everyday edits and commands can run automatically, while deleting, overwriting, installing,
  spending and changing settings still ask. See [Tools](../concepts/tools.md).

## Keys

Your provider key stays on your machine and is used from there to reach your provider. Where it
is kept depends on the system:

* **Windows:** Windows Credential Manager, as a generic credential named `VOOL/credentials/` plus the key's name.
  Windows seals it to your sign-in; any program running as you can read it. If Credential Manager
  is not available, VOOL keeps the key in its encrypted vault file instead and says so. Keys saved
  by an older version are moved into Credential Manager once, and removed from the vault only after
  they read back correctly.
* **macOS:** the Keychain, once you allow it in Settings; until then the encrypted vault file.
* **Linux:** VOOL's encrypted vault file. Its sealing key is in the same user profile, so it is
  protected by your account's file permissions only, not against someone who copies the profile.

`where are my keys stored?` in chat names the store this install really uses. It is not sent to
Parad0x Labs, and there is no Parad0x-operated endpoint in the request path.

## Plugins

A plugin runs with the runtime's access. That is what makes plugins useful and what makes
them worth reading before installing. See [Skills and plugins](../guides/skills-and-plugins.md).

## Reporting an issue

Report security issues privately through
[GitHub Security Advisories](https://github.com/Parad0x-Labs/vool/security/advisories/new),
never in a public issue. Include the version, the platform, and the steps to reproduce.

{% hint style="warning" %}
The macOS apps are ad-hoc signed and not notarized; the Linux tarball is unsigned. See
[Release status](release-status.md).
{% endhint %}
