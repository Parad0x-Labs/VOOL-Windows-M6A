using VoolConfine.Core;

// VoolConfine.exe (docs/WINDOWS_CONFINEMENT.md section 2; the forms are in LauncherCli).
// Exit codes: the child's own, 123 new repository, 124 timeout, 125 confinement not set up (the
// child never started), 126 invalid command line or policy. Only the status file proves a start.
// The run form verifies the ACLs (AclChecks) before the child starts and scans after the job.
return LauncherCli.Run(args, AclChecks.Instance,
    grant: policy => AclChecks.Grant(policy, Console.Error),
    revoke: policy => AclChecks.Revoke(policy, Console.Error),
    Console.Error);
