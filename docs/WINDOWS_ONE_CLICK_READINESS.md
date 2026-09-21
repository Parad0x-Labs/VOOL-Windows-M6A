# Windows one-click readiness

Status: **not ready for public release**. This file describes a development
handoff command, not a signed, one-download VOOL installer or a parity claim.

`Test_VOOL_Windows_Stack.cmd` calls `tools/windows_stack_handoff.ps1` for the
repositories named in that script. Its `fast` profile runs a bounded source
check; `-Profile release` requests those repositories' release checks. The
command writes a JSON report under `dist/windows-stack-handoff/`. A passing
handoff does not install VOOL, validate the native host, sign an artifact, or
exercise a clean Windows machine. It is separate from the VOOL Windows release
gate and is not needed by someone who downloads the eventual app.

The release gate remains blocked by the remaining detached Companion pointer,
multi-DPI, and installed-artifact proof; installer integration; unresolved
authority rows; a Windows effect broker; signed-artifact attestation; and clean
Windows 10/11 x64 installation evidence.
The candidate Windows UI also currently shows Mac `⌘` shortcut glyphs and a
Swift/Xcode speech-toolchain notice. Those platform-specific affordances must
be corrected and tested before a usability-parity claim.
See `docs/windows_parity/authority_findings.md` for the current source audit.
No Phantom wallet signing, authentication, or private-key behavior is in scope
for this handoff.
