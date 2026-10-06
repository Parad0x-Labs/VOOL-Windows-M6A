using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text;
using System.Text.Json;
using Vool.WindowsBroker;

internal static class Program
{
    private const int SecurityMandatoryLowRid = 0x1000;
    private const long SeChangeNotifyPrivilegeLuid = 23;
    private const int RepeatedLaunchCount = 20;
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

        string? brokerFixtureRoot = null;
        try
        {
            var brokerPath = ParseBrokerPath(args);
            var installedImagePath = ParseInstalledImagePath(args);
            var support = WindowsExecutionBroker.GetSupportReport();
            Console.WriteLine($"Broker: {brokerPath}");
            Console.WriteLine($"Support: {JsonSerializer.Serialize(support)}");
            using (var currentProcess = Process.GetCurrentProcess())
                Console.WriteLine($"Launch context: session={currentProcess.SessionId}; userInteractive={Environment.UserInteractive}");
            ReportLaunchObjects();

            if (!support.Supported)
            {
                Console.Error.WriteLine($"[FAIL] WindowsExecutionBroker is unsupported: {support.Reason}");
                return 2;
            }

            brokerFixtureRoot = Path.Combine(Path.GetTempPath(), "vool-broker-controlled-worker", Guid.NewGuid().ToString("N"));
            Directory.CreateDirectory(brokerFixtureRoot);
            var workspaceBrokerPath = CreateControlledBrokerFixture(brokerPath, brokerFixtureRoot);

            var tests = new List<(string Name, Func<Task> Run)>
            {
                ("AppContainer token has no capabilities and low integrity", () =>
                    VerifyAppContainerTokenAsync(brokerPath)),
                ("Worker is console-detached with piped stdout and stderr", () =>
                    VerifyDetachedConsoleAsync(brokerPath)),
                ("Execution gate refuses unrestricted, admin-enabled or privileged tokens", () =>
                {
                    VerifyGateRejectsUnrestrictedSnapshots();
                    return Task.CompletedTask;
                }),
                ("Restricted-token gate holds across repeated launches", () =>
                    VerifyRestrictedTokenStableAcrossLaunchesAsync(brokerPath)),
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
                ("Scratch cleanup waits for a released sharing lock", () =>
                    VerifyScratchSharingCleanupAsync(releaseLock: true)),
                ("Scratch cleanup refuses a persistent sharing lock", () =>
                    VerifyScratchSharingCleanupAsync(releaseLock: false)),
                ("System32 image launches directly without scratch staging", () =>
                    VerifySystemImageLaunchAsync()),
                ("Child process remains contained by the Job Object", () =>
                    VerifyChildJobContainmentAsync(brokerPath)),
                ("Broker kills a long-lived descendant before returning", () =>
                    VerifyDescendantTerminationAsync(brokerPath)),
                ("Plugin broker removes process, ACL, scratch, and AppContainer residue", () =>
                    VerifyPluginResidueCleanupAsync()),
                ("Workspace parent grant cannot mutate unrelated same-parent canaries", () =>
                    VerifyWorkspaceAuthorityAsync(workspaceBrokerPath)),
                ("Workspace write broker enforces exact effect and cleans confinement", () =>
                    VerifyWorkspaceWriteAsync(workspaceBrokerPath)),
                ("Plugin cleanup preserves legacy DACL control metadata", () =>
                    VerifyPluginResidueCleanupAsync(legacyDacl: true)),
                ("Workspace cleanup preserves legacy DACL control metadata", () =>
                    VerifyWorkspaceWriteAsync(workspaceBrokerPath, legacyDacl: true)),
                ("Native DACL primitive preserves legacy metadata and handle binding", () =>
                {
                    VerifyNativeDaclPrimitive();
                    return Task.CompletedTask;
                }),
                ("Temporary grant cleanup preserves unrelated ACL edits", () =>
                {
                    VerifyGrantCleanupPreservesForeignAcl();
                    return Task.CompletedTask;
                }),
                ("Partial grant application restores existing child ACLs", () =>
                {
                    VerifyGrantFailureAndDuplicateCleanup(injectFailure: true);
                    return Task.CompletedTask;
                }),
                ("Duplicate-path grants restore children and remove new-file grants", () =>
                {
                    VerifyGrantFailureAndDuplicateCleanup(injectFailure: false);
                    return Task.CompletedTask;
                }),
                ("Bound DACL restoration refuses replacement-path identity drift", () =>
                {
                    VerifyBoundDaclReplacementRefusal();
                    return Task.CompletedTask;
                }),
                ("Bound file DACL reads match filesystem views under legacy parents", () =>
                {
                    VerifyDaclReadContract();
                    return Task.CompletedTask;
                }),
                ("Unprotected explicit legacy grants refuse before mutation", () =>
                {
                    VerifyLegacyExplicitGrantRefusal();
                    return Task.CompletedTask;
                }),
                ("Native DACL writer refuses a handle without WRITE_DAC", () =>
                {
                    VerifyDaclWriterAccessDenied();
                    return Task.CompletedTask;
                }),
                ("Workspace refuses foreign target ACL drift before commit", () =>
                    VerifyWorkspaceAclDriftAsync(workspaceBrokerPath, afterCommit: false)),
                ("Workspace preserves foreign target ACL drift after commit", () =>
                    VerifyWorkspaceAclDriftAsync(workspaceBrokerPath, afterCommit: true)),
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
        finally
        {
            if (brokerFixtureRoot is not null) DeleteDirectoryTree(brokerFixtureRoot);
        }
    }

    private static string CreateControlledBrokerFixture(string sourcePath, string root)
    {
        PrepareControlledDirectory(root);
        PathSafety.RejectReparseAncestors(sourcePath);
        var destination = Path.Combine(root, "vool-broker.exe");
        byte[] sourceHash;
        using (var source = new FileStream(sourcePath, FileMode.Open, FileAccess.Read, FileShare.Read))
        {
            sourceHash = SHA256.HashData(source);
            source.Position = 0;
            // Create new bytes under this fixture's DACL; do not copy a source
            // security descriptor or change the checkout/publish directory.
            using (var output = new FileStream(destination, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                source.CopyTo(output);
            source.Position = 0;
            Assert(sourceHash.SequenceEqual(SHA256.HashData(source)), "Broker source bytes changed during fixture preparation.");
        }
        using (var copy = File.OpenRead(destination))
            Assert(sourceHash.SequenceEqual(SHA256.HashData(copy)), "Controlled broker fixture is not byte-identical.");
        using var held = WindowsDacl.Capture(destination);
        ReportDaclContext("controlled-worker", held);
        Assert(held.HasUnambiguousFileEncoding(), "Controlled worker DACL encoding is ambiguous.");
        Console.WriteLine($"[WORKER-FIXTURE] source={sourcePath}; copy={destination}; sha256={Convert.ToHexString(sourceHash)}");
        return destination;
    }

    private static void PrepareControlledDirectory(string root)
    {
        PathSafety.RejectReparseAncestors(root);
        Assert(!Directory.EnumerateFileSystemEntries(root).Any(), "Controlled ACL setup requires an empty disposable directory.");
        using var before = WindowsDacl.Capture(root);
        var expected = new RawSecurityDescriptor(before.OriginalAcl);
        foreach (GenericAce ace in expected.DiscretionaryAcl!) ace.AceFlags &= ~AceFlags.Inherited;
        var directory = new DirectoryInfo(root);
        var security = directory.GetAccessControl(AccessControlSections.Access);
        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: true);
        directory.SetAccessControl(security);
        before.RequireBoundPath();
        var observed = before.Read();
        Assert((observed.ControlFlags & ControlFlags.DiscretionaryAclProtected) != 0,
            "Controlled directory did not retain inheritance protection.");
        expected.SetFlags(observed.ControlFlags);
        AssertEqual(expected.GetSddlForm(AccessControlSections.Access), observed.GetSddlForm(AccessControlSections.Access),
            "Controlled fixture retained ACE rights and order");
        AssertEqual(before.Original.Owner?.Value, observed.Owner?.Value, "Controlled fixture owner");
        ReportDaclContext("controlled-directory", before);
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
        Assert(!result.Token.AdministratorsGroupEnabled, "The child token has BUILTIN\\Administrators enabled.");
        Assert(
            result.Token.EnabledPrivilegeLuids.All(luid => luid == SeChangeNotifyPrivilegeLuid),
            "The child token has an enabled privilege beyond SeChangeNotifyPrivilege: "
            + string.Join(",", result.Token.EnabledPrivilegeLuids));
    }

    private static async Task VerifyDetachedConsoleAsync(string brokerPath)
    {
        var result = await RunProbeAsync(brokerPath, TimeSpan.FromSeconds(10), "--inspect-console")
            .ConfigureAwait(false);
        var probe = RequireSuccessfulProbe(result, "detached console");
        AssertEqual(0u, probe.GetProperty("consoleCodePage").GetUInt32(), "Child console code page");
        AssertEqual(3u, probe.GetProperty("stdoutType").GetUInt32(), "Child stdout FILE_TYPE_PIPE");
        AssertEqual(3u, probe.GetProperty("stderrType").GetUInt32(), "Child stderr FILE_TYPE_PIPE");
        TokenInspector.RequireUntrustedExecutionToken(result.Token, result.Token.AppContainerSid);
    }

    // CI runs 3 and 4 refused the same --inspect launch intermittently (some cases passed,
    // others failed, with identical arguments). One pass per case cannot prove the marker
    // is stable, so repeat the launch and require every child to satisfy the full gate.
    private static async Task VerifyRestrictedTokenStableAcrossLaunchesAsync(string brokerPath)
    {
        for (var attempt = 1; attempt <= RepeatedLaunchCount; attempt++)
        {
            var result = await RunProbeAsync(brokerPath, TimeSpan.FromSeconds(10), "--inspect")
                .ConfigureAwait(false);
            _ = RequireSuccessfulProbe(result, $"Repeated restricted launch {attempt}");
            Assert(result.Token.HasRestrictions, $"Launch {attempt} child token has no restricted-token marker.");
            Assert(!result.Token.AdministratorsGroupEnabled, $"Launch {attempt} child token has Administrators enabled.");
            Assert(
                result.Token.EnabledPrivilegeLuids.All(luid => luid == SeChangeNotifyPrivilegeLuid),
                $"Launch {attempt} child token has an extra enabled privilege.");
        }
    }

    private static void VerifyGateRejectsUnrestrictedSnapshots()
    {
        var valid = new TokenSecuritySnapshot(
            IsAppContainer: true,
            HasRestrictions: true,
            IntegrityLevelRid: (int)SecurityMandatoryLowRid,
            IntegrityLevel: "Low",
            UserSid: "S-1-5-21-1-1-1-1001",
            AppContainerSid: "S-1-15-2-1",
            RestrictedSidCount: 0,
            Capabilities: Array.Empty<string>(),
            EnabledPrivilegeLuids: new[] { SeChangeNotifyPrivilegeLuid },
            AdministratorsGroupEnabled: false);
        TokenInspector.RequireUntrustedExecutionToken(valid, valid.AppContainerSid);

        AssertGateRefuses(valid with { HasRestrictions = false }, "restricted-token marker");
        AssertGateRefuses(valid with { AdministratorsGroupEnabled = true }, "Administrators");
        AssertGateRefuses(
            valid with { EnabledPrivilegeLuids = new[] { SeChangeNotifyPrivilegeLuid, 20L } },
            "enabled privilege");
    }

    private static void AssertGateRefuses(TokenSecuritySnapshot snapshot, string expectedReason)
    {
        try
        {
            TokenInspector.RequireUntrustedExecutionToken(snapshot, snapshot.AppContainerSid);
        }
        catch (BrokerSecurityException exception) when (exception.Message.Contains(
            expectedReason,
            StringComparison.Ordinal))
        {
            return;
        }

        throw new HarnessAssertionException($"The execution gate accepted a token lacking: {expectedReason}.");
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

    private static async Task VerifyScratchSharingCleanupAsync(bool releaseLock)
    {
        using var appContainer = AppContainerSession.Create();
        using var scratch = ScratchWorkspace.Create(appContainer.Sid, TokenInspector.CurrentUserSid());
        var path = Path.Combine(scratch.PayloadPath, "sharing-lock.txt");
        File.WriteAllText(path, "owned cleanup fixture");
        using var held = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        var release = releaseLock ? Task.Run(async () =>
        {
            await Task.Delay(200).ConfigureAwait(false);
            held.Dispose();
        }) : Task.CompletedTask;
        try
        {
            var refused = false;
            try
            {
                scratch.Dispose();
            }
            catch (IOException exception) when ((exception.HResult & 0xFFFF) == 32)
            {
                refused = true;
            }
            if (releaseLock)
            {
                Assert(!refused && !Directory.Exists(scratch.Path),
                    "Released sharing lock must allow complete scratch cleanup.");
            }
            else
            {
                Assert(refused && File.Exists(path),
                    "Persistent sharing lock must report failure and preserve the locked file.");
            }
        }
        finally
        {
            await release.ConfigureAwait(false);
            held.Dispose();
            // Only this fixture's known file and newly created empty directories.
            PathSafety.RejectReparseAncestors(scratch.Path);
            if (File.Exists(path)) File.Delete(path);
            if (Directory.Exists(scratch.PayloadPath)) Directory.Delete(scratch.PayloadPath);
            if (Directory.Exists(scratch.Path)) Directory.Delete(scratch.Path);
        }
    }

    private static async Task VerifySystemImageLaunchAsync()
    {
        var imagePath = Path.Combine(Environment.SystemDirectory, "whoami.exe");
        Assert(File.Exists(imagePath), $"System32 test image is missing: {imagePath}");

        var help = await RunDirectImageAsync(imagePath, "/?").ConfigureAwait(false);
        Console.WriteLine($"[SYSTEM32-CONTROL] {JsonSerializer.Serialize(help)}");
        var result = await RunDirectImageAsync(imagePath, "/user").ConfigureAwait(false);
        Console.WriteLine($"[SYSTEM32-IDENTITY] {JsonSerializer.Serialize(result)}");
        Assert(!help.TimedOut && help.ExitCode == 0 && !string.IsNullOrWhiteSpace(help.Stdout),
            $"Direct System32 help control failed: {help.ExitCode}; {help.Stdout}; {help.Stderr}");
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
        Console.WriteLine($"[DIRECT-IMAGE] image={imagePath}; args={JsonSerializer.Serialize(arguments)}; "
            + $"pid={process.ProcessId}; jobOwned={process.JobOwned}; token={JsonSerializer.Serialize(process.TokenSecurity)}");
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

    private static async Task VerifyPluginResidueCleanupAsync(bool legacyDacl = false)
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
        var payloadHashes = CaptureFileHashes(pluginRoot);
        var broker = new WindowsExecutionBroker();
        try
        {
            if (legacyDacl)
            {
                SetLegacyFixtureDacl(root, pluginRoot, directory: true);
                SetLegacyFixtureDacl(root, scratchRoot, directory: true);
            }
            var pluginAcl = DirectoryAcl(pluginRoot);
            var scratchAcl = DirectoryAcl(scratchRoot);
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

    private static async Task VerifyWorkspaceWriteAsync(string brokerPath, bool legacyDacl = false)
    {
        var root = Path.Combine(
            Path.GetTempPath(),
            "vool-broker-workspace-write-harness",
            Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        if (!legacyDacl) PrepareControlledDirectory(root);
        var target = Path.Combine(root, "approved.txt");
        var replaceTarget = Path.Combine(root, "replace.txt");
        var createTarget = Path.Combine(root, "create.txt");
        var maskTarget = Path.Combine(root, "mask.txt");
        var disposableParent = Path.GetDirectoryName(root)
            ?? throw new HarnessAssertionException("Workspace harness root has no parent.");
        var outsideFile = Path.Combine(disposableParent, $"{Path.GetFileName(root)}-outside.txt");
        var outsideDirectory = Path.Combine(disposableParent, $"{Path.GetFileName(root)}-outside-dir");
        File.WriteAllText(target, "before\n", Encoding.UTF8);
        File.WriteAllText(replaceTarget, "old\n", Encoding.UTF8);
        try
        {
            if (legacyDacl)
            {
                SetLegacyFixtureDacl(root, root, directory: true);
                SetLegacyFixtureDacl(root, target, directory: false);
                SetLegacyFixtureDacl(root, replaceTarget, directory: false);
            }
            var rootAcl = DirectoryAcl(root);
            var targetAcl = FileAcl(target);
            var replaceTargetAcl = FileAcl(replaceTarget);
            var request = BuildWorkspaceWriteRequest(
                root,
                target,
                "after\n",
                approvalId: "workspace-approval-1",
                proposalId: "workspace-proposal-1",
                operationId: "workspace-operation-1",
                runtimeId: "workspace-runtime-1");
            var result = await WorkspaceWriteBroker.ExecuteAsync(
                    request,
                    workerExecutablePath: brokerPath,
                    beforeTargetAclVerificationTestHook: () =>
                        Console.WriteLine($"[ACL] workspace legacy={legacyDacl}; before={targetAcl}; after={FileAcl(target)}"))
                .ConfigureAwait(false);
            var evidence = result.Confinement
                ?? throw new HarnessAssertionException($"Workspace write returned no confinement evidence: status={result.Status}; error={result.Error}");
            Assert(result.Ok && result.Status == "executed", $"Workspace write failed: {result.Error}");
            AssertEqual("after\n", File.ReadAllText(target, Encoding.UTF8), "Workspace write content");
            AssertEqual(result.ContentSha256, result.FinalSha256, "Workspace write final hash");
            AssertEqual("workspace_write_v1", evidence.Effect, "Workspace write effect");
            AssertEqual("workspace.write_file", evidence.Tool, "Workspace write tool");
            AssertEqual("consumed", evidence.ApprovalStatus, "Workspace approval status");
            AssertEqual("none", evidence.NetworkPolicy, "Workspace network policy");
            Assert(evidence.Verified, "Workspace write receipt did not record post-write verification.");
            Assert(evidence.JobOwned, "Workspace worker was not owned by its Job Object.");
            Assert(evidence.Token.IsAppContainer, "Workspace worker was not AppContainer-confined.");
            Assert(evidence.Token.Capabilities.Count == 0, "Workspace worker unexpectedly had capabilities.");
            Assert(ProcessHasExited(evidence.ProcessId), "Workspace worker survived broker return.");
            AssertNoWorkspaceStagingResidue(root, "Workspace write");
            var stagingGrants = evidence.FilesystemGrants
                .Where(grant => grant.Rights == (int)WorkspaceWriteBroker.WorkspaceParentStagingRights
                    && Path.GetDirectoryName(Path.GetFullPath(grant.Path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)) is { } parent
                    && string.Equals(
                        parent.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                        Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                        StringComparison.OrdinalIgnoreCase))
                .ToArray();
            AssertEqual(1, stagingGrants.Length, "Workspace staging grant count");
            var stagingGrant = stagingGrants.Single();
            AssertEqual(
                (int)WorkspaceWriteBroker.WorkspaceParentStagingRights,
                stagingGrant.Rights,
                "Workspace staging access mask");
            AssertEqual("ContainerInherit, ObjectInherit", stagingGrant.Inheritance,
                "Workspace staging inheritance");
            Assert(
                evidence.FilesystemGrants.All(grant => !string.Equals(
                    Path.GetFullPath(grant.Path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                    Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                    StringComparison.OrdinalIgnoreCase)),
                "Workspace write retained an AppContainer grant on the target parent.");
            AssertNoAppContainerAllowRule(target, evidence.AppContainerSid);
            AssertEqual(targetAcl, FileAcl(target), "Workspace target ACL after success");
            AssertEqual(rootAcl, DirectoryAcl(root), "Workspace target-parent ACL after success");
            AssertAppContainerProfileDeleted(evidence.AppContainerName, "workspace success");

            var cleanupFailureTarget = Path.Combine(root, "cleanup-failure.txt");
            File.WriteAllText(cleanupFailureTarget, "cleanup-before\n", Encoding.UTF8);
            var cleanupFailureAcl = FileAcl(cleanupFailureTarget);
            string cleanupFailureIdentity;
            using (var original = WindowsDacl.Capture(cleanupFailureTarget))
                cleanupFailureIdentity = original.Identity;
            var cleanupFailureOwner = FixtureOwner(cleanupFailureTarget, directory: false);
            var cleanupHookCalled = false;
            var cleanupFailureRequest = BuildWorkspaceWriteRequest(
                root,
                cleanupFailureTarget,
                "cleanup-after\n",
                approvalId: "workspace-approval-cleanup-failure",
                proposalId: "workspace-proposal-cleanup-failure",
                operationId: "workspace-operation-cleanup-failure",
                runtimeId: "workspace-runtime-1");
            var cleanupFailure = await WorkspaceWriteBroker.ExecuteAsync(
                    cleanupFailureRequest,
                    workerExecutablePath: brokerPath,
                    beforeTargetAclVerificationTestHook: () =>
                    {
                        cleanupHookCalled = true;
                        throw new BrokerSecurityException("injected cleanup verification failure");
                    })
                .ConfigureAwait(false);
            Console.WriteLine($"[CLEANUP-FAILURE] legacy={legacyDacl}; status={cleanupFailure.Status}; error={cleanupFailure.Error}");
            if (legacyDacl)
            {
                Assert(!cleanupFailure.Ok && cleanupFailure.Status == "refused",
                    "Ambiguous legacy target was not refused before mutation.");
                Assert(cleanupFailure.Error.Contains("ambiguous legacy DACL", StringComparison.Ordinal),
                    "Legacy refusal did not identify the encoding boundary.");
                Assert(cleanupFailure.Confinement is null && !cleanupFailure.AtomicInstalled && !cleanupHookCalled,
                    "Legacy refusal reached worker or post-mutation verification.");
                AssertEqual("cleanup-before\n", File.ReadAllText(cleanupFailureTarget, Encoding.UTF8),
                    "Refused legacy target content");
                using (var unchanged = WindowsDacl.Capture(cleanupFailureTarget))
                    AssertEqual(cleanupFailureIdentity, unchanged.Identity, "Refused legacy target identity");
                AssertEqual(cleanupFailureRequest.PriorSha256, cleanupFailure.FinalSha256, "Refused legacy final hash");
                AssertEqual(cleanupFailureOwner, FixtureOwner(cleanupFailureTarget, directory: false),
                    "Refused legacy target owner");
            }
            else
            {
                var cleanupFailureEvidence = cleanupFailure.Confinement
                    ?? throw new HarnessAssertionException("Cleanup-failure case returned no confinement evidence.");
                Assert(!cleanupFailure.Ok && cleanupFailure.Status == "security_cleanup_failed",
                    "A cleanup verification failure was reported as ordinary success.");
                Assert(cleanupHookCalled && cleanupFailure.Error.Contains("Mutation occurred", StringComparison.OrdinalIgnoreCase),
                    "Cleanup-failure result did not distinguish mutation from remediation failure.");
                AssertEqual("cleanup-after\n", File.ReadAllText(cleanupFailureTarget, Encoding.UTF8),
                    "Cleanup-failure target content");
                AssertNoAppContainerAllowRule(cleanupFailureTarget, cleanupFailureEvidence.AppContainerSid);
                Console.WriteLine($"[ACL-GRANT-FREE] legacy={legacyDacl}; cleanup-failure target has no operation allow ACE");
                AssertEqual(cleanupFailureAcl, FileAcl(cleanupFailureTarget),
                    "Cleanup-failure target ACL after remediation failure");
                Assert(ProcessHasExited(cleanupFailureEvidence.ProcessId),
                    "Cleanup-failure worker survived broker return.");
                AssertAppContainerProfileDeleted(cleanupFailureEvidence.AppContainerName, "workspace cleanup failure");
            }
            AssertEqual(cleanupFailureAcl, FileAcl(cleanupFailureTarget),
                "Cleanup-failure or refusal target ACL");
            AssertNoWorkspaceStagingResidue(root, "Cleanup-failure case");
            AssertEqual(rootAcl, DirectoryAcl(root),
                "Workspace target-parent ACL after cleanup-failure case");

            var createRequest = BuildWorkspaceWriteRequest(
                root,
                createTarget,
                "created\n",
                approvalId: "workspace-approval-create",
                proposalId: "workspace-proposal-create",
                operationId: "workspace-operation-create",
                runtimeId: "workspace-runtime-1");
            var created = await WorkspaceWriteBroker.ExecuteAsync(
                    createRequest,
                    workerExecutablePath: brokerPath)
                .ConfigureAwait(false);
            var createdEvidence = created.Confinement
                ?? throw new HarnessAssertionException("Workspace create returned no confinement evidence.");
            Assert(created.Ok && created.Status == "executed", $"Workspace create failed: {created.Error}");
            AssertEqual("created\n", File.ReadAllText(createTarget, Encoding.UTF8), "Workspace create content");
            AssertEqual(createRequest.ContentSha256, created.FinalSha256, "Workspace create final hash");
            Assert(ProcessHasExited(createdEvidence.ProcessId), "Workspace create worker survived broker return.");
            AssertNoAppContainerAllowRule(createTarget, createdEvidence.AppContainerSid);
            AssertNoWorkspaceStagingResidue(root, "Workspace create");
            AssertEqual(rootAcl, DirectoryAcl(root), "Workspace target-parent ACL after create");
            AssertAppContainerProfileDeleted(createdEvidence.AppContainerName, "workspace create");

            File.WriteAllText(maskTarget, "mask-before\n", Encoding.UTF8);
            // Reach the access-mask test with an unambiguous legacy encoding.
            // The untouched fresh-child refusal is asserted above separately.
            if (legacyDacl) SetLegacyFixtureDacl(root, maskTarget, directory: false);
            var maskAcl = FileAcl(maskTarget);
            var missingReplacementRight = BuildWorkspaceWriteRequest(
                root,
                maskTarget,
                "mask-after\n",
                approvalId: "workspace-approval-mask",
                proposalId: "workspace-proposal-mask",
                operationId: "workspace-operation-mask",
                runtimeId: "workspace-runtime-1");
            var missingRightResult = await WorkspaceWriteBroker.ExecuteAsync(
                    missingReplacementRight,
                    workerExecutablePath: brokerPath,
                    workspaceParentRightsOverride:
                        FileSystemRights.ReadAndExecute)
                .ConfigureAwait(false);
            var missingRightEvidence = missingRightResult.Confinement
                ?? throw new HarnessAssertionException("Missing access-mask case returned no confinement evidence.");
            Assert(!missingRightResult.Ok && missingRightResult.Status == "refused",
                "Workspace replacement silently broadened a missing parent access mask.");
            AssertEqual("mask-before\n", File.ReadAllText(maskTarget, Encoding.UTF8),
                "Missing access-mask target");
            AssertEqual(maskAcl, FileAcl(maskTarget), "Missing access-mask target ACL");
            Assert(ProcessHasExited(missingRightEvidence.ProcessId),
                "Missing access-mask worker survived broker return.");
            var missingRightStagingGrants = missingRightEvidence.FilesystemGrants
                .Where(grant => grant.Rights == (int)FileSystemRights.ReadAndExecute
                    && Path.GetDirectoryName(Path.GetFullPath(grant.Path).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar)) is { } parent
                    && string.Equals(
                        parent.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                        Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar),
                        StringComparison.OrdinalIgnoreCase))
                .ToArray();
            AssertEqual(1, missingRightStagingGrants.Length, "Missing access-mask grant count");
            AssertEqual(
                (int)FileSystemRights.ReadAndExecute,
                missingRightStagingGrants[0].Rights,
                "Missing access-mask grant rights");
            AssertEqual(rootAcl, DirectoryAcl(root), "Workspace target-parent ACL after missing access-mask case");
            AssertAppContainerProfileDeleted(missingRightEvidence.AppContainerName, "missing access-mask case");

            var beforeGrantIdentityTarget = Path.Combine(root, "identity-before-grant.txt");
            File.WriteAllText(beforeGrantIdentityTarget, "before\n", Encoding.UTF8);
            if (legacyDacl) SetLegacyFixtureDacl(root, beforeGrantIdentityTarget, directory: false);
            string? beforeGrantReplacementAcl = null;
            string? beforeGrantReplacementOwner = null;
            var beforeGrantIdentityRequest = BuildWorkspaceWriteRequest(
                root,
                beforeGrantIdentityTarget,
                "after\n",
                approvalId: "workspace-approval-identity-before",
                proposalId: "workspace-proposal-identity-before",
                operationId: "workspace-operation-identity-before",
                runtimeId: "workspace-runtime-1");
            Exception? beforeGrantIdentityFailure = null;
            try
            {
                await WorkspaceWriteBroker.ExecuteAsync(
                        beforeGrantIdentityRequest,
                        workerExecutablePath: brokerPath,
                        beforeParentGrantTestHook: () =>
                        {
                            ReplaceWithSameContent(beforeGrantIdentityTarget);
                            beforeGrantReplacementAcl = FileAcl(beforeGrantIdentityTarget);
                            beforeGrantReplacementOwner = FixtureOwner(beforeGrantIdentityTarget, directory: false);
                        })
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                beforeGrantIdentityFailure = exception;
            }
            Assert(beforeGrantIdentityFailure is BrokerSecurityException,
                "A target identity change before parent grant was not refused.");
            AssertEqual("before\n", File.ReadAllText(beforeGrantIdentityTarget, Encoding.UTF8),
                "Pre-grant identity-change target");
            Assert(beforeGrantReplacementAcl is not null && beforeGrantReplacementOwner is not null,
                "Pre-grant identity-swap hook did not execute.");
            AssertEqual(beforeGrantReplacementAcl, FileAcl(beforeGrantIdentityTarget),
                "Pre-grant foreign replacement ACL preservation");
            AssertEqual(beforeGrantReplacementOwner, FixtureOwner(beforeGrantIdentityTarget, directory: false),
                "Pre-grant foreign replacement owner preservation");
            AssertEqual(rootAcl, DirectoryAcl(root), "Parent ACL after pre-grant identity refusal");
            AssertNoWorkspaceStagingResidue(root, "Pre-grant identity refusal");

            var beforeReplacementIdentityTarget = Path.Combine(root, "identity-before-replacement.txt");
            File.WriteAllText(beforeReplacementIdentityTarget, "before\n", Encoding.UTF8);
            if (legacyDacl) SetLegacyFixtureDacl(root, beforeReplacementIdentityTarget, directory: false);
            string? beforeReplacementAcl = null;
            string? beforeReplacementOwner = null;
            var beforeReplacementIdentityRequest = BuildWorkspaceWriteRequest(
                root,
                beforeReplacementIdentityTarget,
                "after\n",
                approvalId: "workspace-approval-identity-replacement",
                proposalId: "workspace-proposal-identity-replacement",
                operationId: "workspace-operation-identity-replacement",
                runtimeId: "workspace-runtime-1");
            Exception? beforeReplacementIdentityFailure = null;
            try
            {
                await WorkspaceWriteBroker.ExecuteAsync(
                        beforeReplacementIdentityRequest,
                        workerExecutablePath: brokerPath,
                        afterParentGrantTestHook: () =>
                        {
                            ReplaceWithSameContent(beforeReplacementIdentityTarget);
                            beforeReplacementAcl = FileAcl(beforeReplacementIdentityTarget);
                            beforeReplacementOwner = FixtureOwner(beforeReplacementIdentityTarget, directory: false);
                        })
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                beforeReplacementIdentityFailure = exception;
            }
            Assert(beforeReplacementIdentityFailure is BrokerSecurityException,
                "A target identity change before worker replacement was not refused.");
            AssertEqual("before\n", File.ReadAllText(beforeReplacementIdentityTarget, Encoding.UTF8),
                "Pre-replacement identity-change target");
            Assert(beforeReplacementAcl is not null && beforeReplacementOwner is not null,
                "Pre-replacement identity-swap hook did not execute.");
            AssertEqual(beforeReplacementAcl, FileAcl(beforeReplacementIdentityTarget),
                "Pre-replacement foreign ACL preservation");
            AssertEqual(beforeReplacementOwner, FixtureOwner(beforeReplacementIdentityTarget, directory: false),
                "Pre-replacement foreign owner preservation");
            AssertEqual(rootAcl, DirectoryAcl(root), "Parent ACL after pre-replacement identity refusal");
            AssertNoWorkspaceStagingResidue(root, "Pre-replacement identity refusal");

            var swappedContent = BuildWorkspaceWriteRequest(
                root,
                target,
                "swapped\n",
                approvalId: "workspace-approval-1",
                proposalId: "workspace-proposal-1",
                operationId: "workspace-operation-1",
                runtimeId: "workspace-runtime-1",
                approvedEffectIdentityOverride: request.ApprovedEffectIdentity);
            await AssertWorkspaceRefusedAsync(swappedContent, brokerPath, "swapped content");

            var sibling = Path.Combine(root, "sibling.txt");
            var swappedTarget = BuildWorkspaceWriteRequest(
                root,
                sibling,
                "sibling\n",
                approvalId: "workspace-approval-1",
                proposalId: "workspace-proposal-1",
                operationId: "workspace-operation-1",
                runtimeId: "workspace-runtime-1",
                approvedEffectIdentityOverride: request.ApprovedEffectIdentity);
            await AssertWorkspaceRefusedAsync(swappedTarget, brokerPath, "swapped target");

            Exception? replay = null;
            try
            {
                await WorkspaceWriteBroker.ExecuteAsync(request, workerExecutablePath: brokerPath)
                    .ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                replay = exception;
            }
            Assert(replay is BrokerSecurityException, "The exact workspace write replay was not refused.");
            AssertEqual("after\n", File.ReadAllText(target, Encoding.UTF8), "Workspace replay target");

            var mustBeAbsent = Path.Combine(root, "must-be-absent.txt");
            var absentRequest = BuildWorkspaceWriteRequest(
                root,
                mustBeAbsent,
                "absent\n",
                approvalId: "workspace-approval-absent",
                proposalId: "workspace-proposal-absent",
                operationId: "workspace-operation-absent",
                runtimeId: "workspace-runtime-1");
            File.WriteAllText(mustBeAbsent, "unexpected\n", Encoding.UTF8);
            await AssertWorkspaceRefusedAsync(absentRequest, brokerPath, "unexpected target creation");
            AssertEqual("unexpected\n", File.ReadAllText(mustBeAbsent, Encoding.UTF8), "Absent-target refusal");

            var replacement = BuildWorkspaceWriteRequest(
                root,
                replaceTarget,
                "new\n",
                tool: "workspace.replace_in_file",
                approvalId: "workspace-approval-2",
                proposalId: "workspace-proposal-2",
                operationId: "workspace-operation-2",
                runtimeId: "workspace-runtime-1");
            var replaced = await WorkspaceWriteBroker.ExecuteAsync(
                    replacement,
                    workerExecutablePath: brokerPath)
                .ConfigureAwait(false);
            var replacementEvidence = replaced.Confinement
                ?? throw new HarnessAssertionException("Workspace replacement returned no confinement evidence.");
            Assert(replaced.Ok, $"Workspace replacement failed: {replaced.Error}");
            AssertEqual("new\n", File.ReadAllText(replaceTarget, Encoding.UTF8), "Workspace replacement content");
            AssertEqual(replacement.ContentSha256, replaced.FinalSha256, "Workspace replacement final hash");
            Assert(ProcessHasExited(replacementEvidence.ProcessId),
                "Workspace replacement worker survived broker return.");
            AssertNoAppContainerAllowRule(replaceTarget, replacementEvidence.AppContainerSid);
            AssertEqual(replaceTargetAcl, FileAcl(replaceTarget), "Workspace replacement target ACL");
            AssertEqual(rootAcl, DirectoryAcl(root), "Workspace target-parent ACL after replacement");
            AssertAppContainerProfileDeleted(replacementEvidence.AppContainerName, "workspace replacement");

            var outside = outsideFile;
            var outsideRequest = BuildWorkspaceWriteRequest(
                root,
                outside,
                "outside",
                approvalId: "workspace-approval-outside",
                proposalId: "workspace-proposal-outside",
                operationId: "workspace-operation-outside",
                runtimeId: "workspace-runtime-1");
            await AssertWorkspaceRefusedAsync(outsideRequest, brokerPath, "outside workspace target");

            var reparseTarget = Path.Combine(root, "reparse", "escape.txt");
            Directory.CreateDirectory(outsideDirectory);
            Directory.CreateSymbolicLink(Path.Combine(root, "reparse"), outsideDirectory);
            try
            {
                var reparseRequest = BuildWorkspaceWriteRequest(
                    root,
                    reparseTarget,
                    "reparse",
                    approvalId: "workspace-approval-reparse",
                    proposalId: "workspace-proposal-reparse",
                    operationId: "workspace-operation-reparse",
                    runtimeId: "workspace-runtime-1");
                await AssertWorkspaceRefusedAsync(reparseRequest, brokerPath, "reparse target");
                Assert(!File.Exists(Path.Combine(outsideDirectory, "escape.txt")),
                    "Reparse refusal wrote through to the outside canary.");
            }
            finally
            {
                Directory.Delete(Path.Combine(root, "reparse"));
                Directory.Delete(outsideDirectory, recursive: true);
            }

            AssertEqual(rootAcl, DirectoryAcl(root), "Workspace target-parent ACL after all cases");
        }
        finally
        {
            DeleteDirectoryTree(root);
            if (File.Exists(outsideFile))
                File.Delete(outsideFile);
            DeleteDirectoryTree(outsideDirectory);
        }
    }

    private static async Task VerifyWorkspaceAuthorityAsync(string brokerPath)
    {
        var root = Path.Combine(
            Path.GetTempPath(),
            "vool-broker-workspace-authority-harness",
            Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        PrepareControlledDirectory(root);
        var nested = Path.Combine(root, "nested");
        Directory.CreateDirectory(nested);
        var target = Path.Combine(root, "approved-target.txt");
        var sibling = Path.Combine(root, "sibling.txt");
        var overwrite = Path.Combine(root, "overwrite-canary.txt");
        var append = Path.Combine(root, "append-canary.txt");
        var delete = Path.Combine(root, "delete-canary.txt");
        var renameSource = Path.Combine(root, "rename-source.txt");
        var renameDestination = Path.Combine(root, "rename-destination.txt");
        var replaceSource = Path.Combine(root, "replace-source.txt");
        var replaceDestination = Path.Combine(root, "replace-destination.txt");
        var truncate = Path.Combine(root, "truncate-canary.txt");
        var attributes = Path.Combine(root, "attributes-canary.txt");
        var security = Path.Combine(root, "security-canary.txt");
        var nestedCanary = Path.Combine(nested, "nested-canary.txt");
        foreach (var path in new[]
                 {
                     target,
                     sibling,
                     overwrite,
                     append,
                     delete,
                     renameSource,
                     renameDestination,
                     replaceSource,
                     replaceDestination,
                     truncate,
                     attributes,
                     security,
                     nestedCanary,
                 })
        {
            File.WriteAllText(path, $"fixture:{Path.GetFileName(path)}\n", Encoding.UTF8);
        }

        var expectedHashes = CaptureFileHashes(root);
        var rootAcl = DirectoryAcl(root);
        var securityAcl = FileAcl(security);
        var attributeState = File.GetAttributes(attributes);
        try
        {
            var request = BuildWorkspaceWriteRequest(
                root,
                target,
                "approved-after\n",
                approvalId: "workspace-approval-authority",
                proposalId: "workspace-proposal-authority",
                operationId: "workspace-operation-authority",
                runtimeId: "workspace-runtime-authority");
            var operations = new[]
            {
                new WorkspaceAuthorityProbeOperation("read", sibling),
                new WorkspaceAuthorityProbeOperation("overwrite", overwrite),
                new WorkspaceAuthorityProbeOperation("append", append),
                new WorkspaceAuthorityProbeOperation("delete", delete),
                new WorkspaceAuthorityProbeOperation("rename", renameSource, renameDestination),
                new WorkspaceAuthorityProbeOperation("replace", replaceSource, replaceDestination),
                new WorkspaceAuthorityProbeOperation("truncate", truncate),
                new WorkspaceAuthorityProbeOperation("attributes", attributes),
                new WorkspaceAuthorityProbeOperation("security", security),
                new WorkspaceAuthorityProbeOperation("overwrite", nestedCanary),
            };
            var execution = await WorkspaceWriteBroker.ProbeParentAuthorityAsync(
                    request,
                    brokerPath,
                    operations)
                .ConfigureAwait(false);
            Console.WriteLine($"[AUTHORITY] {execution.ProcessResult.Stdout.Trim()}");
            Assert(!execution.ProcessResult.TimedOut,
                "Workspace authority probe timed out.");
            AssertEqual(0, execution.ProcessResult.ExitCode,
                $"Workspace authority probe exit code. stderr={execution.ProcessResult.Stderr}");
            Assert(execution.JobOwned, "Workspace authority probe was not Job-owned.");
            Assert(execution.TokenSecurity.IsAppContainer,
                "Workspace authority probe was not AppContainer-confined.");
            AssertEqual(0, execution.TokenSecurity.Capabilities.Count,
                "Workspace authority probe unexpectedly had capabilities.");

            using var document = JsonDocument.Parse(execution.ProcessResult.Stdout);
            var authority = document.RootElement.GetProperty("workspaceAuthority");
            AssertEqual(operations.Length, authority.GetArrayLength(),
                "Workspace authority operation count");
            for (var index = 0; index < authority.GetArrayLength(); index++)
            {
                var operation = authority[index];
                AssertEqual(
                    "denied",
                    operation.GetProperty("outcome").GetString(),
                    $"Unrelated authority operation {index} result");
            }

            Assert(FilesMatch(root, expectedHashes),
                "An unrelated same-parent authority probe changed a canary.");
            AssertEqual(securityAcl, FileAcl(security),
                "Unrelated security canary ACL");
            AssertEqual(attributeState, File.GetAttributes(attributes),
                "Unrelated attributes canary state");
            AssertEqual(rootAcl, DirectoryAcl(root),
                "Workspace authority parent ACL after probe");
            AssertAppContainerProfileDeleted(execution.AppContainerName, "workspace authority probe");
        }
        finally
        {
            DeleteDirectoryTree(root);
        }
    }

    private static WorkspaceWriteRequest BuildWorkspaceWriteRequest(
        string workspaceRoot,
        string targetPath,
        string content,
        string tool = "workspace.write_file",
        string approvalId = "workspace-approval",
        string proposalId = "workspace-proposal",
        string operationId = "workspace-operation",
        string runtimeId = "workspace-runtime",
        string? approvedEffectIdentityOverride = null)
    {
        var root = Path.GetFullPath(workspaceRoot).TrimEnd(Path.DirectorySeparatorChar);
        var target = Path.GetFullPath(targetPath);
        var relative = Path.GetRelativePath(root, target).Replace('\\', '/');
        var contentBytes = Encoding.UTF8.GetBytes(content);
        var priorState = File.Exists(target) ? "file" : "absent";
        var priorHash = priorState == "file"
            ? Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(target))).ToLowerInvariant()
            : "";
        var contentHash = Convert.ToHexString(SHA256.HashData(contentBytes)).ToLowerInvariant();
        var identity = string.Join(
            "\n",
            "vool.workspace_write.v1",
            "workspace_write_v1",
            tool,
            approvalId,
            proposalId,
            operationId,
            runtimeId,
            root,
            relative,
            priorState,
            priorHash,
            contentHash,
            contentBytes.LongLength.ToString(CultureInfo.InvariantCulture));
        return new WorkspaceWriteRequest
        {
            Schema = "vool.workspace_write.v1",
            Effect = "workspace_write_v1",
            Tool = tool,
            ApprovalId = approvalId,
            ApprovalStatus = "consumed",
            ProposalId = proposalId,
            OperationId = operationId,
            RuntimeId = runtimeId,
            WorkspaceRoot = root,
            RelativeTarget = relative,
            TargetPath = target,
            PriorState = priorState,
            PriorSha256 = priorHash,
            ContentSha256 = contentHash,
            ContentLength = contentBytes.LongLength,
            ContentBase64 = Convert.ToBase64String(contentBytes),
            ApprovedEffectIdentity = approvedEffectIdentityOverride
                ?? Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(identity))).ToLowerInvariant(),
            TimeoutMilliseconds = 10_000,
        };
    }

    private static async Task AssertWorkspaceRefusedAsync(
        WorkspaceWriteRequest request,
        string brokerPath,
        string label)
    {
        Exception? observed = null;
        try
        {
            await WorkspaceWriteBroker.ExecuteAsync(request, workerExecutablePath: brokerPath)
                .ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            observed = exception;
        }

        Assert(observed is BrokerSecurityException, $"{label} was not refused: {observed}");
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

    private static void AssertNoWorkspaceStagingResidue(string root, string context)
    {
        var residue = Directory.EnumerateFileSystemEntries(
                root,
                "*.vool-write-*",
                SearchOption.TopDirectoryOnly)
            .Concat(Directory.EnumerateFiles(
                root,
                "*.vool-write-*.tmp",
                SearchOption.AllDirectories))
            .ToArray();
        Assert(residue.Length == 0,
            $"{context} left workspace staging residue: {string.Join(", ", residue)}");
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

    private static void ReplaceWithSameContent(string target)
    {
        var temporary = $"{target}.identity-swap-{Guid.NewGuid():N}.tmp";
        File.Copy(target, temporary, overwrite: false);
        try
        {
            File.Replace(temporary, target, destinationBackupFileName: null, ignoreMetadataErrors: true);
        }
        finally
        {
            if (File.Exists(temporary))
                File.Delete(temporary);
        }
    }

    private static string DirectoryAcl(string path) =>
        new DirectoryInfo(path)
            .GetAccessControl(AccessControlSections.Access)
            .GetSecurityDescriptorSddlForm(AccessControlSections.Access);

    private static string FileAcl(string path) =>
        new FileInfo(path)
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
        Assert(!string.IsNullOrWhiteSpace(result.Stdout),
            $"{context} produced no probe JSON. Exit={result.ExitCode} (0x{unchecked((uint)result.ExitCode):X8}); "
            + $"restricted={result.Token.HasRestrictions}; adminEnabled={result.Token.AdministratorsGroupEnabled}; "
            + $"Stderr: {result.Stderr}");

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

    // Borrowed USER handles: query only; never close, switch, or grant access to them.
    // This identifies inherited launch context, not effective access for the child token.
    private static void ReportLaunchObjects()
    {
        ReportUserObject("windowStation", GetProcessWindowStation());
        ReportUserObject("desktop", GetThreadDesktop(GetCurrentThreadId()));
        if (!NativeMethods.IsProcessInJob(NativeMethods.GetCurrentProcess(), IntPtr.Zero, out var inJob))
        {
            Console.WriteLine($"Launch job: queryError={Marshal.GetLastWin32Error()}");
            return;
        }
        if (!inJob)
        {
            Console.WriteLine("Launch job: inJob=False");
            return;
        }
        if (QueryInformationJobObject(IntPtr.Zero, 4, out var uiRestrictions, sizeof(uint), out _))
            Console.WriteLine($"Launch job: inJob=True; uiRestrictions=0x{uiRestrictions:X8}");
        else
            Console.WriteLine($"Launch job: inJob=True; uiQueryError={Marshal.GetLastWin32Error()}");
    }

    private static void ReportUserObject(string label, IntPtr handle)
    {
        if (handle == IntPtr.Zero)
        {
            Console.WriteLine($"Launch {label}: handleError={Marshal.GetLastWin32Error()}");
            return;
        }
        var name = new StringBuilder(256);
        var named = GetUserObjectInformationW(handle, 2, name, name.Capacity * sizeof(char), out _);
        var nameFact = named ? name.ToString() : $"unknown(error={Marshal.GetLastWin32Error()})";
        uint information = 1 | 2 | 4; // Owner, group and DACL only; no SACL privilege request.
        GetUserObjectSecurity(handle, ref information, null, 0, out var required);
        var sizeError = Marshal.GetLastWin32Error();
        if (required == 0 || required > 65536 || sizeError != NativeMethods.ErrorInsufficientBuffer)
        {
            Console.WriteLine($"Launch {label}: name={nameFact}; securitySizeError={sizeError}; bytes={required}");
            return;
        }
        var descriptor = new byte[checked((int)required)];
        if (!GetUserObjectSecurity(handle, ref information, descriptor, required, out _))
        {
            Console.WriteLine($"Launch {label}: name={nameFact}; securityReadError={Marshal.GetLastWin32Error()}");
            return;
        }
        var security = new RawSecurityDescriptor(descriptor, 0);
        Console.WriteLine($"Launch {label}: name={nameFact}; sddl={security.GetSddlForm(AccessControlSections.Owner | AccessControlSections.Group | AccessControlSections.Access)}");
    }

    // Test setup only: the obsolete writer deliberately creates the old inheritance
    // model seen on the runner. It does not propagate to children. Never use it as
    // a product cleanup fallback or change a path outside this disposable fixture.
    private static void SetLegacyFixtureDacl(string fixtureRoot, string path, bool directory)
    {
        var root = Path.GetFullPath(fixtureRoot);
        var full = Path.GetFullPath(path);
        Assert(string.Equals(root, full, StringComparison.OrdinalIgnoreCase)
            || PathSafety.IsWithin(root, full), "Legacy ACL setup escaped its fixture.");
        PathSafety.RejectReparseAncestors(full);
        var before = new RawSecurityDescriptor(directory ? DirectoryAcl(full) : FileAcl(full));
        Assert(before.DiscretionaryAcl is not null, "Legacy fixture must have a non-null DACL.");
        before.SetFlags(before.ControlFlags
            & ~(ControlFlags.DiscretionaryAclAutoInherited | ControlFlags.DiscretionaryAclAutoInheritRequired));
        var expected = before.GetSddlForm(AccessControlSections.Access);
        var descriptor = new byte[before.BinaryLength];
        before.GetBinaryForm(descriptor, 0);
        if (!SetFileSecurityW(full, 4, descriptor)) // DACL_SECURITY_INFORMATION only.
            throw NativeMethods.LastError("SetFileSecurityW for disposable legacy fixture");
        AssertEqual(expected, directory ? DirectoryAcl(full) : FileAcl(full), "Legacy fixture DACL precondition");
    }

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileSecurityW(string path, uint information, byte[] descriptor);

    // Primitive feasibility only, not a product cleanup implementation. The
    // caller would still need retained identity and foreign-ACL drift checks.
    private static void VerifyNativeDaclPrimitive()
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-dacl-primitive", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            foreach (var directory in new[] { false, true })
            foreach (var protect in new[] { false, true })
            {
                var path = Path.Combine(root, $"{(directory ? "directory" : "file")}-{protect}");
                if (directory)
                    Directory.CreateDirectory(path);
                else
                    File.WriteAllText(path, "original\n", Encoding.UTF8);
                if (protect)
                {
                    if (directory)
                    {
                        var security = new DirectoryInfo(path).GetAccessControl(AccessControlSections.Access);
                        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: true);
                        new DirectoryInfo(path).SetAccessControl(security);
                    }
                    else
                    {
                        var security = new FileInfo(path).GetAccessControl(AccessControlSections.Access);
                        security.SetAccessRuleProtection(isProtected: true, preserveInheritance: true);
                        new FileInfo(path).SetAccessControl(security);
                    }
                }
                SetLegacyFixtureDacl(root, path, directory);
                var original = directory ? DirectoryAcl(path) : FileAcl(path);
                var ownerBefore = FixtureOwner(path, directory);
                var changed = new RawSecurityDescriptor(original);
                changed.SetFlags(changed.ControlFlags | ControlFlags.DiscretionaryAclAutoInherited);
                if (directory)
                {
                    var security = new DirectorySecurity();
                    security.SetSecurityDescriptorSddlForm(
                        changed.GetSddlForm(AccessControlSections.Access), AccessControlSections.Access);
                    new DirectoryInfo(path).SetAccessControl(security);
                }
                else
                {
                    var security = new FileSecurity();
                    security.SetSecurityDescriptorSddlForm(
                        changed.GetSddlForm(AccessControlSections.Access), AccessControlSections.Access);
                    new FileInfo(path).SetAccessControl(security);
                }
                Assert(!string.Equals(original, directory ? DirectoryAcl(path) : FileAcl(path), StringComparison.Ordinal),
                    "The primitive fixture did not reproduce the control-metadata change.");
                using var handle = NativeMethods.CreateFileForSecurityW(
                    path, NativeMethods.ReadControl | NativeMethods.WriteDac,
                    NativeMethods.FileShareRead | NativeMethods.FileShareWrite | NativeMethods.FileShareDelete,
                    IntPtr.Zero, NativeMethods.OpenExisting,
                    0x02000000 | 0x00200000, IntPtr.Zero); // BACKUP_SEMANTICS | OPEN_REPARSE_POINT.
                if (handle.IsInvalid)
                    throw NativeMethods.LastError("CreateFileW for disposable DACL primitive");
                var heldPath = path + ".held";
                if (directory)
                    Directory.Move(path, heldPath);
                else
                    File.Move(path, heldPath);
                if (directory)
                    Directory.CreateDirectory(path);
                else
                    File.WriteAllText(path, "replacement canary\n", Encoding.UTF8);
                var replacementAcl = directory ? DirectoryAcl(path) : FileAcl(path);
                var descriptor = new RawSecurityDescriptor(original);
                var bytes = new byte[descriptor.BinaryLength];
                descriptor.GetBinaryForm(bytes, 0);
                var status = NtSetSecurityObject(handle, 4, bytes); // DACL only; no owner/SACL mutation.
                AssertEqual(0, status, $"Native DACL primitive NTSTATUS (directory={directory})");
                AssertEqual(original, directory ? DirectoryAcl(heldPath) : FileAcl(heldPath),
                    "Handle-bound original DACL restoration");
                AssertEqual(ownerBefore, FixtureOwner(heldPath, directory), "Original owner after DACL write");
                AssertEqual(replacementAcl, directory ? DirectoryAcl(path) : FileAcl(path),
                    "Replacement-path ACL canary");
                if (!directory)
                    AssertEqual("replacement canary\n", File.ReadAllText(path, Encoding.UTF8), "Replacement content canary");
            }
        }
        finally
        {
            DeleteDirectoryTree(root);
        }
    }

    private static string FixtureOwner(string path, bool directory) => directory
        ? new DirectoryInfo(path).GetAccessControl(AccessControlSections.Owner).GetSecurityDescriptorSddlForm(AccessControlSections.Owner)
        : new FileInfo(path).GetAccessControl(AccessControlSections.Owner).GetSecurityDescriptorSddlForm(AccessControlSections.Owner);

    [DllImport("ntdll.dll")]
    private static extern int NtSetSecurityObject(
        Microsoft.Win32.SafeHandles.SafeFileHandle handle, uint information, byte[] descriptor);

    private static void VerifyGrantCleanupPreservesForeignAcl()
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-grant-drift", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            using var container = AppContainerSession.Create("acl-test", "v1", "foreign-acl", Guid.NewGuid().ToString("N"));
            using var grants = WindowsPathGrants.Create(container.Sid, new[] { (root, false) });
            var directory = new DirectoryInfo(root);
            var foreign = new FileSystemAccessRule(new SecurityIdentifier("S-1-5-21-1-2-3-40001"),
                FileSystemRights.ReadAttributes, AccessControlType.Allow);
            var live = directory.GetAccessControl(AccessControlSections.Access);
            live.AddAccessRule(foreign);
            directory.SetAccessControl(live);
            var expected = directory.GetAccessControl(AccessControlSections.Access);
            expected.RemoveAccessRuleSpecific(new FileSystemAccessRule(container.Sid,
                FileSystemRights.ReadAndExecute, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            Exception? cleanupFailure = null;
            try { grants.Dispose(); }
            catch (BrokerSecurityException exception) { cleanupFailure = exception; }
            Assert(cleanupFailure is not null, "Foreign ACL drift was reported as exact cleanup.");
            AssertEqual(expected.GetSecurityDescriptorSddlForm(AccessControlSections.Access), DirectoryAcl(root),
                "Foreign ACL edit after temporary grant cleanup");
            AssertNoAppContainerAllowRule(root, container.Sid.Value);
        }
        finally
        {
            DeleteDirectoryTree(root);
        }
    }

    private static void VerifyGrantFailureAndDuplicateCleanup(bool injectFailure)
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-grant-failure", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        var child = Path.Combine(root, "existing.txt");
        var created = Path.Combine(root, "created.txt");
        File.WriteAllText(child, "child canary\n", Encoding.UTF8);
        var rootAcl = DirectoryAcl(root);
        var childAcl = FileAcl(child);
        try
        {
            using var container = AppContainerSession.Create("acl-test", "v1", "partial-or-duplicate", Guid.NewGuid().ToString("N"));
            if (injectFailure)
            {
                var observed = false;
                try
                {
                    using var grants = WindowsPathGrants.CreateWithRules(container.Sid,
                        new[] { new WindowsPathGrantRequest(root, FileSystemRights.ReadAndExecute,
                            InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None) },
                        afterGrantWriteTestHook: () => throw new HarnessAssertionException("injected post-grant failure"));
                }
                catch (HarnessAssertionException exception)
                {
                    observed = exception.Message == "injected post-grant failure";
                }
                Assert(observed, "Partial grant failure was not preserved.");
            }
            else
            {
                using var grants = WindowsPathGrants.CreateWithRules(container.Sid, new[]
                {
                    new WindowsPathGrantRequest(root, FileSystemRights.ReadAndExecute,
                        InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None),
                    new WindowsPathGrantRequest(root, FileSystemRights.ReadAttributes,
                        InheritanceFlags.None, PropagationFlags.None),
                });
                File.WriteAllText(created, "created canary\n", Encoding.UTF8);
                grants.Dispose();
                AssertNoAppContainerAllowRule(created, container.Sid.Value);
                AssertEqual("created canary\n", File.ReadAllText(created, Encoding.UTF8), "New-file content");
            }
            AssertEqual(rootAcl, DirectoryAcl(root), "Root ACL after partial/duplicate cleanup");
            AssertEqual(childAcl, FileAcl(child), "Existing child ACL after partial/duplicate cleanup");
            AssertEqual("child canary\n", File.ReadAllText(child, Encoding.UTF8), "Existing child content");
            AssertNoAppContainerAllowRule(root, container.Sid.Value);
            AssertNoAppContainerAllowRule(child, container.Sid.Value);
        }
        finally { DeleteDirectoryTree(root); }
    }

