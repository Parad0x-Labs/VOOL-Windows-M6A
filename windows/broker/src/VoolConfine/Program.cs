using VoolConfine.Core;

// docs/WINDOWS_CONFINEMENT.md section 2. Three forms, all run by VOOL itself, never by a job:
//   VoolConfine.exe --policy <path> -- <program.exe> [args...]   run a job (verifies ACLs, never writes them)
//   VoolConfine.exe grant --policy <path>                        write the grants and deny ACEs (chunk 2)
//   VoolConfine.exe revoke --policy <path>                       remove exactly what grant added (chunk 2)
// Exit codes: the child's own, 123 new repository, 124 timeout, 125 confinement not set up (the
// child never started), 126 invalid command line or policy. Only the status file proves a start.

static int Refuse(int code, string reason)
{
    Console.Error.WriteLine($"{ExitCodes.RefusedMarker} {reason}");
    return code;
}

static Policy? Load(string path, out int failure)
{
    failure = 0;
    try
    {
        return Policy.Parse(File.ReadAllText(path));
    }
    catch (PolicyException e)
    {
        failure = Refuse(ExitCodes.PolicyInvalid, e.Message);
    }
    catch (IOException e)
    {
        failure = Refuse(ExitCodes.PolicyInvalid, $"policy unreadable: {e.Message}");
    }
    catch (UnauthorizedAccessException e)
    {
        failure = Refuse(ExitCodes.PolicyInvalid, $"policy unreadable: {e.Message}");
    }
    return null;
}

if (args.Length == 3 && (args[0] == "grant" || args[0] == "revoke") && args[1] == "--policy")
{
    if (Load(args[2], out var failure) is null) return failure;
    // Chunk 2 (Acl*.cs) fills these in. Until then nothing is granted, so a job can write nothing.
    return Refuse(ExitCodes.SetupFailed, $"{args[0]} is not built yet (chunk 2)");
}

var separator = Array.IndexOf(args, "--");
if (args.Length < 4 || args[0] != "--policy" || separator != 2 || separator == args.Length - 1)
    return Refuse(ExitCodes.PolicyInvalid,
        "usage: VoolConfine.exe --policy <path> -- <program.exe> [args...] | grant --policy <path> | revoke --policy <path>");
var policy = Load(args[1], out var loadFailure);
if (policy is null) return loadFailure;
return Launcher.Run(policy, args[(separator + 1)..], PendingChecks.Instance, Console.Error);

/// <summary>
/// Stands in for chunk 2's ACL verify and repository scan until that branch lands, and is replaced
/// by it in this one line of Program.cs. It verifies nothing. That is not a way out of the box:
/// without chunk 2's grants the container can write nowhere at all. But the run form must not
/// ship like this, so the port only takes this branch together with chunk 2 or after it.
/// </summary>
sealed class PendingChecks : IConfinementChecks, IRepositoryScan
{
    internal static readonly PendingChecks Instance = new();
    public void VerifyAcls(Policy policy, string packageSid) { }
    public IRepositoryScan StartRepositoryScan(Policy policy) => this;
    public IReadOnlyList<string> NewRepositories() => [];
}
