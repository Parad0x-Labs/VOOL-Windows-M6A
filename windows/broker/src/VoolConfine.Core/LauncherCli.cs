namespace VoolConfine.Core;

/// <summary>
/// The launcher's command line (docs/WINDOWS_CONFINEMENT.md section 2). Three forms, all run by VOOL
/// itself, never by a job:
///   --policy &lt;path&gt; -- &lt;program.exe&gt; [args...]   run a job (verifies ACLs, never writes them)
///   grant --policy &lt;path&gt;                        write the grants and deny ACEs (chunk 2)
///   revoke --policy &lt;path&gt;                       remove exactly what grant added (chunk 2)
/// </summary>
public static class LauncherCli
{
    public const string Usage =
        "usage: VoolConfine.exe --policy <path> -- <program.exe> [args...] | grant --policy <path> | revoke --policy <path>";

    /// <summary>
    /// Parses and runs one form. <paramref name="grant"/> and <paramref name="revoke"/> take the
    /// parsed policy and return an exit code; the run form goes through <see cref="Launcher.Run"/>.
    /// </summary>
    public static int Run(string[] args, IConfinementChecks checks, Func<Policy, int> grant, Func<Policy, int> revoke,
        TextWriter stderr)
    {
        if (args.Length == 3 && (args[0] == "grant" || args[0] == "revoke") && args[1] == "--policy")
        {
            var loaded = Load(args[2], stderr, out var failure);
            if (loaded is null) return failure;
            return args[0] == "grant" ? grant(loaded) : revoke(loaded);
        }
        var separator = Array.IndexOf(args, "--");
        if (args.Length < 4 || args[0] != "--policy" || separator != 2 || separator == args.Length - 1)
            return Refuse(stderr, ExitCodes.PolicyInvalid, Usage);
        var policy = Load(args[1], stderr, out var loadFailure);
        if (policy is null) return loadFailure;
        return Launcher.Run(policy, args[(separator + 1)..], checks, stderr);
    }

    static Policy? Load(string path, TextWriter stderr, out int failure)
    {
        failure = 0;
        try
        {
            return Policy.Parse(File.ReadAllText(path));
        }
        catch (PolicyException e)
        {
            failure = Refuse(stderr, ExitCodes.PolicyInvalid, e.Message);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            failure = Refuse(stderr, ExitCodes.PolicyInvalid, $"policy unreadable: {e.Message}");
        }
        return null;
    }

    public static int Refuse(TextWriter stderr, int code, string reason)
    {
        stderr.WriteLine($"{ExitCodes.RefusedMarker} {reason}");
        return code;
    }
}

/// <summary>
/// Stands in for chunk 2's ACL verify and repository scan until that branch lands. Unknown means
/// unavailable: it refuses, so the run form exits 125 and no child ever starts. Chunk 2 replaces
/// it in Program.cs with its real checks.
/// </summary>
public sealed class PendingChecks : IConfinementChecks
{
    public static readonly PendingChecks Instance = new();

    public const string Reason = "the ACL verify is not built yet (chunk 2), so no job can run";

    public void VerifyAcls(Policy policy, string packageSid) => throw new SetupException(Reason);

    public IRepositoryScan StartRepositoryScan(Policy policy) => throw new SetupException(Reason);
}
