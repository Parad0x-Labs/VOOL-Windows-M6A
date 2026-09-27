using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;
using Vool.WindowsBroker;

internal static class Program
{
    private const int SecurityMandatoryLowRid = 0x1000;
    private const uint ExpectedJobLimitFlags =
        0x00000008u // JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        | 0x00000002u // JOB_OBJECT_LIMIT_PROCESS_TIME
        | 0x00000100u // JOB_OBJECT_LIMIT_PROCESS_MEMORY
        | 0x00000200u // JOB_OBJECT_LIMIT_JOB_MEMORY
        | 0x00002000u; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    private const uint ExpectedCpuControlFlags =
        0x00000001u // JOB_OBJECT_CPU_RATE_CONTROL_ENABLE
        | 0x00000004u; // JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
    private const uint ExpectedActiveProcessLimit = 16;
    private const ulong ExpectedProcessMemoryLimitBytes = 256UL * 1024UL * 1024UL;
    private const ulong ExpectedJobMemoryLimitBytes = 512UL * 1024UL * 1024UL;
    private const long ExpectedPerProcessUserTimeLimit100Ns = 30L * 10_000_000L;
    private const uint ExpectedCpuRate = 5_000;
    private const int TimeoutExitCode = unchecked((int)0xC000013A);

    private static async Task<int> Main(string[] args)
    {
        if (args.Length > 0 && args[0].Equals("--plugin-cleanup-child", StringComparison.Ordinal))
        {
            if (args.Length != 2
                || !int.TryParse(args[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out var delay)
                || delay < 0
                || delay > 60_000)
            {
                return 7;
            }

            Thread.Sleep(delay);
            return 0;
        }

        try
        {
            var brokerPath = ParseBrokerPath(args);
            var installedImagePath = ParseInstalledImagePath(args);
            var support = WindowsExecutionBroker.GetSupportReport();
            Console.WriteLine($"Broker: {brokerPath}");
            Console.WriteLine($"Support: {JsonSerializer.Serialize(support)}");

            if (!support.Supported)
            {
                Console.Error.WriteLine($"[FAIL] WindowsExecutionBroker is unsupported: {support.Reason}");
                return 2;
            }

            var tests = new List<(string Name, Func<Task> Run)>
            {
                ("AppContainer token has no capabilities and low integrity", () =>
                    VerifyAppContainerTokenAsync(brokerPath)),
                ("Job Object limits match the exact policy", () =>
                    VerifyJobLimitsAsync(brokerPath)),
                ("AppContainer can write the broker scratch directory", () =>
                    VerifyScratchWriteAsync(brokerPath)),
                ("AppContainer cannot read a non-AppContainer file", () =>
                    VerifyFileReadDeniedAsync(brokerPath)),
                ("AppContainer cannot create a file outside scratch", () =>
                    VerifyFileWriteDeniedAsync(brokerPath)),
                ("AppContainer cannot connect to a live localhost listener", () =>
                    VerifySocketDeniedAsync(brokerPath)),
                ("Unlisted inheritable parent handle is not passed to the child", () =>
                    VerifyHandleIsolationAsync(brokerPath)),
                ("Windows arguments survive quoting and empty values", () =>
                    VerifyArgumentRoundTripAsync(brokerPath)),
                ("Timeout terminates the process through its Job Object", () =>
                    VerifyTimeoutTerminationAsync(brokerPath)),
                ("Cancellation terminates the job and cleans scratch", () =>
                    VerifyCancellationAsync(brokerPath)),
                ("UNC and device executable paths are rejected", () =>
                    VerifyPathRejectionAsync(brokerPath)),
                ("Executable and ancestor reparse points are rejected", () =>
                    VerifyReparseRejectionAsync(brokerPath)),
                ("Staged executable rename and write are blocked before launch", () =>
                    VerifyStagedExecutableLockedAsync(brokerPath)),
                ("System32 image launches directly without scratch staging", () =>
                    VerifySystemImageLaunchAsync()),
                ("Child process remains contained by the Job Object", () =>
                    VerifyChildJobContainmentAsync(brokerPath)),
                ("Broker kills a long-lived descendant before returning", () =>
                    VerifyDescendantTerminationAsync(brokerPath)),
                ("Plugin broker removes process, ACL, scratch, and AppContainer residue", () =>
                    VerifyPluginResidueCleanupAsync()),
            };
            if (installedImagePath is not null)
            {
                tests.Add(("Installed Store image resists same-user mutation and launches directly", () =>
                    VerifyInstalledStoreImageLaunchAsync(installedImagePath)));
            }

            var failures = 0;
            foreach (var test in tests)
            {
                Console.WriteLine($"[RUN ] {test.Name}");
                try
                {
                    await test.Run().ConfigureAwait(false);
                    Console.WriteLine($"[PASS] {test.Name}");
                }
                catch (Exception exception)
                {
                    failures++;
                    Console.Error.WriteLine($"[FAIL] {test.Name}: {exception}");
                }
            }

            Console.WriteLine($"Summary: {tests.Count - failures} passed, {failures} failed.");
            return failures == 0 ? 0 : 1;
        }
        catch (HarnessAssertionException exception)
        {
            Console.Error.WriteLine($"[FAIL] Harness setup: {exception.Message}");
            return 2;
        }
        catch (Exception exception)
        {
            Console.Error.WriteLine($"[FAIL] Harness setup: {exception}");
            Console.Error.WriteLine("Usage: vool-broker-tests.exe <published-self-contained-vool-broker.exe> [--store-winget <installed-winget.exe>]");
            return 2;
        }
    }

    private static string ParseBrokerPath(IReadOnlyList<string> args)
    {
        var path = args.Count switch
        {
            1 => args[0],
            2 when args[0].Equals("--broker", StringComparison.OrdinalIgnoreCase) => args[1],
            3 when args[1].Equals("--store-winget", StringComparison.OrdinalIgnoreCase) => args[0],
            _ => throw new HarnessAssertionException(
                "Usage: vool-broker-tests.exe <published-self-contained-vool-broker.exe> [--store-winget <installed-winget.exe>]"),
        };

        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
            throw new HarnessAssertionException("The broker EXE argument must be an absolute path.");

        path = Path.GetFullPath(path);
        if (!path.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
            throw new HarnessAssertionException("The broker argument must be a .exe path.");
        if (!File.Exists(path))
            throw new HarnessAssertionException($"Published broker EXE was not found: {path}");

        var fileInfo = new FileInfo(path);
        if (fileInfo.Length == 0)
            throw new HarnessAssertionException($"Published broker EXE is empty: {path}");

        return path;
    }

    private static string? ParseInstalledImagePath(IReadOnlyList<string> args)
    {
        if (args.Count != 3)
            return null;

        var path = args[2];
        if (string.IsNullOrWhiteSpace(path) || !Path.IsPathFullyQualified(path))
            throw new HarnessAssertionException("The installed winget image must have an absolute path.");
        path = Path.GetFullPath(path);
        if (!Path.GetFileName(path).Equals("winget.exe", StringComparison.OrdinalIgnoreCase)
            || !File.Exists(path))
        {
            throw new HarnessAssertionException("The installed Store image must be an existing winget.exe.");
        }

        return path;
    }

    private static async Task VerifyAppContainerTokenAsync(string brokerPath)
    {
        var result = await RunProbeAsync(brokerPath, TimeSpan.FromSeconds(10), "--inspect")
            .ConfigureAwait(false);
        _ = RequireSuccessfulProbe(result, "AppContainer token");

        Assert(result.StagedExecutable, "The broker did not report a staged executable.");
        Assert(result.ProcessId > 0, $"The broker returned an invalid process ID: {result.ProcessId}.");
        Assert(result.Token.IsAppContainer, "The child token is not an AppContainer token.");
        Assert(result.Token.HasRestrictions, "The child token has no restricted-token marker.");
        AssertEqual(0, result.Token.Capabilities.Count, "AppContainer capability count");
        AssertEqual(SecurityMandatoryLowRid, result.Token.IntegrityLevelRid, "AppContainer integrity RID");
        AssertEqual("Low", result.Token.IntegrityLevel, "AppContainer integrity name");
        Assert(!string.IsNullOrWhiteSpace(result.Token.AppContainerSid), "AppContainer SID is empty.");
    }

    private static async Task VerifyJobLimitsAsync(string brokerPath)
    {
        var result = await RunProbeAsync(brokerPath, TimeSpan.FromSeconds(10), "--inspect")
            .ConfigureAwait(false);
        _ = RequireSuccessfulProbe(result, "Job Object policy");

        AssertEqual(ExpectedJobLimitFlags, result.Job.LimitFlags, "Job Object limit flags");
        AssertEqual(ExpectedActiveProcessLimit, result.Job.ActiveProcessLimit, "Active process limit");
        AssertEqual(
            ExpectedProcessMemoryLimitBytes,
            result.Job.ProcessMemoryLimitBytes,
            "Per-process memory limit");
        AssertEqual(
            ExpectedJobMemoryLimitBytes,
            result.Job.JobMemoryLimitBytes,
            "Job memory limit");
        AssertEqual(
            ExpectedPerProcessUserTimeLimit100Ns,
            result.Job.PerProcessUserTimeLimit100Ns,
            "Per-process user time limit");
        AssertEqual(ExpectedCpuControlFlags, result.Job.CpuControlFlags, "CPU control flags");
        AssertEqual(ExpectedCpuRate, result.Job.CpuRate, "CPU rate");
    }

    private static async Task VerifyScratchWriteAsync(string brokerPath)
    {
        var scratchName = $"security-harness-{Guid.NewGuid():N}.txt";
        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromSeconds(10),
                "--write-scratch",
                scratchName)
            .ConfigureAwait(false);
        var probe = RequireSuccessfulProbe(result, "scratch write");

        Assert(probe.GetProperty("scratchWrite").GetBoolean(), "Probe did not confirm the scratch write.");
        AssertEqual(scratchName, probe.GetProperty("scratchName").GetString(), "Scratch file name");
        Assert(result.Scratch.AccessRulesProtected, "Scratch ACL is not protected from inheritance.");
        Assert(result.Scratch.AppContainerCanWrite, "Scratch ACL does not grant AppContainer write access.");
        Assert(
            result.Scratch.AllowedSids.Contains(
                result.Scratch.AppContainerSid,
                StringComparer.OrdinalIgnoreCase),
            "Scratch ACL does not contain the reported AppContainer SID.");
    }

    private static async Task VerifyFileReadDeniedAsync(string brokerPath)
    {
        using var fixture = PrivateFileFixture.Create();
        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromSeconds(10),
                "--file",
                fixture.FilePath)
            .ConfigureAwait(false);
        var probe = RequireSuccessfulProbe(result, "non-AppContainer file denial");

        Assert(!probe.GetProperty("fileRead").GetBoolean(), "The AppContainer read a non-AppContainer file.");
        Assert(
            probe.TryGetProperty("fileError", out var fileError)
                && fileError.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(fileError.GetString()),
            "The denied file read did not report an error.");
        Assert(
            !fixture.FilePath.Equals(result.Scratch.Path, StringComparison.OrdinalIgnoreCase),
            "The denial fixture unexpectedly points at broker scratch.");
        AssertNoAppContainerAllowRule(fixture.FilePath, result.Token.AppContainerSid);
    }

