using VoolConfine.Core;

// VoolConfine.exe (docs/WINDOWS_CONFINEMENT.md section 2; the forms are in LauncherCli).
// Exit codes: the child's own, 123 new repository, 124 timeout, 125 confinement not set up (the
// child never started), 126 invalid command line or policy. Only the status file proves a start.
//
// Chunk 2 replaces PendingChecks and the two grant/revoke refusals with its Acl*/RepoScan* code.
// Until then every run is 125 and no child starts.
return LauncherCli.Run(args, PendingChecks.Instance,
    grant: _ => LauncherCli.Refuse(Console.Error, ExitCodes.SetupFailed, "grant is not built yet (chunk 2)"),
    revoke: _ => LauncherCli.Refuse(Console.Error, ExitCodes.SetupFailed, "revoke is not built yet (chunk 2)"),
    Console.Error);
