using System.Diagnostics;
using System.Net;
using System.Net.Sockets;
using VoolConfine.Core;
using Xunit;
using static VoolConfine.Tests.LauncherTestSupport;

namespace VoolConfine.Tests;

/// <summary>
/// The built VoolConfine.exe, run for real: exit codes and the status file, the AppContainer token,
/// the network block, write isolation per root, the whole-tree kill, breakaway, and the limits.
/// The children are in-box programs the box can read without any grant (cmd, whoami, curl,
/// Windows PowerShell 5.1), so no folder outside the test's own is touched.
/// </summary>
public class LauncherRunTests
{
    // ---- exit codes and the status file ----

    [Fact]
    public void The_childs_exit_code_comes_back_and_the_status_says_it_started()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", "exit 7"]);
        AssertStartedAndFinished(result, 7);
        var status = result.Status!.Value;
        Assert.False(status.GetProperty("timed_out").GetBoolean());
        Assert.Equal(System.Text.Json.JsonValueKind.Null, status.GetProperty("limit").ValueKind);
        Assert.Empty(status.GetProperty("new_repositories").EnumerateArray());
    }

    [Theory]
    [InlineData(123)]
    [InlineData(124)]
    [InlineData(125)]
    [InlineData(126)]
    public void A_child_that_exits_with_a_launcher_code_is_still_recorded_as_started(int code)
    {
        // The code alone proves nothing; the status file is what tells the cases apart.
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", $"exit {code}"]);
        AssertStartedAndFinished(result, code);
    }

    [Fact]
    public void Stdout_and_stderr_reach_the_caller()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", "echo to-out& echo to-err 1>&2"]);
        AssertStartedAndFinished(result, 0);
        Assert.Contains("to-out", result.Stdout);
        Assert.Contains("to-err", result.Stderr);
    }

    [Theory]
    [InlineData("cmd.exe")]
    [InlineData(@"C:\Windows\System32\..\System32\cmd.exe")]
    public void A_program_that_is_not_an_absolute_resolved_path_is_126_and_never_starts(string program)
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [program, "/c", "exit 0"]);
        AssertRefused(result, ExitCodes.PolicyInvalid);
    }

    [Fact]
    public void A_batch_file_is_126_and_never_runs()
    {
        using var box = new ConfineSandbox();
        var marker = Path.Combine(box.Outside, "batch-ran.txt");
        var batch = Path.Combine(box.Outside, "job.cmd");
        File.WriteAllText(batch, $"@echo ran> \"{marker}\"\r\n");
        var result = Launch(box, [batch]);
        AssertRefused(result, ExitCodes.PolicyInvalid);
        Assert.False(File.Exists(marker));
    }

    [Fact]
    public void A_missing_program_is_125_with_the_reason_in_the_status()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Path.Combine(System32, "vool-no-such-program.exe")]);
        AssertRefused(result, ExitCodes.SetupFailed);
        Assert.Contains("CreateProcess", result.Status!.Value.GetProperty("error").GetString());
    }

    [Fact]
    public void A_missing_temp_dir_is_125_and_the_child_never_starts()
    {
        using var box = new ConfineSandbox();
        Directory.Delete(box.Temp);
        var result = Launch(box, [Cmd, "/c", "exit 0"]);
        AssertRefused(result, ExitCodes.SetupFailed);
        Assert.Contains("temp_dir", result.Status!.Value.GetProperty("error").GetString());
    }

    [Fact]
    public void A_temp_dir_that_is_a_junction_is_125()
    {
        using var box = new ConfineSandbox();
        Directory.Delete(box.Temp);
        var (code, output) = RunPlain(Cmd, ["/c", "mklink", "/J", box.Temp, box.Outside]);
        Assert.True(code == 0, output);
        var result = Launch(box, [Cmd, "/c", "exit 0"]);
        AssertRefused(result, ExitCodes.SetupFailed);
    }

    [Fact]
    public void An_invalid_policy_is_126_and_writes_no_status()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", "exit 0"], p => p["surprise"] = 1);
        AssertRefused(result, ExitCodes.PolicyInvalid);
        Assert.Null(result.Status);
    }

    [Fact]
    public void A_stale_status_file_is_overwritten_before_anything_runs()
    {
        using var box = new ConfineSandbox();
        File.WriteAllText(box.StatusPath, """{"schema":"vool.windows_confinement.status.v1","child_started":true,"exit_code":0}""");
        var result = Launch(box, [Path.Combine(System32, "vool-no-such-program.exe")]);
        AssertRefused(result, ExitCodes.SetupFailed);
        Assert.False(result.Status!.Value.GetProperty("child_started").GetBoolean());
    }

    // ---- the token ----

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void The_child_is_a_low_integrity_appcontainer_with_only_its_own_capabilities(bool network)
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Whoami, "/groups", "/fo", "csv", "/nh"], p => p["network"] = network);
        AssertStartedAndFinished(result, 0);
        // whoami inside an AppContainer lists no capability SIDs at all (run 41903488067), so the
        // capabilities are proven by behaviour: the write tests (root) and the network tests (internet).
        Assert.True(result.Stdout.Contains("S-1-16-4096"), $"not Low integrity: {result.Stdout}");
    }

    // ---- network ----

    /// <summary>curl's exit codes for "could not resolve" (6), "could not connect" (7) and "timed out" (28).</summary>
    static readonly int[] Unreachable = [6, 7, 28];

    /// <summary>Runs curl, unconfined when box is null. Returns curl's exit code (0 = reached).</summary>
    static int RunCurl(string url, ConfineSandbox? box, bool network, out string detail)
    {
        string[] args = ["-s", "-S", "--max-time", "10", "--connect-timeout", "5", url];
        if (box is null)
        {
            var (code, output) = RunPlain(Curl, args);
            detail = $"unconfined curl exit {code}: {output}";
            return code;
        }
        var result = Launch(box, [Curl, .. args], p => p["network"] = network);
        detail = $"confined curl exit {result.ExitCode}; stdout: {result.Stdout}; stderr: {result.Stderr}";
        Assert.True(Started(result), detail);
        return result.ExitCode;
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void Loopback_is_blocked_with_or_without_network(bool network)
    {
        using var box = new ConfineSandbox();
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        try
        {
            var port = ((IPEndPoint)listener.LocalEndpoint).Port;
            var url = $"http://127.0.0.1:{port}/";
            var accepted = 0;
            var token = TestContext.Current.CancellationToken;
            _ = Task.Run(async () =>
            {
                while (true)
                {
                    using var client = await listener.AcceptTcpClientAsync(token);
                    Interlocked.Increment(ref accepted);
                    var stream = client.GetStream();
                    await stream.WriteAsync("HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n"u8.ToArray(), token);
                }
            }, token);
            // Positive control: the same request unconfined does reach the listener.
            Assert.True(RunCurl(url, null, network, out var plain) == 0, plain);
            Assert.Equal(1, Volatile.Read(ref accepted));
            // Blocked means curl could not connect, not any other failure.
            var code = RunCurl(url, box, network, out var confined);
            Assert.True(Unreachable.Contains(code), $"expected could-not-connect; {confined}");
            Assert.True(Volatile.Read(ref accepted) == 1, $"the box reached the listener; {confined}");
        }
        finally { listener.Stop(); }
    }

    const string External = "http://www.msftconnecttest.com/connecttest.txt";

    [Fact]
    public void The_internet_is_blocked_without_network_and_reachable_with_it()
    {
        Assert.True(RunCurl(External, null, false, out var plain) == 0, $"this machine has no internet, so the test proves nothing: {plain}");
        using (var box = new ConfineSandbox())
        {
            var code = RunCurl(External, box, network: false, out var blocked);
            Assert.True(Unreachable.Contains(code), $"expected could-not-connect; {blocked}");
        }
        using (var box = new ConfineSandbox())
            Assert.True(RunCurl(External, box, network: true, out var allowed) == 0, allowed);
    }

    // ---- files ----

    [Fact]
    public void The_child_writes_its_own_root_and_nothing_else()
    {
        using var box = new ConfineSandbox();
        using var other = new ConfineSandbox(); // granted to its own root's capability, not this job's
        var mine = Path.Combine(box.Work, "mine.txt");
        var theirs = Path.Combine(other.Work, "theirs.txt");
        var outside = Path.Combine(box.Outside, "outside.txt");
        var result = Launch(box, [Cmd, "/c", $"(echo a> {mine}) & (echo b> {theirs}) & (echo c> {outside}) & exit 0"]);
        AssertStartedAndFinished(result, 0);
        Assert.True(File.Exists(mine), $"the job could not write its own root; stderr: {result.Stderr}");
        Assert.False(File.Exists(theirs), "the job wrote another job's root");
        Assert.False(File.Exists(outside), "the job wrote a folder nobody granted");
    }

    [Fact]
    public void The_child_cannot_read_the_users_files()
    {
        using var box = new ConfineSandbox();
        var secret = Path.Combine(box.Outside, "secret.txt");
        File.WriteAllText(secret, "vool-secret-7f3a");
        var result = Launch(box, [Cmd, "/c", $"type {secret}"]);
        Assert.True(Started(result), result.Stderr);
        Assert.DoesNotContain("vool-secret-7f3a", result.Stdout);
        Assert.NotEqual(0, result.ExitCode);
    }

    [Fact]
    public void Temp_inside_the_child_is_the_policy_temp_dir()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", "echo TEMP=%TEMP%& echo TMP=%TMP%"]);
        AssertStartedAndFinished(result, 0);
        Assert.Contains($"TEMP={box.Temp}", result.Stdout);
        Assert.Contains($"TMP={box.Temp}", result.Stdout);
    }

    // ---- the tree ----

    const string Spin = "for /l %i in () do @rem";

    [Fact]
    public void Killing_the_launcher_ends_the_childs_whole_tree()
    {
        using var box = new ConfineSandbox();
        var tracked = new Dictionary<uint, nint>();
        try
        {
            Launch(box, [Cmd, "/c", $"{Cmd} /c {Spin}"], wait: TimeSpan.FromSeconds(60), whileRunning: launcher =>
            {
                var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
                while (tracked.Count < 2 && DateTime.UtcNow < deadline)
                {
                    Descendants((uint)launcher.Id, tracked);
                    Thread.Sleep(50);
                }
                Assert.True(tracked.Count >= 2, $"expected a child and a grandchild, saw {tracked.Count}");
                launcher.Kill(entireProcessTree: false); // the launcher only; the job must do the rest
            });
            Assert.True(AllEnded(tracked, TimeSpan.FromSeconds(10)), "a confined process outlived the launcher");
        }
        finally { Close(tracked); }
    }

    [Fact]
    public void A_grandchild_left_running_ends_when_the_child_exits()
    {
        using var box = new ConfineSandbox();
        var tracked = new Dictionary<uint, nint>();
        try
        {
            // The child starts a spinning grandchild in the background, counts for a moment, and exits 0.
            var result = Launch(box, [Cmd, "/c", $"start /b {Cmd} /c {Spin} & for /l %j in (1,1,150000) do @rem"],
                whileRunning: launcher =>
                {
                    var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
                    while (tracked.Count < 2 && !launcher.HasExited && DateTime.UtcNow < deadline)
                    {
                        Descendants((uint)launcher.Id, tracked);
                        Thread.Sleep(20);
                    }
                });
            AssertStartedAndFinished(result, 0);
            Assert.True(tracked.Count >= 2, $"expected a child and a grandchild, saw {tracked.Count}");
            Assert.True(AllEnded(tracked, TimeSpan.FromSeconds(5)), "a backgrounded grandchild outlived the job");
            Assert.DoesNotContain("still had processes", result.Stderr);
        }
        finally { Close(tracked); }
    }

    [Fact]
    public void A_job_past_max_seconds_is_124_and_its_tree_is_gone()
    {
        using var box = new ConfineSandbox();
        var tracked = new Dictionary<uint, nint>();
        try
        {
            var clock = Stopwatch.StartNew();
            var result = Launch(box, [Cmd, "/c", $"{Cmd} /c {Spin}"], p => p["max_seconds"] = 3,
                whileRunning: launcher =>
                {
                    var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(20);
                    while (tracked.Count < 2 && !launcher.HasExited && DateTime.UtcNow < deadline)
                    {
                        Descendants((uint)launcher.Id, tracked);
                        Thread.Sleep(50);
                    }
                });
            Assert.Equal(ExitCodes.Timeout, result.ExitCode);
            Assert.InRange(clock.Elapsed.TotalSeconds, 2.5, 30.0);
            var status = result.Status!.Value;
            Assert.True(status.GetProperty("child_started").GetBoolean());
            Assert.True(status.GetProperty("timed_out").GetBoolean());
            Assert.Equal("time", status.GetProperty("limit").GetString());
            Assert.True(tracked.Count >= 2, $"expected a child and a grandchild, saw {tracked.Count}");
            Assert.True(AllEnded(tracked, TimeSpan.FromSeconds(5)), "a process outlived the timeout");
        }
        finally { Close(tracked); }
    }

    // ---- breakaway ----

    const string CreateProcessScript = """
        $sig = '[DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)] public static extern bool CreateProcessW(string app, System.Text.StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string dir, byte[] si, byte[] pi);'
        $k = Add-Type -MemberDefinition $sig -Name K -Namespace VoolConfineTest -PassThru
        $si = New-Object byte[] 104; $si[0] = 104
        $pi = New-Object byte[] 24
        $cmd = New-Object System.Text.StringBuilder 'cmd.exe /c exit 0'
        $ok = $k::CreateProcessW("$env:SystemRoot\System32\cmd.exe", $cmd, [IntPtr]::Zero, [IntPtr]::Zero, $false, FLAGS, [IntPtr]::Zero, $null, $si, $pi)
        $e = [Runtime.InteropServices.Marshal]::GetLastWin32Error()
        if ($ok) { 'CREATED'; exit 20 } else { "REFUSED $e"; exit 21 }
        """;

    [Fact]
    public void A_plain_child_process_can_be_created_inside_the_box()
    {
        // The control for the breakaway test: the same script without the flag succeeds.
        using var box = new ConfineSandbox();
        var result = Launch(box, PowerShellArgs(CreateProcessScript.Replace("FLAGS", "0")));
        Assert.True(result.ExitCode == 20, $"exit {result.ExitCode}; out: {result.Stdout}; err: {result.Stderr}");
    }

    [Fact]
    public void Breakaway_from_the_job_is_refused()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, PowerShellArgs(CreateProcessScript.Replace("FLAGS", "0x01000000")));
        Assert.True(result.ExitCode == 21, $"exit {result.ExitCode}; out: {result.Stdout}; err: {result.Stderr}");
        Assert.Contains("REFUSED 5", result.Stdout); // ERROR_ACCESS_DENIED
    }

    [Fact]
    public void Wmi_cannot_start_a_process_outside_the_job()
    {
        using var box = new ConfineSandbox();
        var marker = Path.Combine(box.Outside, "wmi-ran.txt");
        var script = $$"""
            try {
              $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = 'cmd.exe /c echo x> "{{marker}}"' } -ErrorAction Stop
              "RETURN $($r.ReturnValue)"; if ($r.ReturnValue -eq 0) { exit 30 } else { exit 31 }
            } catch { "DENIED $($_.Exception.Message)"; exit 31 }
            """;
        var result = Launch(box, PowerShellArgs(script));
        Assert.True(Started(result), result.Stderr);
        Thread.Sleep(2000);
        Assert.False(File.Exists(marker), "WMI started a process outside the box");
        Assert.True(result.ExitCode == 31, $"exit {result.ExitCode}; out: {result.Stdout}; err: {result.Stderr}");
    }

    // ---- limits ----

    static string Allocate(int megabytes) => $$"""
        try { $b = New-Object byte[] ({{megabytes}}MB); for ($i = 0; $i -lt $b.Length; $i += 4096) { $b[$i] = 1 }; 'ALLOCATED'; exit 40 }
        catch { "FAILED $($_.Exception.GetType().Name)"; exit 41 }
        """;

    [Fact]
    public void An_allocation_under_memory_mb_succeeds()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, PowerShellArgs(Allocate(32)), p => p["memory_mb"] = 400);
        Assert.True(result.ExitCode == 40, $"exit {result.ExitCode}; out: {result.Stdout}; err: {result.Stderr}");
        Assert.Equal(System.Text.Json.JsonValueKind.Null, result.Status!.Value.GetProperty("limit").ValueKind);
    }

    [Fact]
    public void An_allocation_past_memory_mb_fails_and_the_status_says_memory()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, PowerShellArgs(Allocate(1024)), p => p["memory_mb"] = 400);
        Assert.True(Started(result), result.Stderr);
        Assert.NotEqual(40, result.ExitCode);
        Assert.DoesNotContain("ALLOCATED", result.Stdout);
        Assert.Equal("memory", result.Status!.Value.GetProperty("limit").GetString());
    }

    [Fact]
    public void A_process_chain_within_max_processes_runs()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", $"{Cmd} /c {Cmd} /c exit 0"], p => p["max_processes"] = 3);
        AssertStartedAndFinished(result, 0);
        Assert.Equal(System.Text.Json.JsonValueKind.Null, result.Status!.Value.GetProperty("limit").ValueKind);
    }

    [Fact]
    public void A_process_past_max_processes_is_refused_and_the_status_says_processes()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", $"{Cmd} /c {Cmd} /c exit 0"], p => p["max_processes"] = 2);
        Assert.True(Started(result), result.Stderr);
        Assert.NotEqual(0, result.ExitCode);
        Assert.Equal("processes", result.Status!.Value.GetProperty("limit").GetString());
    }

    // ---- the checks seam and resolved paths ----

    [Fact]
    public void The_shipped_launcher_refuses_every_run_until_the_acl_verify_exists()
    {
        using var box = new ConfineSandbox();
        var marker = Path.Combine(box.Work, "ran.txt");
        var result = Launch(box, [Cmd, "/c", $"echo ran> {marker}"], host: Host.Real);
        AssertRefused(result, ExitCodes.SetupFailed);
        Assert.Contains("chunk 2", result.Status!.Value.GetProperty("error").GetString());
        Assert.False(File.Exists(marker), "the child ran");
    }

    [Fact]
    public void A_missed_acl_verify_is_125_and_the_child_never_starts()
    {
        using var box = new ConfineSandbox();
        var marker = Path.Combine(box.Work, "ran.txt");
        var result = Launch(box, [Cmd, "/c", $"echo ran> {marker}"], host: Host.FailVerify);
        AssertRefused(result, ExitCodes.SetupFailed);
        Assert.False(File.Exists(marker), "the child ran");
    }

    [Fact]
    public void A_repository_made_by_the_job_is_123_and_listed_in_the_status()
    {
        using var box = new ConfineSandbox();
        var git = Path.Combine(box.Work, ".git");
        var result = Launch(box, [Cmd, "/c", $"mkdir {git}"]);
        Assert.True(Directory.Exists(git), $"the job could not make the folder; stderr: {result.Stderr}");
        Assert.Equal(ExitCodes.NewRepository, result.ExitCode);
        var status = result.Status!.Value;
        Assert.True(status.GetProperty("child_started").GetBoolean());
        Assert.Equal(0, status.GetProperty("exit_code").GetInt32());
        Assert.Equal(git, Assert.Single(status.GetProperty("new_repositories").EnumerateArray()).GetString(), ignoreCase: true);
    }

    [Fact]
    public void A_write_root_that_is_a_junction_is_126_and_the_child_never_starts()
    {
        using var box = new ConfineSandbox();
        var link = Path.Combine(box.Top, "link");
        var (code, output) = RunPlain(Cmd, ["/c", "mklink", "/J", link, box.Work]);
        Assert.True(code == 0, output);
        var marker = Path.Combine(box.Work, "ran.txt");
        var result = Launch(box, [Cmd, "/c", $"echo ran> {marker}"], p =>
        {
            p["write_roots"] = new[] { link };
            p["temp_dir"] = Path.Combine(link, "tmp");
        });
        AssertRefused(result, ExitCodes.PolicyInvalid);
        Assert.Contains("resolves to", result.Stderr);
        Assert.False(File.Exists(marker), "the child ran");
    }

    [Fact]
    public void A_status_folder_that_is_a_junction_is_126_and_nothing_is_written_through_it()
    {
        using var box = new ConfineSandbox();
        var link = Path.Combine(box.Top, "status-link");
        var (code, output) = RunPlain(Cmd, ["/c", "mklink", "/J", link, box.Outside]);
        Assert.True(code == 0, output);
        var result = Launch(box, [Cmd, "/c", "exit 0"], p => p["status_path"] = Path.Combine(link, "status.json"));
        Assert.Equal(ExitCodes.PolicyInvalid, result.ExitCode);
        Assert.False(File.Exists(Path.Combine(box.Outside, "status.json")), "the status was written through the junction");
    }

    [Fact]
    public void A_missing_write_root_is_126()
    {
        using var box = new ConfineSandbox();
        var missing = Path.Combine(box.Top, "missing");
        var result = Launch(box, [Cmd, "/c", "exit 0"], p => p["write_roots"] = new[] { box.Work, missing });
        AssertRefused(result, ExitCodes.PolicyInvalid);
    }

    [Fact]
    public void A_child_can_start_a_grandchild_by_its_full_path()
    {
        using var box = new ConfineSandbox();
        var result = Launch(box, [Cmd, "/c", $"cd & set & {Cmd} /c echo nested-ok"]);
        Assert.True(result.Stdout.Contains("nested-ok") && result.ExitCode == 0,
            $"exit {result.ExitCode}; stdout: {result.Stdout}; stderr: {result.Stderr}");
    }
}