    private static void VerifyDaclWriterAccessDenied()
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-dacl-denial", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var file = Path.Combine(root, "canary.txt");
            File.WriteAllText(file, "writer denial canary\n", Encoding.UTF8);
            var original = FileAcl(file);
            using var handle = NativeMethods.CreateFileForSecurityW(file, NativeMethods.ReadControl,
                NativeMethods.FileShareRead | NativeMethods.FileShareWrite | NativeMethods.FileShareDelete,
                IntPtr.Zero, NativeMethods.OpenExisting, 0x00200000, IntPtr.Zero);
            if (handle.IsInvalid) throw NativeMethods.LastError("CreateFileW for DACL writer denial");
            var descriptor = new RawSecurityDescriptor(original);
            var bytes = new byte[descriptor.BinaryLength];
            descriptor.GetBinaryForm(bytes, 0);
            var status = NtSetSecurityObject(handle, 4, bytes);
            Console.WriteLine($"[ACL-WRITER-DENIAL] NTSTATUS=0x{status:X8}");
            AssertEqual(unchecked((int)0xC0000022), status, "WRITE_DAC denial NTSTATUS");
            AssertEqual(original, FileAcl(file), "ACL after denied native writer");
            AssertEqual("writer denial canary\n", File.ReadAllText(file, Encoding.UTF8), "Content after denied native writer");
        }
        finally { DeleteDirectoryTree(root); }
    }

    private static async Task VerifyWorkspaceAclDriftAsync(string brokerPath, bool afterCommit)
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-target-acl-drift", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            PrepareControlledDirectory(root);
            var target = Path.Combine(root, "target.txt");
            File.WriteAllText(target, "before\n", Encoding.UTF8);
            var rootAcl = DirectoryAcl(root);
            string? foreignAcl = null;
            void AddForeignAcl()
            {
                var file = new FileInfo(target);
                var acl = file.GetAccessControl(AccessControlSections.Access);
                acl.AddAccessRule(new FileSystemAccessRule(new SecurityIdentifier("S-1-5-21-1-2-3-40002"),
                    FileSystemRights.ReadAttributes, AccessControlType.Allow));
                file.SetAccessControl(acl);
                foreignAcl = FileAcl(target);
            }
            var request = BuildWorkspaceWriteRequest(root, target, "after\n", approvalId: "acl-drift-approval",
                proposalId: "acl-drift-proposal", operationId: Guid.NewGuid().ToString("N"), runtimeId: "acl-drift-runtime");
            var result = await WorkspaceWriteBroker.ExecuteAsync(request, workerExecutablePath: brokerPath,
                afterParentGrantTestHook: afterCommit ? null : AddForeignAcl,
                beforeTargetAclRestorationTestHook: afterCommit ? AddForeignAcl : null).ConfigureAwait(false);
            var evidence = result.Confinement ?? throw new HarnessAssertionException($"ACL drift returned no confinement evidence: status={result.Status}; error={result.Error}");
            Assert(!result.Ok && !evidence.Verified, "Foreign target ACL drift was accepted.");
            Assert(foreignAcl is not null, "Foreign ACL test hook did not execute.");
            AssertEqual(foreignAcl, FileAcl(target), "Foreign target ACL preservation");
            AssertEqual(afterCommit ? "after\n" : "before\n", File.ReadAllText(target, Encoding.UTF8), "ACL drift content boundary");
            Assert(result.Error.Contains("Foreign", StringComparison.Ordinal), "ACL drift refusal lost its cause.");
            if (afterCommit) AssertEqual("security_cleanup_failed", result.Status, "Post-commit ACL drift status");
            else AssertEqual("handler_failed", result.Status, "Pre-commit ACL drift status");
            AssertNoAppContainerAllowRule(target, evidence.AppContainerSid);
            Assert(ProcessHasExited(evidence.ProcessId), "ACL drift worker survived return.");
            AssertNoWorkspaceStagingResidue(root, "ACL drift");
            AssertEqual(rootAcl, DirectoryAcl(root), "ACL drift parent ACL");
            AssertAppContainerProfileDeleted(evidence.AppContainerName, "ACL drift");
        }
        finally { DeleteDirectoryTree(root); }
    }

    private static void VerifyDaclReadContract()
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-dacl-read", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var modernParent = Path.Combine(root, "modern-parent");
            Directory.CreateDirectory(modernParent);
            PrepareControlledDirectory(modernParent);
            var modern = Path.Combine(modernParent, "modern-child.txt");
            File.WriteAllText(modern, "modern canary\n", Encoding.UTF8);
            using (var modernHeld = WindowsDacl.Capture(modern))
            {
                ReportDaclContext("controlled-modern-parent", modernHeld);
                Assert(modernHeld.HasUnambiguousFileEncoding(), "Modern DACL encoding was refused.");
            }
            SetLegacyFixtureDacl(root, root, directory: true);
            var file = Path.Combine(root, "new-child.txt");
            File.WriteAllText(file, "read-contract canary\n", Encoding.UTF8);
            var named = FileAcl(file);
            using var held = WindowsDacl.Capture(file);
            ReportDaclContext("legacy-parent", held);
            Console.WriteLine($"[ACL-READ] named={named}; held={held.OriginalAcl}; named-after={FileAcl(file)}");
            AssertEqual(named, held.OriginalAcl, "Named versus held file DACL");
            Assert(!held.HasUnambiguousFileEncoding(), "Ambiguous legacy encoding was admitted.");
            using (var unchanged = WindowsDacl.Capture(file))
            {
                AssertEqual(held.Identity, unchanged.Identity, "Identity after encoding refusal");
                AssertEqual(held.Original.Owner?.Value, unchanged.Original.Owner?.Value,
                    "Owner after encoding refusal");
            }
            AssertEqual(named, FileAcl(file), "Named DACL after held read");
            AssertEqual("read-contract canary\n", File.ReadAllText(file, Encoding.UTF8), "Read-contract content");
        }
        finally { DeleteDirectoryTree(root); }
    }

    private static void ReportDaclContext(string label, WindowsDacl held)
    {
        var sections = AccessControlSections.Owner | AccessControlSections.Access;
        Console.WriteLine($"[ACL-CONTEXT] {label}; path={held.Path}; identity={held.Identity}; "
            + $"filesystem={held.Read().GetSddlForm(sections)}; stored={held.ReadStored().GetSddlForm(sections)}");
    }

    private static void VerifyLegacyExplicitGrantRefusal()
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-legacy-explicit-refusal", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var descriptor = new RawSecurityDescriptor(DirectoryAcl(root));
            // A distinct non-inheriting ACE remains explicit in the filesystem
            // projection. It adds no effective right: the current owner already
            // has full control in this newly created disposable fixture.
            using var current = WindowsIdentity.GetCurrent();
            var sid = current.User ?? throw new HarnessAssertionException("Fixture user SID is unavailable.");
            descriptor.DiscretionaryAcl!.InsertAce(0, new CommonAce(AceFlags.None,
                AceQualifier.AccessAllowed, (int)FileSystemRights.ReadAttributes, sid, false, null));
            descriptor.SetFlags(descriptor.ControlFlags & ~(ControlFlags.DiscretionaryAclAutoInherited
                | ControlFlags.DiscretionaryAclAutoInheritRequired | ControlFlags.DiscretionaryAclProtected));
            var bytes = new byte[descriptor.BinaryLength];
            descriptor.GetBinaryForm(bytes, 0);
            if (!SetFileSecurityW(root, 4, bytes))
                throw NativeMethods.LastError("SetFileSecurityW for unprotected explicit refusal fixture");
            AssertEqual(descriptor.GetSddlForm(AccessControlSections.Access), DirectoryAcl(root),
                "Unprotected explicit refusal precondition");
            var child = Path.Combine(root, "canary.txt");
            File.WriteAllText(child, "unchanged\n", Encoding.UTF8);
            using var before = WindowsDacl.Capture(root);
            var childAcl = FileAcl(child);
            var childOwner = FixtureOwner(child, directory: false);
            ReportDaclContext("unprotected-explicit", before);
            using var container = AppContainerSession.Create("acl-test", "v1", "legacy-explicit-refusal", Guid.NewGuid().ToString("N"));
            var refused = false;
            var wrote = false;
            try
            {
                using var grants = WindowsPathGrants.CreateWithRules(container.Sid,
                    new[] { new WindowsPathGrantRequest(root, FileSystemRights.ReadAndExecute,
                        InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit, PropagationFlags.None) },
                    afterGrantWriteTestHook: () => wrote = true);
            }
            catch (BrokerSecurityException exception)
            {
                refused = exception.Message.Contains("grant refused before mutation", StringComparison.Ordinal);
                Console.WriteLine($"[LEGACY-REFUSAL] {exception.Message}");
                Assert((exception.Message.Contains($"Path={root};", StringComparison.Ordinal)
                        || exception.Message.Contains($"Path={root}{Path.DirectorySeparatorChar};", StringComparison.Ordinal))
                    && exception.Message.Contains($"identity={before.Identity};", StringComparison.Ordinal)
                    && exception.Message.Contains($"DACL={before.OriginalAcl}", StringComparison.Ordinal),
                    "Legacy refusal omitted its exact rejected path or DACL.");
            }
            Assert(refused && !wrote, "Unprotected explicit grant did not refuse before its first write.");
            before.RequireBoundPath();
            AssertEqual(before.OriginalAcl, DirectoryAcl(root), "Refused root ACL");
            AssertEqual(before.Original.GetSddlForm(AccessControlSections.Owner), FixtureOwner(root, directory: true), "Refused root owner");
            AssertEqual(childAcl, FileAcl(child), "Refused child ACL");
            AssertEqual(childOwner, FixtureOwner(child, directory: false), "Refused child owner");
            AssertEqual("unchanged\n", File.ReadAllText(child, Encoding.UTF8), "Refused child contents");
            AssertNoAppContainerAllowRule(root, container.Sid.Value);
            AssertNoAppContainerAllowRule(child, container.Sid.Value);
            // Qualify the success-fixture setup under the reproduced legacy
            // parent, rather than assuming the host TEMP inheritance is modern.
            // Only this new empty child is configured; the refused root stays old.
            var controlled = Path.Combine(root, "controlled-child");
            Directory.CreateDirectory(controlled);
            PrepareControlledDirectory(controlled);
            var modernChild = Path.Combine(controlled, "modern.txt");
            File.WriteAllText(modernChild, "controlled canary\n", Encoding.UTF8);
            using var modernHeld = WindowsDacl.Capture(modernChild);
            ReportDaclContext("controlled-under-legacy", modernHeld);
            Assert(modernHeld.HasUnambiguousFileEncoding(), "Controlled setup under a legacy parent remained ambiguous.");
            AssertEqual(before.OriginalAcl, DirectoryAcl(root), "Legacy parent after controlled child setup");
            AssertEqual(childAcl, FileAcl(child), "Legacy canary after controlled child setup");
        }
        finally { DeleteDirectoryTree(root); }
    }

    private static void VerifyBoundDaclReplacementRefusal()
    {
        var root = Path.Combine(Path.GetTempPath(), "vool-broker-dacl-replacement", Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        try
        {
            var path = Path.Combine(root, "target.txt");
            var held = path + ".held";
            File.WriteAllText(path, "held original\n", Encoding.UTF8);
            using var custody = WindowsDacl.Capture(path);
            File.Move(path, held);
            File.WriteAllText(path, "replacement canary\n", Encoding.UTF8);
            var replacementAcl = FileAcl(path);
            var refused = false;
            try { custody.RestoreMetadata(); }
            catch (BrokerSecurityException exception) { refused = exception.Message.Contains("identity changed", StringComparison.Ordinal); }
            Assert(refused, "Replacement-path identity was not refused.");
            AssertEqual(custody.OriginalAcl, FileAcl(held), "Held original after refusal");
            AssertEqual(replacementAcl, FileAcl(path), "Replacement ACL after refusal");
            AssertEqual("replacement canary\n", File.ReadAllText(path, Encoding.UTF8), "Replacement content after refusal");
            custody.Dispose();
            var released = false;
            try { custody.Read(); }
            catch (ObjectDisposedException) { released = true; }
            Assert(released, "Disposed DACL custody remained usable.");
        }
        finally { DeleteDirectoryTree(root); }
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetProcessWindowStation();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetThreadDesktop(uint threadId);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetUserObjectInformationW(
        IntPtr handle, int index, StringBuilder information, int length, out int needed);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetUserObjectSecurity(
        IntPtr handle, ref uint information, [Out] byte[]? descriptor, uint length, out uint needed);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool QueryInformationJobObject(
        IntPtr job, int informationClass, out uint information, uint length, out uint needed);

    private const uint HandleFlagInherit = 1;

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetHandleInformation(IntPtr handle, out uint flags);
}
