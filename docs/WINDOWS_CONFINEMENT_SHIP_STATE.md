# VoolConfine shipping (chunk 4): state when B was paused, 4 Nov 2026

B is paused and nothing from it ships in this release. This branch, windows/port-confine-4-ship,
is left as it was so the work can be picked up later. It is not merged and has no PR.

## What is here
- 95dd1348: windows/broker/publish.ps1 also publishes VoolConfine self-contained into
  `<Output>\confine` (StartupHookSupport=false) and writes the pin (every file there with its
  SHA-256). build_bundle.ps1 puts the pin in `<Stage>\config` and checks the staged folder.
  vool.iss installs the pin as `{app}\config\confine-pin.json` and clears `{app}\broker\confine`
  on upgrade. sandbox/windows_confinement_pin.py refuses the launcher unless the folder holds
  exactly the pinned files, and on Windows holds them open with read-only sharing.
- ed9c0ad3: install smoke checks (tools/windows_install_smoke.ps1 + tools/confine_pin_check.py).
  This is test-contract; Jesse OK'd it on 4 Nov ("OK install smoke checks ed9c0ad3"). Keep it as
  its own commit.
- 16ae38d6: the shipping tests run on Windows with no skip (the windows-broker job allows none).
- Merged with windows/port-confine-0-interface at 20ad8a3a.

## Evidence
- windows-broker run 41906836335 at 16ae38d6: 269 passed, 0 skipped.
- Install smoke run 41903491140 at 41f28136: green; install/confine_launcher_pinned and
  uninstall/confine_launcher_removed PASS.
- Owner review (Broker): GRN, jobs/broker/confinement-B/reviews/REVIEW-chunk4-owner-broker.md.
- Linux: tests/windows_broker plus the installer, bundle and smoke tests gave 356 passed,
  19 skipped. The repo-wide scan was stopped at the pause: 34 of about 92 files had run, all green.

## Not done
- No Notes review yet. Re-run the repo-wide Linux scan.
- The shipped launcher is still chunk 0's stub (it exits 125). Shipping it is only useful once
  chunks 1-3 land.
- Before shipping, chunk 3 must drop CORECLR_*, COR_*, COMPlus_*, DOTNET_* and COREHOST_* from
  the launcher's environment, and put each launcher in VOOL's kill-on-close job (doc section 2).
- Shipping costs installer size: a second self-contained .NET runtime (about 70 MB before
  compression).
