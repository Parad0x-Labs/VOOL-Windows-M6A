# Windows native approval host (candidate)

This WinForms/WPF/WebView2 host is an isolated implementation candidate. It reviews
`/api/mode` and `/api/commands/dispatch` in a native dialog, signs the exact
request bytes with a per-launch stdin secret, and never exposes that secret to
page JavaScript. The Python API verifies the proof and rejects replay.
Dependency resolution is pinned in `packages.lock.json`.

The candidate now has a source-tested bridge for the 14 served-UI native
methods: Settings/Setup open and close, folder selection, answer PDF save,
chat export, pet appearance, and six detached Companion actions. The bridge
accepts only exact localhost top-level page routes and allowlisted methods;
exports fetch only the fixed loopback endpoint,
use a native save dialog, enforce response type and size, and refuse overwrite.
Source tests do not prove these windows and dialogs work in an installed app.

The host holds the same named window mutex that the installer checks. Pure
Windows companion policies now test presentation payload normalization and
script-safe encoding, monitor recovery, separate canvas and control hit
regions, transparent-margin click-through, and yielding the operator-answer
region. The API serves the existing Python-generated renderer at an exact,
loopback-only `/companion` page. The host has a candidate WPF transparent
composition window, with drag, pointer yielding, position callbacks, and
fail-closed initialization. A local source-API probe on September 20, 2026
showed the detached pet visible on the desktop and confirmed move, hide,
show, and return-to-app from Settings without a host crash. This was repeated
with the self-contained, unsigned published `VOOL.exe`; the probe was stopped
afterward. The host targets `net10.0-windows10.0.17763.0` so the
WebView2 WPF composition dependency is present in its build output. The
project pins the Windows x64 self-contained single-file publish settings; a
locked restore still passes after publishing. The fixed WebView2 runtime must
be staged separately by the eventual installer. This is not an installed-app
test. Live cross-process click-through, drag, controls, first-frame timing,
operator-answer yielding, restart persistence, multi-DPI, and installed-artifact
tests remain open.

The detached window must be proven against another process, not just a test
form. The candidate dynamically applies `WS_EX_TRANSPARENT` to its layered
window outside the canvas/controls or over the operator-answer region. Test
at multiple DPI scales, over approval dialogs, and over unrelated applications
before treating this as parity or switching hosts. Mixed-DPI screen/DOM
coordinate agreement remains specifically unproven.

This is **not** the installed VOOL window. Do not replace `vool_window.py` in
the installer yet: live Companion parity, app menu, deep links, notifications,
and lifecycle behavior have not been proven against the Mac experience. The Windows tool
broker, full authority census, per-user data isolation, signing, bundled
WebView2 runtime, and clean-VM installed-artifact acceptance are also still
release blockers. A successful source build or proof test is not release parity.

Build with .NET SDK 10.0.401 on Windows:

```powershell
dotnet restore installer/windows_host/VOOL.WindowsHost.csproj --locked-mode
dotnet build installer/windows_host/VOOL.WindowsHost.csproj --configuration Release --no-restore -warnaserror
dotnet publish installer/windows_host/VOOL.WindowsHost.csproj --configuration Release --no-restore
python -m pytest -q tests/test_windows_host_native_control.py
```
