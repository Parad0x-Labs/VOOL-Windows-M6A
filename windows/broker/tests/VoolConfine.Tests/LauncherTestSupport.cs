using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using VoolConfine.Core;
using Xunit;

// The launcher tests start real processes and count them; one at a time keeps the counts exact.
[assembly: CollectionBehavior(DisableTestParallelization = true)]

namespace VoolConfine.Tests;

/// <summary>What one launcher run produced.</summary>
internal sealed record LaunchResult(int ExitCode, string Stdout, string Stderr, JsonElement? Status, int LauncherPid);

/// <summary>
/// A throwaway folder layout for one test: T\work (the write root, granted to its capability SID
/// the way the ACL chunk will), T\work\tmp (temp_dir), T\status (outside the root, for the status
/// file), and T\outside (granted to nobody).
/// </summary>
internal sealed class ConfineSandbox : IDisposable
{
    internal string Top { get; }
    internal string Work { get; }
    internal string Temp { get; }
    internal string StatusPath { get; }
    internal string Outside { get; }

    internal ConfineSandbox(bool grantWork = true)
    {
        Top = Path.Combine(LongTempPath(), "vc-" + Guid.NewGuid().ToString("N")[..12]);
        Work = Path.Combine(Top, "work");
        Temp = Path.Combine(Work, "tmp");
        Outside = Path.Combine(Top, "outside");
        Directory.CreateDirectory(Temp);
        Directory.CreateDirectory(Outside);
        Directory.CreateDirectory(Path.Combine(Top, "status"));
        StatusPath = Path.Combine(Top, "status", "status.json");
        if (grantWork) Grant(Work, Sids.RootCapabilitySid(Work));
    }

    /// <summary>Read and write, inherited by new files and folders, for one SID (what chunk 2 will do).</summary>
    internal static void Grant(string folder, string sid)
    {
        var (code, output) = LauncherTestSupport.RunPlain(Path.Combine(Environment.SystemDirectory, "icacls.exe"),
            [folder, "/grant", $"*{sid}:(OI)(CI)M"]);
        Assert.True(code == 0, $"icacls grant failed ({code}): {output}");
    }

    internal Dictionary<string, object> Policy(Action<Dictionary<string, object>>? change = null)
    {
        var policy = new Dictionary<string, object>
        {
            ["schema"] = VoolConfine.Core.Policy.Schema,
            ["write_roots"] = new[] { Work },
            ["read_roots"] = Array.Empty<string>(),
            ["deny_write_paths"] = Array.Empty<string>(),
            ["deny_write_git"] = true,
            ["network"] = false,
            ["max_seconds"] = 60,
            ["memory_mb"] = 1024,
            ["max_processes"] = 16,
            ["temp_dir"] = Temp,
            ["status_path"] = StatusPath,
        };
        change?.Invoke(policy);
        return policy;
    }

