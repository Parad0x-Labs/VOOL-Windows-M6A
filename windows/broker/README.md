# VOOL Windows broker

This framework-dependent .NET 10 x64 broker is a Phase A component. Nothing in VOOL calls it yet.
Build and test from this directory with SDK 10.0.402 (pinned in `global.json`). Packages restore
from nuget.org at the versions `Directory.Packages.props` pins:

```powershell
dotnet restore VoolBroker.sln
dotnet build VoolBroker.sln --no-restore
dotnet test --solution VoolBroker.sln --no-build
```

The broker accepts one length-prefixed UTF-8 JSON bootstrap frame on stdin: the secret, the API
process's pid and creation time, the flagged folders and the journal folder. It opens and holds
each flagged folder, creates an authenticated, current-user named pipe, and prints one start line
with its pipe name and process identity on stdout.

Operations: `ping`, `open_root`, `close_root`, `stat`, `read`, `write_file`, `ensure_directory`,
`delete`, `rename`, `apply_batch` and `restore_batch`. Every change runs as a journalled batch; the
journal lives in the journal folder (outside every flagged folder) and is replayed at the next
start. See `docs/WINDOWS_BROKER.md` for the protocol and the rules.

The Python client is `core/windows_broker`; `.github/workflows/windows-broker.yml` builds and
tests both on a hosted Windows runner.
