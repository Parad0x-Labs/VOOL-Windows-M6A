using System.Collections;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;

[assembly: InternalsVisibleTo("VoolConfine.Tests")]

namespace VoolConfine.Core;

/// <summary>Confinement could not be set up; the child never started (exit 125).</summary>
public sealed class SetupException(string message) : Exception(message);

/// <summary>
/// The filesystem checks the run form makes around the child (chunk 2, Acl*.cs and RepoScan*.cs):
/// verify the grants and deny ACEs before the child starts (a miss throws SetupException, so 125),
/// and the repository scan whose second half runs after the job ended.
/// </summary>
public interface IConfinementChecks
{
    /// <summary>Throws <see cref="SetupException"/> when the policy's grants or deny ACEs are not in place.</summary>
    void VerifyAcls(Policy policy, string packageSid);

    /// <summary>Records the repository layouts under the write roots before the child starts.</summary>
    IRepositoryScan StartRepositoryScan(Policy policy);
}

/// <summary>The second half of the repository scan, after the job has ended.</summary>
public interface IRepositoryScan
{
    /// <summary>Repository layouts (a .git, or a bare repository) that appeared under a write root.</summary>
    IReadOnlyList<string> NewRepositories();
}

/// <summary>
/// Runs one child confined (docs/WINDOWS_CONFINEMENT.md section 2). Every path that does not end
/// with the child started inside the AppContainer and the job returns 125 or 126, and the status
/// file then says child_started false (or is absent). Nothing ever runs unconfined.
/// </summary>
public static class Launcher
{
    /// <summary>How long the launcher waits for the kernel to report the job empty after ending it.</summary>
    static readonly TimeSpan KillWait = TimeSpan.FromSeconds(10);

    public static int Run(Policy policy, IReadOnlyList<string> argv, IConfinementChecks checks, TextWriter stderr)
    {
        ArgumentNullException.ThrowIfNull(checks);
        var status = new LauncherStatus(policy.StatusPath);
        try
        {
            // Before the first write to it, so a link can't carry the status file anywhere else.
            CheckResolved(Path.GetDirectoryName(policy.StatusPath) ?? policy.StatusPath, "status folder");
        }
        catch (PolicyException e)
        {
            return Refuse(stderr, ExitCodes.PolicyInvalid, e.Message);
        }

        string commandLine;
        try
        {
            commandLine = LauncherCommandLine.Build(argv);
        }
        catch (PolicyException e)
        {
            TryNotStarted(status, e.Message, stderr);
            return Refuse(stderr, ExitCodes.PolicyInvalid, e.Message);
        }

        try
        {
            // First, so a stale record can never be read as this run's.
            status.NotStarted("starting");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return Refuse(stderr, ExitCodes.SetupFailed, $"status file unwritable: {e.Message}");
        }

        try
        {
            return RunConfined(policy, argv[0], commandLine, checks, status, stderr);
        }
        catch (PolicyException e)
        {
            // A path that does not resolve on disk to its own spelling (section 3): nothing started.
            TryNotStarted(status, e.Message, stderr);
            return Refuse(stderr, ExitCodes.PolicyInvalid, e.Message);
        }
        catch (Exception e)
        {
            // SetupException or anything unexpected: the child is not running (the job, if it was
            // made, is closed by the using block below, which ends its tree).
            var reason = e is SetupException ? e.Message : $"{e.GetType().Name}: {e.Message}";
            TryNotStarted(status, reason, stderr);
            return Refuse(stderr, ExitCodes.SetupFailed, reason);
        }
    }

    static int RunConfined(Policy policy, string program, string commandLine, IConfinementChecks checks,
        LauncherStatus status, TextWriter stderr)
    {
        foreach (var root in policy.WriteRoots) CheckResolved(root, "write root");
        CheckTempDir(policy.TempDir);
        var packageSid = Sids.EnsureProfile();
        var capabilities = Sids.CapabilitiesFor(policy);
        var environment = ChildEnvironment(policy.TempDir);
        checks.VerifyAcls(policy, packageSid);
        var scan = checks.StartRepositoryScan(policy);

        using var job = ConfinementJob.Create(policy.MaxProcesses, policy.MemoryMb);
        using var child = LauncherProcess.CreateSuspendedInJob(program, commandLine, packageSid, capabilities, job, environment);
        try
        {
            if (!LauncherNative.IsProcessInJob(child.Process, job.Handle, out var inJob))
                throw new SetupException($"IsProcessInJob failed: {LastError()}");
            if (!inJob) throw new SetupException("the child is not in the job");
            if (LauncherNative.ResumeThread(child.Thread) == uint.MaxValue)
                throw new SetupException($"ResumeThread failed: {LastError()}");
        }
        catch
        {
            LauncherNative.TerminateProcess(child.Process, ExitCodes.SetupFailed);
            throw;
        }

        // Only now, after the suspended create inside the job and the resume, does the record say started.
        try
        {
            status.Started();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            job.KillAll(ExitCodes.SetupFailed, KillWait);
            throw new SetupException($"status file unwritable after start; the job was ended: {e.Message}");
        }

        var timedOut = !WaitForExit(child.Process, TimeSpan.FromSeconds(policy.MaxSeconds));
        uint code;
        if (timedOut)
        {
            code = ExitCodes.Timeout;
        }
        else if (!LauncherNative.GetExitCodeProcess(child.Process, out code))
        {
            job.KillAll(ExitCodes.SetupFailed, KillWait);
            throw new SetupException($"GetExitCodeProcess failed: {LastError()}");
        }

        // The main child is gone (or out of time). Whatever it left running in the job goes too,
        // so no grandchild outlives the run.
        // A job that will not empty can't be scanned or reported as finished: refuse the run (the
        // job handle closes on the way out, which ends whatever is left).
        if (!job.KillAll(timedOut ? (uint)ExitCodes.Timeout : 1u, KillWait))
            throw new SetupException($"the job still had processes {KillWait.TotalSeconds:0}s after it was ended; the run is refused");
        job.Drain(TimeSpan.FromSeconds(1));

        var exitCode = timedOut ? ExitCodes.Timeout : unchecked((int)code);
        var limit = timedOut ? "time" : job.LimitHit;
        // After the whole tree is gone, so nothing can add a repository behind the scan.
        var newRepositories = scan.NewRepositories();
        if (newRepositories.Count > 0)
        {
            status.Finished(exitCode, timedOut, limit, newRepositories);
            stderr.WriteLine($"{ExitCodes.RefusedMarker} a repository appeared under a write root: {string.Join(", ", newRepositories)}");
            return ExitCodes.NewRepository;
        }
        status.Finished(exitCode, timedOut, limit, newRepositories);
        return exitCode;
    }