    private static async Task VerifyFileWriteDeniedAsync(string brokerPath)
    {
        using var fixture = PrivateFileFixture.Create();
        var targetPath = Path.Combine(fixture.DirectoryPath, "outside-appcontainer-write-absent.txt");
        Assert(!File.Exists(targetPath), $"Write-denial target unexpectedly exists: {targetPath}");

        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromSeconds(10),
                "--write-file",
                targetPath)
            .ConfigureAwait(false);
        var probe = RequireProbe(result, "non-AppContainer file write denial");

        Assert(
            probe.TryGetProperty("fileWrite", out var fileWrite)
                && fileWrite.ValueKind == JsonValueKind.False,
            $"Probe did not report fileWrite=false. Probe JSON: {result.Stdout}");
        Assert(!File.Exists(targetPath), $"AppContainer created an outside-scratch file: {targetPath}");
        Assert(
            result.ExitCode == 0,
            $"Write-denial probe exit code was {result.ExitCode}. Probe JSON: {result.Stdout}");
    }

    private static async Task VerifySocketDeniedAsync(string brokerPath)
    {
        using var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var endpoint = (IPEndPoint)listener.LocalEndpoint;
        Assert(endpoint.Port > 0, "The localhost listener did not receive a port.");

        using (var livenessClient = new TcpClient())
        {
            livenessClient.Connect(IPAddress.Loopback, endpoint.Port);
            using var accepted = listener.AcceptTcpClient();
        }

        Assert(listener.Server.IsBound, "The localhost listener stopped before the broker probe.");
        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromSeconds(10),
                "--socket",
                "127.0.0.1",
                endpoint.Port.ToString(CultureInfo.InvariantCulture))
            .ConfigureAwait(false);
        var probe = RequireSuccessfulProbe(result, "localhost socket denial");

        Assert(!probe.GetProperty("socketConnected").GetBoolean(), "The AppContainer connected to localhost.");
        Assert(
            probe.TryGetProperty("socketError", out var socketError)
                && socketError.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(socketError.GetString()),
            "The denied localhost socket did not report an error.");
    }

    private static async Task VerifyHandleIsolationAsync(string brokerPath)
    {
        using var fixture = PrivateFileFixture.Create();
        using var handle = File.OpenHandle(fixture.FilePath, FileMode.Open, FileAccess.Read, FileShare.Read);
        var rawHandle = handle.DangerousGetHandle();
        Assert(
            SetHandleInformation(rawHandle, HandleFlagInherit, HandleFlagInherit),
            $"Could not mark fixture handle inheritable: {Marshal.GetLastWin32Error()}.");
        try
        {
            Assert(
                GetHandleInformation(rawHandle, out var flags)
                    && (flags & HandleFlagInherit) != 0,
                "Fixture handle was not inheritable before child launch.");
            var handleText = unchecked((ulong)rawHandle.ToInt64()).ToString("X", CultureInfo.InvariantCulture);
            var result = await RunProbeAsync(
                    brokerPath,
                    TimeSpan.FromSeconds(10),
                    "--handle",
                    handleText,
                    fixture.FilePath)
                .ConfigureAwait(false);
            var probe = RequireSuccessfulProbe(result, "inherited handle denial");
            Assert(
                !probe.GetProperty("handlePointsToExpected").GetBoolean(),
                "The child inherited a parent file handle outside the stdio allowlist.");
            Assert(
                result.InheritedHandleAllowlist.SequenceEqual(new[] { "stdin", "stdout", "stderr" }),
                "Broker reported an unexpected inherited-handle allowlist.");
        }
        finally
        {
            SetHandleInformation(rawHandle, HandleFlagInherit, 0);
        }
    }

    private static async Task VerifyArgumentRoundTripAsync(string brokerPath)
    {
        var values = new[] { "", "two words", "a\"quote", @"trailing\", @"backslash\""quote" };
        foreach (var value in values)
        {
            var result = await RunProbeAsync(
                    brokerPath,
                    TimeSpan.FromSeconds(10),
                    "--echo-value", value)
                .ConfigureAwait(false);
            var probe = RequireSuccessfulProbe(result, "argument round trip");
            AssertEqual(value, probe.GetProperty("echoValue").GetString(), "Argument round trip");
        }
    }

    private static async Task VerifyTimeoutTerminationAsync(string brokerPath)
    {
        var stopwatch = Stopwatch.StartNew();
        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromMilliseconds(250),
                "--sleep-ms",
                "5000")
            .ConfigureAwait(false);
        stopwatch.Stop();

        Assert(result.TimedOut, "The long-running probe was not marked as timed out.");
        AssertEqual(TimeoutExitCode, result.ExitCode, "Timeout process exit code");
        Assert(
            stopwatch.Elapsed < TimeSpan.FromSeconds(10),
            $"Timeout handling took too long: {stopwatch.Elapsed}.");
        Assert(ProcessHasExited(result.ProcessId), $"Timed-out process {result.ProcessId} is still running.");
        AssertScratchRemoved(result, "timeout");
    }

    private static async Task VerifyCancellationAsync(string brokerPath)
    {
        var scratchRoot = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "VOOL", "broker-scratch");
        var before = Directory.Exists(scratchRoot)
            ? Directory.GetDirectories(scratchRoot).OrderBy(path => path).ToArray()
            : Array.Empty<string>();
        using var cancellation = new CancellationTokenSource();
        cancellation.CancelAfter(TimeSpan.FromMilliseconds(250));
        var watch = Stopwatch.StartNew();
        var canceled = false;
        try
        {
            await new WindowsExecutionBroker().ExecuteAsync(
                    new BrokerExecutionRequest(
                        brokerPath,
                        new[] { "probe", "--sleep-ms", "5000" },
                        TimeSpan.FromSeconds(10)),
                    cancellation.Token)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            canceled = true;
        }
        watch.Stop();
        Assert(canceled, "Broker did not surface cancellation.");
        Assert(watch.Elapsed < TimeSpan.FromSeconds(4), $"Cancellation took too long: {watch.Elapsed}.");
        var after = Directory.Exists(scratchRoot)
            ? Directory.GetDirectories(scratchRoot).OrderBy(path => path).ToArray()
            : Array.Empty<string>();
        Assert(before.SequenceEqual(after), "Cancellation left a new scratch directory behind.");
    }

    private static async Task VerifyPathRejectionAsync(string brokerPath)
    {
        var paths = new[]
        {
            (Name: "UNC", Path: @"\\server\share\vool-broker.exe"),
            (Name: "NT device", Path: @"\\?\C:\vool-broker.exe"),
            (Name: "Win32 device", Path: @"\\.\pipe\vool-broker.exe"),
        };

        foreach (var item in paths)
        {
            Exception? observed = null;
            try
            {
                await new WindowsExecutionBroker()
                    .ExecuteAsync(new BrokerExecutionRequest(
                        item.Path,
                        Array.Empty<string>(),
                        TimeSpan.FromSeconds(1)))
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                observed = exception;
            }

            Assert(
                observed is BrokerSecurityException,
                $"{item.Name} path was not rejected with BrokerSecurityException: "
                + (observed is null ? "no exception" : observed.ToString()));
        }
    }

    private static async Task VerifyReparseRejectionAsync(string brokerPath)
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-reparse-test", Guid.NewGuid().ToString("N"));
        var target = Path.Combine(root, "target");
        var link = Path.Combine(root, "link");
        var executable = Path.Combine(target, "payload.exe");
        var fileLink = Path.Combine(root, "payload.exe");
        Directory.CreateDirectory(target);
        try
        {
            File.Copy(brokerPath, executable);
            Directory.CreateSymbolicLink(link, target);
            File.CreateSymbolicLink(fileLink, executable);
            foreach (var path in new[] { Path.Combine(link, "payload.exe"), fileLink })
            {
                Exception? observed = null;
                try
                {
                    await new WindowsExecutionBroker().ExecuteAsync(
                        new BrokerExecutionRequest(path, Array.Empty<string>(), TimeSpan.FromSeconds(1)))
                        .ConfigureAwait(false);
                }
                catch (Exception exception)
                {
                    observed = exception;
                }

                Assert(observed is BrokerSecurityException,
                    $"Reparse path was not refused: {path}; observed {observed}");
            }
        }
        finally
        {
            if (Directory.Exists(link))
                Directory.Delete(link);
            if (File.Exists(fileLink))
                File.Delete(fileLink);
            if (Directory.Exists(target))
                Directory.Delete(target, recursive: true);
            if (Directory.Exists(root))
                Directory.Delete(root);
        }
    }

    private static Task VerifyStagedExecutableLockedAsync(string brokerPath)
    {
        using var appContainer = AppContainerSession.Create();
        using var scratch = ScratchWorkspace.Create(appContainer.Sid, TokenInspector.CurrentUserSid());
        using var source = WindowsFileOpen.OpenSourceExecutable(brokerPath);
        var stagedPath = scratch.StageExecutable(source);
        var movedPath = Path.Combine(scratch.PayloadPath, "moved.exe");
        byte[] originalHash;
        using (var stagedRead = File.OpenRead(stagedPath))
            originalHash = SHA256.HashData(stagedRead);
        var moved = false;
        var renameDenied = false;
        try
        {
            try
            {
                File.Move(stagedPath, movedPath);
                moved = true;
            }
            catch (IOException exception) when ((exception.HResult & 0xFFFF) == 32)
            {
                renameDenied = true;
            }
        }
        finally
        {
            if (moved)
                File.Move(movedPath, stagedPath);
        }

        var writeDenied = false;
        try
        {
            using var write = new FileStream(stagedPath, FileMode.Open, FileAccess.Write, FileShare.ReadWrite);
        }
        catch (IOException exception) when ((exception.HResult & 0xFFFF) == 32)
        {
            writeDenied = true;
        }

        Assert(renameDenied, "A same-user process can rename the staged executable before launch.");
        Assert(writeDenied, "A same-user process can open the staged executable for writing before launch.");
        Assert(File.Exists(stagedPath) && !File.Exists(movedPath), "The staged path changed during the swap test.");
        using var stagedAfter = File.OpenRead(stagedPath);
        Assert(originalHash.SequenceEqual(SHA256.HashData(stagedAfter)), "Staged payload bytes changed during the swap test.");
        return Task.CompletedTask;
    }

    private static async Task VerifySystemImageLaunchAsync()
    {
        var imagePath = Path.Combine(Environment.SystemDirectory, "whoami.exe");
        Assert(File.Exists(imagePath), $"System32 test image is missing: {imagePath}");

        var result = await RunDirectImageAsync(imagePath, "/user").ConfigureAwait(false);
        Assert(!result.TimedOut && result.ExitCode == 0,
            $"Direct System32 launch failed: {result.ExitCode}; {result.Stdout}; {result.Stderr}");
        Assert(!string.IsNullOrWhiteSpace(result.Stdout),
            "Direct System32 child produced no identity output.");
    }

    private static async Task VerifyInstalledStoreImageLaunchAsync(string imagePath)
    {
        PathSafety.RejectReparseAncestors(imagePath);
        var writeDenied = false;
        try
        {
            using var write = new FileStream(imagePath, FileMode.Open, FileAccess.Write, FileShare.ReadWrite);
        }
        catch (UnauthorizedAccessException exception) when ((exception.HResult & 0xFFFF) == 5)
        {
            writeDenied = true;
        }
        Assert(writeDenied, "A same-user process can open the installed Store image for writing.");

        var deleteRaw = NativeMethods.CreateFileW(
            imagePath, 0x00010000, NativeMethods.FileShareRead | NativeMethods.FileShareWrite
                | NativeMethods.FileShareDelete, IntPtr.Zero, NativeMethods.OpenExisting,
            NativeMethods.FileAttributeNormal, IntPtr.Zero);
        var deleteError = Marshal.GetLastWin32Error();
        using var delete = new SafeNativeHandle(deleteRaw, ownsHandle: true);
        Assert(delete.IsInvalid && deleteError == 5,
            $"A same-user process obtained DELETE access to the installed Store image (Win32 {deleteError}).");

        var result = await RunDirectImageAsync(imagePath, "--version").ConfigureAwait(false);
        Assert(!result.TimedOut && result.ExitCode == 0,
            $"Direct Store image launch failed: {result.ExitCode}; {result.Stdout}; {result.Stderr}");
        Assert(result.Stdout.TrimStart().StartsWith('v'),
            $"Direct Store image returned unexpected version output: {result.Stdout}; {result.Stderr}");
    }

    private static async Task<NativeProcessResult> RunDirectImageAsync(
        string imagePath,
        params string[] arguments)
    {

        using var appContainer = AppContainerSession.Create();
        using var job = WindowsJob.Create();
        var systemRoot = Environment.GetEnvironmentVariable("SystemRoot")
            ?? throw new HarnessAssertionException("SystemRoot is unavailable.");
        var environment = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SystemRoot"] = systemRoot,
            ["WINDIR"] = systemRoot,
            ["SystemDrive"] = Path.GetPathRoot(systemRoot)?.TrimEnd(Path.DirectorySeparatorChar) ?? "C:",
            ["PATH"] = Environment.SystemDirectory + ";" + systemRoot,
            ["TEMP"] = Environment.SystemDirectory,
            ["TMP"] = Environment.SystemDirectory,
            ["LOCALAPPDATA"] = Environment.SystemDirectory,
            ["APPDATA"] = Environment.SystemDirectory,
            ["USERPROFILE"] = Environment.SystemDirectory,
            ["HOME"] = Environment.SystemDirectory,
            ["VOOL_BROKER_APP_CONTAINER"] = "1",
            ["PROCESSOR_ARCHITECTURE"] = "AMD64",
        };

        using var process = NativeProcessLauncher.Start(
            appContainer.SidPointer,
            appContainer.Sid.Value,
            job,
            imagePath,
            arguments,
            Environment.SystemDirectory,
            environment);
        var result = await process.WaitAsync(job.Handle, TimeSpan.FromSeconds(10))
            .ConfigureAwait(false);

        Assert(process.TokenSecurity.IsAppContainer,
            "Direct child lost its AppContainer token.");
        return result;
    }

    private static async Task VerifyChildJobContainmentAsync(string brokerPath)
    {
        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromSeconds(10),
                "--spawn-child-report",
                $"child-{Guid.NewGuid():N}.json")
            .ConfigureAwait(false);
        var probe = RequireSuccessfulProbe(result, "child Job Object containment");

        Assert(probe.TryGetProperty("child", out var child), "Child containment probe did not run.");
        Assert(child.ValueKind == JsonValueKind.Object, "Child containment probe returned an invalid payload.");
        var childFields = child.EnumerateObject().Select(property => property.Name).ToArray();
        Assert(
            childFields.Length == 2
                && childFields.Contains("ExitCode", StringComparer.Ordinal)
                && childFields.Contains("InJob", StringComparer.Ordinal),
            "Child containment payload must contain exactly ExitCode and InJob fields. "
            + $"Observed fields: [{string.Join(", ", childFields)}]. "
            + $"Probe JSON: {result.Stdout}");

        Assert(
            child.TryGetProperty("ExitCode", out var childExitCode)
                && childExitCode.ValueKind == JsonValueKind.Number
                && childExitCode.TryGetInt32(out var exitCode)
                && exitCode == 0,
            "Child containment ExitCode must be integer exit code 0.");
        Assert(
            child.TryGetProperty("InJob", out var childInJob)
                && childInJob.ValueKind == JsonValueKind.True,
            "Child containment InJob must be boolean true for Job Object membership.");
    }

    private static async Task VerifyDescendantTerminationAsync(string brokerPath)
    {
        var result = await RunProbeAsync(
                brokerPath,
                TimeSpan.FromSeconds(10),
                "--spawn-child-sleeper", "5000")
            .ConfigureAwait(false);
        var probe = RequireSuccessfulProbe(result, "descendant termination");
        var childPid = probe.GetProperty("childSleeperPid").GetInt32();
        Assert(childPid > 0, "Probe returned an invalid descendant PID.");
        Assert(ProcessHasExited(childPid), $"Descendant {childPid} survived the broker return.");
    }

    private static async Task VerifyPluginResidueCleanupAsync()
    {
        var root = Path.Combine(
            Path.GetTempPath(),
            "vool-broker-plugin-cleanup-harness",
            Guid.NewGuid().ToString("N"));
        var pluginRoot = Path.Combine(root, "plugin");
        var scratchRoot = Path.Combine(root, "scratch");
        Directory.CreateDirectory(pluginRoot);
        Directory.CreateDirectory(scratchRoot);
        var harnessExecutable = Environment.ProcessPath
            ?? throw new HarnessAssertionException("The test process path is unavailable.");
        var harnessDirectory = Path.GetDirectoryName(harnessExecutable)
            ?? throw new HarnessAssertionException("The test process directory is unavailable.");
        CopyDirectoryContents(harnessDirectory, pluginRoot);
        var pluginExecutable = Path.Combine(pluginRoot, Path.GetFileName(harnessExecutable));
        var pluginAcl = DirectoryAcl(pluginRoot);
        var scratchAcl = DirectoryAcl(scratchRoot);
        var payloadHashes = CaptureFileHashes(pluginRoot);
        var broker = new WindowsExecutionBroker();
        try
        {
            var success = await broker.ExecutePluginAsync(
                    BuildPluginCleanupRequest(
                        pluginRoot,
                        scratchRoot,
                        pluginExecutable,
                        operationId: "success",
                        arguments: new[] { "--plugin-cleanup-child", "0" }))
                .ConfigureAwait(false);
            Assert(
                success.Ok && success.Status == "completed",
                $"Plugin success cleanup case failed: status={success.Status}, exit={success.ExitCode}, "
                + $"timed_out={success.TimedOut}, stdout={success.ChildStdout}, stderr={success.ChildStderr}, error={success.Error}");
            AssertPluginResidueRemoved(
                success,
                pluginRoot,
                scratchRoot,
                payloadHashes,
                pluginAcl,
                scratchAcl,
                "success");

            var denied = false;
            try
            {
                await broker.ExecutePluginAsync(
                        BuildPluginCleanupRequest(
                            pluginRoot,
                            scratchRoot,
                            pluginExecutable,
                            operationId: "denied-network",
                            arguments: new[] { "--plugin-cleanup-child", "0" },
                            allowNetwork: true))
                    .ConfigureAwait(false);
            }
            catch (BrokerSecurityException)
            {
                denied = true;
            }

            Assert(denied, "Network-enabled plugin request was not refused before launch.");
            AssertEqual(pluginAcl, DirectoryAcl(pluginRoot), "Plugin ACL after denied request");
            AssertEqual(scratchAcl, DirectoryAcl(scratchRoot), "Scratch ACL after denied request");
            Assert(
                FilesMatch(pluginRoot, payloadHashes),
                "Denied request changed plugin root.");
            Assert(!Directory.EnumerateFileSystemEntries(scratchRoot).Any(), "Denied request changed scratch root.");

            var timedOut = await broker.ExecutePluginAsync(
                    BuildPluginCleanupRequest(
                        pluginRoot,
                        scratchRoot,
                        pluginExecutable,
                        operationId: "timeout",
                        arguments: new[] { "--plugin-cleanup-child", "10000" },
                        timeoutMilliseconds: 1_000))
                .ConfigureAwait(false);
            Assert(timedOut.TimedOut && timedOut.Status == "timeout", "Plugin timeout cleanup case did not time out.");
            AssertPluginResidueRemoved(
                timedOut,
                pluginRoot,
                scratchRoot,
                payloadHashes,
                pluginAcl,
                scratchAcl,
                "timeout");

            var failed = await broker.ExecutePluginAsync(
                    BuildPluginCleanupRequest(
                        pluginRoot,
                        scratchRoot,
                        pluginExecutable,
                        operationId: "failed",
                        arguments: new[] { "--plugin-cleanup-child", "-1" }))
                .ConfigureAwait(false);
            Assert(!failed.Ok && failed.Status == "handler_failed" && failed.ExitCode != 0,
                "Plugin nonzero-exit cleanup case did not preserve failure truth.");
            AssertPluginResidueRemoved(
                failed,
                pluginRoot,
                scratchRoot,
                payloadHashes,
                pluginAcl,
                scratchAcl,
                "failed");
        }
        finally
        {
            DeleteDirectoryTree(root);
        }
    }

    private static void CopyDirectoryContents(string sourceDirectory, string destinationDirectory)
    {
        foreach (var directory in Directory.GetDirectories(sourceDirectory, "*", SearchOption.AllDirectories))
        {
            var relative = Path.GetRelativePath(sourceDirectory, directory);
            Directory.CreateDirectory(Path.Combine(destinationDirectory, relative));
        }

        foreach (var file in Directory.GetFiles(sourceDirectory, "*", SearchOption.AllDirectories))
        {
            var relative = Path.GetRelativePath(sourceDirectory, file);
            var destination = Path.Combine(destinationDirectory, relative);
            Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
            File.Copy(file, destination, overwrite: true);
        }
    }

    private static IReadOnlyDictionary<string, string> CaptureFileHashes(string root)
    {
        return Directory.GetFiles(root, "*", SearchOption.AllDirectories)
            .ToDictionary(
                path => path,
                path => Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))),
                StringComparer.OrdinalIgnoreCase);
    }

    private static bool FilesMatch(string root, IReadOnlyDictionary<string, string> expected)
    {
        var actual = CaptureFileHashes(root);
        return actual.Count == expected.Count
            && actual.All(pair => expected.TryGetValue(pair.Key, out var hash) && hash == pair.Value);
    }

    private static PluginBrokerRequest BuildPluginCleanupRequest(
        string pluginRoot,
        string scratchRoot,
        string executablePath,
        string operationId,
        IReadOnlyList<string> arguments,
        bool allowNetwork = false,
        int timeoutMilliseconds = 10_000)
    {
        return new PluginBrokerRequest
        {
            Schema = "vool.plugin_broker.v1",
            PluginId = "vool-database",
            PluginVersion = "0.1.0",
            PluginDigest = new string('a', 64),
            OperationId = operationId,
            ExecutablePath = executablePath,
            Arguments = arguments,
            WorkingDirectory = pluginRoot,
            PluginRoot = pluginRoot,
            ScratchRoot = scratchRoot,
            ReadRoots = new[] { pluginRoot, scratchRoot },
            WritableRoots = new[] { scratchRoot },
            AllowNetwork = allowNetwork,
            TimeoutMilliseconds = timeoutMilliseconds,
            EnvironmentAllowlist = Array.Empty<string>(),
            Environment = new Dictionary<string, string>(),
        };
    }

    private static void AssertPluginResidueRemoved(
        PluginBrokerExecutionResult result,
        string pluginRoot,
        string scratchRoot,
        IReadOnlyDictionary<string, string> payloadHashes,
        string pluginAcl,
        string scratchAcl,
        string context)
    {
        var confinement = result.Confinement
            ?? throw new HarnessAssertionException($"{context} returned no confinement evidence.");
        Assert(ProcessHasExited(confinement.ProcessId), $"{context} process survived broker return.");
        AssertEqual(pluginAcl, DirectoryAcl(pluginRoot), $"Plugin ACL after {context}");
        AssertEqual(scratchAcl, DirectoryAcl(scratchRoot), $"Scratch ACL after {context}");
        Assert(
            FilesMatch(pluginRoot, payloadHashes),
            $"{context} changed the plugin package contents.");
        Assert(!Directory.EnumerateFileSystemEntries(scratchRoot).Any(), $"{context} changed scratch root.");
        AssertNoAppContainerAllowRule(pluginRoot, confinement.AppContainerSid);
        AssertNoAppContainerAllowRule(scratchRoot, confinement.AppContainerSid);
        AssertAppContainerProfileDeleted(confinement.AppContainerName, context);
    }

    private static void DeleteDirectoryTree(string path)
    {
        if (Directory.Exists(path))
            Directory.Delete(path, recursive: true);
    }

    private static string DirectoryAcl(string path) =>
        new DirectoryInfo(path)
            .GetAccessControl(AccessControlSections.Access)
            .GetSecurityDescriptorSddlForm(AccessControlSections.Access);

    private static void AssertAppContainerProfileDeleted(string name, string context)
    {
        var result = NativeMethods.CreateAppContainerProfile(
            name,
            "VOOL cleanup audit",
            "Temporary profile lifecycle check.",
            IntPtr.Zero,
            0,
            out var sidPointer);
        Assert(result >= 0, $"{context} AppContainer profile still exists: HRESULT 0x{result:X8}.");
        if (sidPointer != IntPtr.Zero)
            NativeMethods.FreeSid(sidPointer);
        var deleted = NativeMethods.DeleteAppContainerProfile(name);
        Assert(deleted >= 0, $"{context} cleanup audit could not remove its verification profile: HRESULT 0x{deleted:X8}.");
    }

    private static async Task<BrokerExecutionResult> RunProbeAsync(
        string brokerPath,
        TimeSpan timeout,
        params string[] arguments)
    {
        var commandArguments = new string[arguments.Length + 1];
        commandArguments[0] = "probe";
        Array.Copy(arguments, 0, commandArguments, 1, arguments.Length);
        var request = new BrokerExecutionRequest(brokerPath, commandArguments, timeout);
        return await new WindowsExecutionBroker().ExecuteAsync(request).ConfigureAwait(false);
    }

    private static JsonElement RequireSuccessfulProbe(
        BrokerExecutionResult result,
        string context)
    {
        var probe = RequireProbe(result, context);
        Assert(
            result.ExitCode == 0,
            $"{context} probe exit code was {result.ExitCode}. "
            + $"Stdout: {result.Stdout}; Stderr: {result.Stderr}");
        return probe;
    }

    private static JsonElement RequireProbe(
        BrokerExecutionResult result,
        string context)
    {
        Assert(
            !result.TimedOut,
            $"{context} unexpectedly timed out. Stdout: {result.Stdout}; Stderr: {result.Stderr}");
        Assert(result.ProcessId > 0, $"{context} returned an invalid process ID.");
        Assert(!string.IsNullOrWhiteSpace(result.Stdout), $"{context} produced no probe JSON.");

        JsonElement probe;
        try
        {
            using var document = JsonDocument.Parse(result.Stdout);
            probe = document.RootElement.Clone();
        }
        catch (JsonException exception)
        {
            throw new HarnessAssertionException(
                $"{context} produced invalid JSON: {exception.Message}. Output: {result.Stdout}");
        }

        Assert(
            probe.TryGetProperty("probe", out var probeMarker)
                && probeMarker.ValueKind == JsonValueKind.True,
            $"{context} output was not a probe result: {result.Stdout}; Stderr: {result.Stderr}");
        if (probe.TryGetProperty("securityInspectionError", out var inspectionError))
        {
            throw new HarnessAssertionException(
                $"{context} security inspection failed: {inspectionError.GetString()}");
        }
        AssertScratchRemoved(result, context);
        return probe;
    }

    private static void AssertScratchRemoved(BrokerExecutionResult result, string context)
    {
        Assert(
            !string.IsNullOrWhiteSpace(result.Scratch.Path)
                && Path.IsPathFullyQualified(result.Scratch.Path),
            $"{context} returned an invalid scratch path: {result.Scratch.Path}");
        Assert(
            !Directory.Exists(result.Scratch.Path) && !File.Exists(result.Scratch.Path),
            $"{context} left the scratch path behind: {result.Scratch.Path}");
    }

    private static bool ProcessHasExited(int processId)
    {
        var deadline = Stopwatch.GetTimestamp()
            + (long)(Stopwatch.Frequency * TimeSpan.FromSeconds(2).TotalSeconds);
        while (Stopwatch.GetTimestamp() < deadline)
        {
            try
            {
                using var process = Process.GetProcessById(processId);
                if (process.HasExited)
                    return true;
            }
            catch (ArgumentException)
            {
                return true;
            }

            Thread.Sleep(25);
        }

        return false;
    }

    private static void AssertNoAppContainerAllowRule(string filePath, string appContainerSid)
    {
        FileSystemSecurity security = Directory.Exists(filePath)
            ? new DirectoryInfo(filePath).GetAccessControl(AccessControlSections.Access)
            : new FileInfo(filePath).GetAccessControl(AccessControlSections.Access);
        var matchingRule = security
            .GetAccessRules(includeExplicit: true, includeInherited: true, typeof(SecurityIdentifier))
            .OfType<FileSystemAccessRule>()
            .FirstOrDefault(rule =>
                rule.AccessControlType == AccessControlType.Allow
                && rule.IdentityReference.Value.Equals(appContainerSid, StringComparison.OrdinalIgnoreCase));
        Assert(matchingRule is null, $"Fixture unexpectedly grants access to {appContainerSid}.");
    }

    private static void Assert(bool condition, string message)
    {
        if (!condition)
            throw new HarnessAssertionException(message);
    }

    private static void AssertEqual<T>(T expected, T actual, string label)
    {
        if (!EqualityComparer<T>.Default.Equals(expected, actual))
        {
            throw new HarnessAssertionException(
                $"{label} mismatch. Expected {expected}; observed {actual}.");
        }
    }

    private sealed class PrivateFileFixture : IDisposable
    {
        private PrivateFileFixture(string directoryPath, string filePath)
        {
            DirectoryPath = directoryPath;
            FilePath = filePath;
        }

        internal string DirectoryPath { get; }
        internal string FilePath { get; }

        internal static PrivateFileFixture Create()
        {
            var directoryPath = Path.Combine(
                Path.GetTempPath(),
                "vool-broker-security-harness",
                Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(directoryPath);

            try
            {
                var directory = new DirectoryInfo(directoryPath);
                var userSid = WindowsIdentity.GetCurrent().User
                    ?? throw new HarnessAssertionException("The test process has no Windows user SID.");
                var directorySecurity = NewDirectorySecurity();
                AddFullControl(directorySecurity, userSid, inherit: true);
                AddFullControl(
                    directorySecurity,
                    new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                    inherit: true);
                AddFullControl(
                    directorySecurity,
                    new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
                    inherit: true);
                directory.SetAccessControl(directorySecurity);

                var filePath = Path.Combine(directoryPath, "outside-appcontainer.txt");
                File.WriteAllText(filePath, "non-appcontainer fixture\n");
                var fileSecurity = NewFileSecurity();
                AddFullControl(fileSecurity, userSid, inherit: false);
                AddFullControl(
                    fileSecurity,
                    new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                    inherit: false);
                AddFullControl(
                    fileSecurity,
                    new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
                    inherit: false);
                new FileInfo(filePath).SetAccessControl(fileSecurity);

                AssertEqual(
                    "non-appcontainer fixture\n",
                    File.ReadAllText(filePath),
                    "Fixture host readback");
                return new PrivateFileFixture(directoryPath, filePath);
            }
            catch
            {
                DeleteDirectory(directoryPath);
                throw;
            }
        }

        public void Dispose() => DeleteDirectory(DirectoryPath);

        private static DirectorySecurity NewDirectorySecurity()
        {
            var security = new DirectorySecurity();
            security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
            return security;
        }

        private static FileSecurity NewFileSecurity()
        {
            var security = new FileSecurity();
            security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);
            return security;
        }

        private static void AddFullControl(
            FileSystemSecurity security,
            SecurityIdentifier sid,
            bool inherit)
        {
            security.AddAccessRule(new FileSystemAccessRule(
                sid,
                FileSystemRights.FullControl,
                inherit
                    ? InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit
                    : InheritanceFlags.None,
                PropagationFlags.None,
                AccessControlType.Allow));
        }

        private static void DeleteDirectory(string directoryPath)
        {
            if (Directory.Exists(directoryPath))
                Directory.Delete(directoryPath, recursive: true);
        }
    }

    private sealed class HarnessAssertionException : Exception
    {
        internal HarnessAssertionException(string message) : base(message)
        {
        }
    }

    private const uint HandleFlagInherit = 1;

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetHandleInformation(IntPtr handle, out uint flags);
}
