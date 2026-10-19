# VOOL Windows broker

This framework-dependent .NET 10 x64 broker is a phase A component. It is not wired into VOOL.
Build and test from this directory with SDK 10.0.402 and the repository's local `.nuget` cache:

```powershell
dotnet build VoolBroker.sln --no-restore
dotnet test VoolBroker.sln --no-restore
```

The broker accepts one length-prefixed UTF-8 JSON bootstrap frame on stdin. It creates an
authenticated, current-user named pipe and prints its name and process identity on stdout. In this
phase it accepts only `ping`; flagged roots are refused until phase 3b. It writes no runtime state.