    /// <summary>The temp folder with 8.3 short names expanded, so paths match what the box sees.</summary>
    static string LongTempPath()
    {
        var path = Path.GetTempPath();
        var buffer = new StringBuilder(32768);
        var length = GetLongPathNameW(path, buffer, (uint)buffer.Capacity);
        return length > 0 && length < buffer.Capacity ? buffer.ToString() : path;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern uint GetLongPathNameW(string shortPath, StringBuilder longPath, uint length);

    public void Dispose()
    {
        try { Directory.Delete(Top, recursive: true); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }
}

internal static class LauncherTestSupport
{
    internal static string System32 => Environment.SystemDirectory;
    internal static string Cmd => Path.Combine(System32, "cmd.exe");
    internal static string Curl => Path.Combine(System32, "curl.exe");
    internal static string PowerShell => Path.Combine(System32, "WindowsPowerShell", "v1.0", "powershell.exe");
    internal static string Whoami => Path.Combine(System32, "whoami.exe");

    /// <summary>
    /// The launcher the solution build produced, beside this test assembly's own configuration. A
    /// missing launcher fails the test; it is never skipped.
    /// </summary>
    internal static string LauncherExe()
    {
        var baseDirectory = AppContext.BaseDirectory;
        var directory = new DirectoryInfo(baseDirectory);
        while (directory is not null && !File.Exists(Path.Combine(directory.FullName, "VoolBroker.sln")))
            directory = directory.Parent;
        Assert.True(directory is not null, $"VoolBroker.sln not found above {baseDirectory}");
        // ...\tests\VoolConfine.Tests\bin\<Configuration>\net10.0-windows\
        var parts = baseDirectory.TrimEnd('\\').Split('\\');
        var bin = Array.LastIndexOf(parts, "bin");
        Assert.True(bin >= 0 && bin + 2 < parts.Length, $"unexpected test output folder {baseDirectory}");
        var exe = Path.Combine(directory!.FullName, "src", "VoolConfine", "bin", parts[bin + 1], parts[bin + 2], "VoolConfine.exe");
        Assert.True(File.Exists(exe), $"the launcher was not built: {exe}");
        return exe;
    }

    /// <summary>PowerShell's -EncodedCommand form, so no quoting layer touches the script.</summary>
    internal static string[] PowerShellArgs(string script) =>
        [PowerShell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand",
            Convert.ToBase64String(Encoding.Unicode.GetBytes(script))];

    internal static ProcessStartInfo StartInfo(string policyPath, IReadOnlyList<string> argv)
    {
        var start = new ProcessStartInfo(LauncherExe())
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            CreateNoWindow = true,
            // The box can read System32 (ALL APPLICATION PACKAGES); the test folder it can't.
            WorkingDirectory = System32,
        };
        start.ArgumentList.Add("--policy");
        start.ArgumentList.Add(policyPath);
        start.ArgumentList.Add("--");
        foreach (var arg in argv) start.ArgumentList.Add(arg);
        return start;
    }

    internal static string WritePolicy(ConfineSandbox box, Dictionary<string, object> policy)
    {
        var path = Path.Combine(box.Top, "status", "policy.json");
        File.WriteAllText(path, JsonSerializer.Serialize(policy));
        return path;
    }

    internal static LaunchResult Launch(ConfineSandbox box, IReadOnlyList<string> argv,
        Action<Dictionary<string, object>>? change = null, TimeSpan? wait = null, Action<Process>? whileRunning = null)
    {
        var policyPath = WritePolicy(box, box.Policy(change));
        using var process = Process.Start(StartInfo(policyPath, argv))!;
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync(TestContext.Current.CancellationToken);
        var stderr = process.StandardError.ReadToEndAsync(TestContext.Current.CancellationToken);
        whileRunning?.Invoke(process);
        if (!process.WaitForExit(wait ?? TimeSpan.FromSeconds(90)))
        {
            process.Kill(entireProcessTree: true);
            Assert.Fail("the launcher did not exit in time");
        }
        process.WaitForExit();
        // The child holds copies of these pipes, so they only close when the whole tree is gone. A
        // pipe still open well after the launcher exited means a confined process outlived it.
        Assert.True(Task.WaitAll(new Task[] { stdout, stderr }, TimeSpan.FromSeconds(20)),
            "the launcher exited but its stdout/stderr stayed open: a confined process outlived it");
        JsonElement? status = null;
        if (File.Exists(box.StatusPath))
            status = JsonDocument.Parse(File.ReadAllText(box.StatusPath)).RootElement.Clone();
        return new LaunchResult(process.ExitCode, stdout.Result, stderr.Result, status, process.Id);
    }

    /// <summary>Runs a program unconfined (a positive control, or icacls) and returns its code and output.</summary>
    internal static (int Code, string Output) RunPlain(string program, IReadOnlyList<string> args, TimeSpan? wait = null)
    {
        var start = new ProcessStartInfo(program)
        {
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        foreach (var arg in args) start.ArgumentList.Add(arg);
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync(TestContext.Current.CancellationToken);
        var stderr = process.StandardError.ReadToEndAsync(TestContext.Current.CancellationToken);
        if (!process.WaitForExit(wait ?? TimeSpan.FromSeconds(60)))
        {
            process.Kill(entireProcessTree: true);
            Assert.Fail($"{program} did not exit in time");
        }
        process.WaitForExit();
        return (process.ExitCode, stdout.Result + stderr.Result);
    }

    internal static bool Started(LaunchResult result) =>
        result.Status is { } s && s.GetProperty("child_started").GetBoolean();

    internal static void AssertStartedAndFinished(LaunchResult result, int exitCode)
    {
        Assert.True(result.Status is not null, $"no status file; stderr: {result.Stderr}");
        var status = result.Status!.Value;
        Assert.Equal(ExitCodes.StatusSchema, status.GetProperty("schema").GetString());
        Assert.True(status.GetProperty("child_started").GetBoolean(), $"child_started false; stderr: {result.Stderr}");
        Assert.Equal(exitCode, status.GetProperty("exit_code").GetInt32());
        Assert.Equal(exitCode, result.ExitCode);
    }

    internal static void AssertRefused(LaunchResult result, int exitCode)
    {
        Assert.Equal(exitCode, result.ExitCode);
        Assert.Contains(ExitCodes.RefusedMarker, result.Stderr);
        if (result.Status is { } status)
            Assert.False(status.GetProperty("child_started").GetBoolean(), "a refused run must not say the child started");
    }

    // ---- process tree tracking (toolhelp), so a test can prove a descendant is gone ----

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct ProcessEntry32
    {
        public uint Size, Usage, ProcessId;
        public nint DefaultHeapId;
        public uint ModuleId, Threads, ParentProcessId;
        public int PriorityClassBase;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)] public string ExeFile;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern nint CreateToolhelp32Snapshot(uint flags, uint processId);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool Process32FirstW(nint snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool Process32NextW(nint snapshot, ref ProcessEntry32 entry);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool CloseHandle(nint handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern nint OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint WaitForSingleObject(nint handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool GetProcessTimes(nint process, out long creation, out long exit, out long kernel, out long user);

    const uint Synchronize = 0x00100000;
    const uint QueryLimitedInformation = 0x1000;

    /// <summary>
    /// Every live process descended from <paramref name="rootPid"/>, each held open by a handle so its
    /// id can't be reused before the test checks it. A parent id only counts when the parent started
    /// before the child, which rules out a reused parent id.
    /// </summary>
    internal static Dictionary<uint, nint> Descendants(uint rootPid, Dictionary<uint, nint>? into = null)
    {
        var found = into ?? new Dictionary<uint, nint>();
        var entries = new List<(uint Pid, uint Parent, string Exe)>();
        var snapshot = CreateToolhelp32Snapshot(2, 0);
        Assert.True(snapshot != -1, "CreateToolhelp32Snapshot failed");
        try
        {
            var entry = new ProcessEntry32 { Size = (uint)Marshal.SizeOf<ProcessEntry32>() };
            if (Process32FirstW(snapshot, ref entry))
                do entries.Add((entry.ProcessId, entry.ParentProcessId, entry.ExeFile));
                while (Process32NextW(snapshot, ref entry));
        }
        finally { CloseHandle(snapshot); }

        var known = new HashSet<uint>(found.Keys) { rootPid };
        bool grew;
        do
        {
            grew = false;
            foreach (var (pid, parent, _) in entries)
            {
                if (known.Contains(pid) || !known.Contains(parent)) continue;
                var handle = OpenProcess(Synchronize | QueryLimitedInformation, false, pid);
                if (handle == 0) continue; // already gone
                if (found.TryGetValue(parent, out var parentHandle) && !StartedBefore(parentHandle, handle))
                {
                    CloseHandle(handle);
                    continue;
                }
                found[pid] = handle;
                known.Add(pid);
                grew = true;
            }
        } while (grew);
        return found;
    }

    static bool StartedBefore(nint parent, nint child) =>
        GetProcessTimes(parent, out var parentStart, out _, out _, out _)
        && GetProcessTimes(child, out var childStart, out _, out _, out _)
        && parentStart <= childStart;

    /// <summary>True when every tracked process has ended within the wait.</summary>
    internal static bool AllEnded(Dictionary<uint, nint> processes, TimeSpan wait)
    {
        var deadline = DateTime.UtcNow + wait;
        foreach (var handle in processes.Values)
        {
            var remaining = deadline - DateTime.UtcNow;
            var ms = remaining <= TimeSpan.Zero ? 0u : (uint)remaining.TotalMilliseconds;
            if (WaitForSingleObject(handle, ms) != 0) return false;
        }
        return true;
    }

    internal static void Close(Dictionary<uint, nint> processes)
    {
        foreach (var handle in processes.Values) CloseHandle(handle);
        processes.Clear();
    }
}