    /// <summary>Waits for the child, true when it exited within the time. Long limits are waited in slices.</summary>
    static bool WaitForExit(nint process, TimeSpan limit)
    {
        var deadline = DateTime.UtcNow + limit;
        while (true)
        {
            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero) return LauncherNative.WaitForSingleObject(process, 0) == LauncherNative.WaitObject0;
            var slice = (uint)Math.Min(remaining.TotalMilliseconds + 1, 60_000);
            var result = LauncherNative.WaitForSingleObject(process, slice);
            if (result == LauncherNative.WaitObject0) return true;
            if (result != LauncherNative.WaitTimeout) throw new SetupException($"WaitForSingleObject failed: {LastError()}");
        }
    }

    /// <summary>
    /// docs/WINDOWS_CONFINEMENT.md section 3, "Resolved on disk": the folder, opened (links followed),
    /// must have exactly the final path it was given, case-insensitively. A junction, a symlink, a
    /// short name or a missing folder anywhere on the way is a PolicyException (126).
    /// </summary>
    internal static void CheckResolved(string folder, string what)
    {
        using var handle = LauncherNative.CreateFileW(folder, LauncherNative.FileReadAttributes, LauncherNative.FileShareAll, 0,
            LauncherNative.OpenExisting, LauncherNative.FileFlagBackupSemantics, 0);
        if (handle.IsInvalid) throw new PolicyException($"the {what} could not be opened: {folder}: {LastError()}");
        var buffer = new System.Text.StringBuilder(32768);
        var length = LauncherNative.GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity)
            throw new PolicyException($"the {what}'s final path could not be read: {folder}: {LastError()}");
        var final = buffer.ToString();
        const string Prefix = @"\\?\";
        if (!final.StartsWith(Prefix, StringComparison.Ordinal) || final.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase))
            throw new PolicyException($"the {what} resolves to {final}, not a drive-letter path: {folder}");
        final = final[Prefix.Length..];
        if (!string.Equals(final.TrimEnd('\\'), folder.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
            throw new PolicyException($"the {what} {folder} resolves to {final}; give the resolved path");
    }

    /// <summary>
    /// The temp folder must already exist as a real folder. The launcher never creates it: it runs as
    /// the user, and a link a previous job planted there could point a create outside the roots.
    /// </summary>
    static void CheckTempDir(string tempDir)
    {
        FileAttributes attributes;
        try { attributes = File.GetAttributes(tempDir); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            throw new SetupException($"temp_dir is not usable: {e.Message}");
        }
        if (!attributes.HasFlag(FileAttributes.Directory)) throw new SetupException("temp_dir is not a folder");
        if (attributes.HasFlag(FileAttributes.ReparsePoint)) throw new SetupException("temp_dir is a link or junction");
    }

    /// <summary>The launcher's own environment with TEMP and TMP set to temp_dir.</summary>
    internal static IReadOnlyDictionary<string, string> ChildEnvironment(string tempDir)
    {
        var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
            environment[(string)entry.Key] = (string?)entry.Value ?? "";
        environment["TEMP"] = tempDir;
        environment["TMP"] = tempDir;
        return environment;
    }

    static void TryNotStarted(LauncherStatus status, string reason, TextWriter stderr)
    {
        try { status.NotStarted(reason); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            stderr.WriteLine($"{ExitCodes.RefusedMarker} status file unwritable: {e.Message}");
        }
    }

    static int Refuse(TextWriter stderr, int code, string reason)
    {
        stderr.WriteLine($"{ExitCodes.RefusedMarker} {reason}");
        return code;
    }

    static string LastError() => new Win32Exception(Marshal.GetLastWin32Error()).Message;
}
