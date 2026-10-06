using System.Diagnostics;
using System.Globalization;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;

namespace Vool.WindowsBroker;

internal static class Probe
{
    internal static int Run(IReadOnlyList<string> arguments)
    {
        var result = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["probe"] = true,
        };
        var returnCode = 0;
        var authorityResults = new List<Dictionary<string, object?>>();
        TokenSecuritySnapshot? tokenSecurity = null;

        try
        {
            if (!NativeMethods.IsProcessInJob(
                    NativeMethods.GetCurrentProcess(),
                    IntPtr.Zero,
                    out var inJob))
            {
                throw NativeMethods.LastError("IsProcessInJob");
            }

            using var token = OpenCurrentToken();
            result["inJob"] = inJob;
            tokenSecurity = TokenInspector.Inspect(token);
            result["token"] = tokenSecurity;
        }
        catch (Exception exception)
        {
            result["securityInspectionError"] = exception.GetType().Name + ": " + exception.Message;
            returnCode = 2;
        }

        var authorityToken = tokenSecurity
            ?? throw new BrokerSecurityException("Probe token inspection did not complete.");
        for (var index = 0; index < arguments.Count; index++)
        {
            switch (arguments[index])
            {
                case "--sleep-ms":
                    var sleepMilliseconds = ReadInt(arguments, ref index, "--sleep-ms");
                    if (sleepMilliseconds is < 0 or > 60_000)
                        throw new ArgumentOutOfRangeException(nameof(sleepMilliseconds));
                    Thread.Sleep(sleepMilliseconds);
                    result["sleptMs"] = sleepMilliseconds;
                    break;

                case "--inspect-console":
                    result["consoleCodePage"] = GetConsoleCP();
                    result["stdoutType"] = NativeMethods.GetFileType(GetStdHandle(-11));
                    result["stderrType"] = NativeMethods.GetFileType(GetStdHandle(-12));
                    break;

                case "--write-scratch":
                    var scratchName = ReadValue(arguments, ref index, "--write-scratch");
                    var scratchPath = ScratchPath(scratchName);
                    File.WriteAllText(scratchPath, "vool-broker-scratch\n", Encoding.UTF8);
                    result["scratchWrite"] = File.Exists(scratchPath);
                    result["scratchName"] = scratchName;
                    break;

                case "--file":
                    var filePath = ReadValue(arguments, ref index, "--file");
                    try
                    {
                        result["fileRead"] = File.ReadAllText(filePath).Length >= 0;
                        result["fileReadPath"] = filePath;
                        if ((bool)result["fileRead"]!)
                            returnCode = Math.Max(returnCode, 20);
                    }
                    catch (Exception exception) when (exception is UnauthorizedAccessException or IOException)
                    {
                        result["fileRead"] = false;
                        result["fileError"] = exception.GetType().Name;
                    }
                    break;

                case "--write-file":
                    var writePath = ReadValue(arguments, ref index, "--write-file");
                    try
                    {
                        using var stream = new FileStream(
                            writePath, FileMode.CreateNew, FileAccess.Write, FileShare.None);
                        stream.WriteByte(0x56);
                        result["fileWrite"] = true;
                        returnCode = Math.Max(returnCode, 24);
                    }
                    catch (Exception exception) when (exception is UnauthorizedAccessException or IOException)
                    {
                        result["fileWrite"] = false;
                        result["fileWriteError"] = exception.GetType().Name;
                    }
                    break;

                case "--socket":
                    var host = ReadValue(arguments, ref index, "--socket host");
                    var port = ReadInt(arguments, ref index, "--socket port");
                    var connected = TryConnect(host, port, out var socketError);
                    result["socketConnected"] = connected;
                    result["socketError"] = socketError;
                    if (connected)
                        returnCode = Math.Max(returnCode, 21);
                    break;

                case "--handle":
                    var handleText = ReadValue(arguments, ref index, "--handle");
                    var expectedPath = ReadValue(arguments, ref index, "--handle expected path");
                    var handlePointsToExpected = InspectHandle(handleText, expectedPath, out var handlePath);
                    result["handlePointsToExpected"] = handlePointsToExpected;
                    result["handlePath"] = handlePath;
                    if (handlePointsToExpected)
                        returnCode = Math.Max(returnCode, 22);
                    break;

                case "--spawn-child-report":
                    var childReport = ReadValue(arguments, ref index, "--spawn-child-report");
                    var childResult = SpawnJobChild(childReport);
                    result["child"] = childResult is null
                        ? null
                        : new { childResult.Value.ExitCode, childResult.Value.InJob };
                    if (childResult is null || !childResult.Value.InJob || childResult.Value.ExitCode != 0)
                        returnCode = Math.Max(returnCode, 23);
                    break;

                case "--spawn-child-sleeper":
                    var childSleep = ReadInt(arguments, ref index, "--spawn-child-sleeper");
                    if (childSleep is < 1 or > 60_000)
                        throw new ArgumentOutOfRangeException(nameof(childSleep));
                    result["childSleeperPid"] = SpawnSleeper(childSleep);
                    break;

                case "--job-child-report":
                    if (arguments.Count != 2 || index != 0)
                        throw new ArgumentException("--job-child-report requires exactly one report path.");
                    var reportPath = ReadValue(arguments, ref index, "--job-child-report");
                    WriteJobChildReport(reportPath);
                    // This child reports through the existing operation-owned
                    // file, not stdout. Do not mix a second JSON object into the
                    // parent's broker pipe or create pipes inside the sandbox.
                    return 0;

                case "--inspect":
                    break;

                case "--echo-value":
                    result["echoValue"] = ReadValue(arguments, ref index, "--echo-value");
                    break;

                case "--workspace-authority-read":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "read", authorityResults, authorityToken.AppContainerSid);
                    break;

                case "--workspace-authority-overwrite":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "overwrite", authorityResults, authorityToken.AppContainerSid);
                    break;

                case "--workspace-authority-append":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "append", authorityResults, authorityToken.AppContainerSid);
                    break;

                case "--workspace-authority-delete":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "delete", authorityResults, authorityToken.AppContainerSid);
                    break;

                case "--workspace-authority-rename":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "rename", authorityResults, authorityToken.AppContainerSid, requiresSecondaryPath: true);
                    break;

                case "--workspace-authority-replace":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "replace", authorityResults, authorityToken.AppContainerSid, requiresSecondaryPath: true);
                    break;

                case "--workspace-authority-truncate":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "truncate", authorityResults, authorityToken.AppContainerSid);
                    break;

                case "--workspace-authority-attributes":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "attributes", authorityResults, authorityToken.AppContainerSid);
                    break;

                case "--workspace-authority-security":
                    RequireAuthorityProbeToken(authorityToken);
                    RunAuthorityOperation(arguments, ref index, "security", authorityResults, authorityToken.AppContainerSid);
                    break;

                default:
                    throw new ArgumentException($"Unknown probe argument: {arguments[index]}");
            }
        }

        if (authorityResults.Count != 0)
            result["workspaceAuthority"] = authorityResults;
        Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
        return returnCode;
    }

    private static void RequireAuthorityProbeToken(TokenSecuritySnapshot snapshot)
    {
        if (!string.Equals(
                Environment.GetEnvironmentVariable("VOOL_WORKSPACE_AUTHORITY_PROBE"),
                "1",
                StringComparison.Ordinal)
            || !snapshot.IsAppContainer
            || !snapshot.HasRestrictions
            || snapshot.Capabilities.Count != 0
            || snapshot.IntegrityLevelRid > NativeMethods.SecurityMandatoryLowRid
            || string.IsNullOrWhiteSpace(snapshot.AppContainerSid))
        {
            throw new BrokerSecurityException(
                "Workspace authority probes require the broker-created restricted AppContainer token.");
        }
    }

    private static void RunAuthorityOperation(
        IReadOnlyList<string> arguments,
        ref int index,
        string operation,
        ICollection<Dictionary<string, object?>> results,
        string appContainerSid,
        bool requiresSecondaryPath = false)
    {
        var targetPath = ReadValue(arguments, ref index, $"--workspace-authority-{operation} target");
        var secondaryPath = requiresSecondaryPath
            ? ReadValue(arguments, ref index, $"--workspace-authority-{operation} secondary target")
            : null;
        results.Add(ExecuteAuthorityOperation(operation, targetPath, secondaryPath, appContainerSid));
    }

    private static Dictionary<string, object?> ExecuteAuthorityOperation(
        string operation,
        string targetPath,
        string? secondaryPath,
        string appContainerSid)
    {
        var result = new Dictionary<string, object?>(StringComparer.Ordinal)
        {
            ["operation"] = operation,
            ["target"] = targetPath,
            ["secondary"] = secondaryPath,
        };
        try
        {
            switch (operation)
            {
                case "read":
                    _ = File.ReadAllBytes(targetPath);
                    break;
                case "overwrite":
                    using (var stream = new FileStream(
                               targetPath,
                               FileMode.Open,
                               FileAccess.Write,
                               FileShare.ReadWrite | FileShare.Delete))
                    {
                        stream.Position = 0;
                        stream.WriteByte(0x58);
                        stream.Flush(flushToDisk: true);
                    }
                    break;
                case "append":
                    using (var stream = new FileStream(
                               targetPath,
                               FileMode.Open,
                               FileAccess.Write,
                               FileShare.ReadWrite | FileShare.Delete))
                    {
                        stream.Seek(0, SeekOrigin.End);
                        stream.WriteByte(0x41);
                        stream.Flush(flushToDisk: true);
                    }
                    break;
                case "delete":
                    File.Delete(targetPath);
                    if (File.Exists(targetPath))
                        throw new IOException("Delete returned without removing the target.");
                    break;
                case "rename":
                    File.Move(targetPath, secondaryPath!, overwrite: true);
                    break;
                case "replace":
                    File.Replace(targetPath, secondaryPath!, destinationBackupFileName: null, ignoreMetadataErrors: true);
                    break;
                case "truncate":
                    using (var stream = new FileStream(
                               targetPath,
                               FileMode.Open,
                               FileAccess.Write,
                               FileShare.ReadWrite | FileShare.Delete))
                    {
                        stream.SetLength(0);
                        stream.Flush(flushToDisk: true);
                    }
                    break;
                case "attributes":
                    File.SetAttributes(targetPath, File.GetAttributes(targetPath) | FileAttributes.Hidden);
                    break;
                case "security":
                    var fileInfo = new FileInfo(targetPath);
                    var security = fileInfo.GetAccessControl(AccessControlSections.Access);
                    security.AddAccessRule(new FileSystemAccessRule(
                        new SecurityIdentifier(appContainerSid),
                        FileSystemRights.Read,
                        InheritanceFlags.None,
                        PropagationFlags.None,
                        AccessControlType.Allow));
                    fileInfo.SetAccessControl(security);
                    break;
                default:
                    throw new ArgumentException($"Unknown authority operation: {operation}");
            }

            result["outcome"] = "allowed";
        }
        catch (Exception exception) when (
            exception is UnauthorizedAccessException
            or IOException
            or System.Security.SecurityException)
        {
            result["outcome"] = "denied";
            result["error"] = exception.GetType().Name;
            result["message"] = exception.Message;
        }
        catch (Exception exception)
        {
            result["outcome"] = "error";
            result["error"] = exception.GetType().Name;
            result["message"] = exception.Message;
        }

        return result;
    }

    private static SafeNativeHandle OpenCurrentToken()
    {
        if (!NativeMethods.OpenProcessToken(
                NativeMethods.GetCurrentProcess(),
                NativeMethods.TokenQuery,
                out var token))
        {
            throw NativeMethods.LastError("OpenProcessToken(probe)");
        }
        return token;
    }

    private static string ScratchPath(string name)
    {
        if (string.IsNullOrWhiteSpace(name) || Path.IsPathRooted(name))
            throw new BrokerSecurityException("Probe scratch names must be relative.");
        var scratch = Environment.GetEnvironmentVariable("VOOL_BROKER_SCRATCH");
        if (string.IsNullOrWhiteSpace(scratch))
            throw new BrokerSecurityException("VOOL_BROKER_SCRATCH is not set.");
        var root = Path.GetFullPath(scratch).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        var candidate = Path.GetFullPath(Path.Combine(root, name));
        if (!candidate.StartsWith(root, StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("Probe scratch path escaped its root.");
        return candidate;
    }

    private static bool TryConnect(string host, int port, out string? error)
    {
        using var client = new TcpClient();
        try
        {
            var task = client.ConnectAsync(host, port);
            if (!task.Wait(TimeSpan.FromMilliseconds(750)))
            {
                error = "timeout";
                return false;
            }
            task.GetAwaiter().GetResult();
            error = null;
            return client.Connected;
        }
        catch (Exception exception)
        {
            error = exception.GetBaseException().GetType().Name;
            return false;
        }
    }

    private static bool InspectHandle(string handleText, string expectedPath, out string? actualPath)
    {
        actualPath = null;
        if (!ulong.TryParse(
                handleText,
                NumberStyles.HexNumber,
                CultureInfo.InvariantCulture,
                out var numericHandle))
        {
            return false;
        }

        var handle = new IntPtr(unchecked((long)numericHandle));
        bool isOpen;
        try
        {
            isOpen = NativeMethods.GetHandleInformation(handle, out _);
        }
        catch (System.Runtime.InteropServices.SEHException)
        {
            // Some Windows handle-verifier configurations raise on an invalid handle.
            return false;
        }
        if (!isOpen || NativeMethods.GetFileType(handle) != NativeMethods.FileTypeDisk)
            return false;
        var buffer = new StringBuilder(1024);
        var length = NativeMethods.GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity)
            return false;

        actualPath = buffer.ToString();
        var normalizedActual = actualPath.StartsWith(@"\\?\", StringComparison.Ordinal)
            ? actualPath[4..]
            : actualPath;
        var normalizedExpected = Path.GetFullPath(expectedPath);
        return string.Equals(
            Path.GetFullPath(normalizedActual),
            normalizedExpected,
            StringComparison.OrdinalIgnoreCase);
    }

    private static (int ExitCode, bool InJob)? SpawnJobChild(string reportPath)
    {
        var processPath = Environment.ProcessPath;
        if (string.IsNullOrWhiteSpace(processPath))
            return null;

        var startupInfo = new NativeMethods.StartupInfoEx
        {
            StartupInfo = new NativeMethods.StartupInfo
            {
                Cb = Marshal.SizeOf<NativeMethods.StartupInfo>(),
            },
        };
        var commandLine = Marshal.StringToHGlobalUni(WindowsCommandLine.Build(
            processPath, ["probe", "--job-child-report", reportPath]));
        NativeMethods.ProcessInformation processInformation;
        try
        {
            // Inherit the current restricted token and job, but no handles or
            // console. This fixed self-probe needs neither a shell nor stdio pipes.
            if (!NativeMethods.CreateProcessW(
                    processPath, commandLine, IntPtr.Zero, IntPtr.Zero,
                    inheritHandles: false, NativeMethods.DetachedProcess,
                    IntPtr.Zero, Environment.CurrentDirectory,
                    ref startupInfo, out processInformation))
                throw NativeMethods.LastError("CreateProcessW(detached job-report child)");
        }
        finally
        {
            Marshal.FreeHGlobal(commandLine);
        }
        using var child = new SafeNativeHandle(processInformation.Process, ownsHandle: true);
        using var thread = new SafeNativeHandle(processInformation.Thread, ownsHandle: true);
        if (child.IsInvalid || thread.IsInvalid)
            throw new BrokerSecurityException("Job-report child returned invalid native handles.");
        var wait = NativeMethods.WaitForSingleObject(child, 5_000);
        if (wait == NativeMethods.WaitTimeout)
        {
            // The enclosing broker's job remains the authoritative cleanup boundary.
            return null;
        }
        if (wait != NativeMethods.WaitObject0)
            throw NativeMethods.LastError("WaitForSingleObject(job-report child)");
        if (!NativeMethods.GetExitCodeProcess(child, out var exitCode))
            throw NativeMethods.LastError("GetExitCodeProcess(job-report child)");

        if (exitCode != 0)
            throw new BrokerSecurityException($"Child job-report exited {unchecked((int)exitCode)} (0x{exitCode:X8}).");
        var inJob = false;
        if (File.Exists(reportPath))
        {
            using var document = JsonDocument.Parse(File.ReadAllText(reportPath));
            inJob = document.RootElement.TryGetProperty("inJob", out var property)
                && property.ValueKind == JsonValueKind.True;
            var childToken = document.RootElement.GetProperty("token").Deserialize<TokenSecuritySnapshot>(JsonOptions)
                ?? throw new BrokerSecurityException("Child job-report has no token evidence.");
            using var parentToken = OpenCurrentToken();
            var expected = TokenInspector.Inspect(parentToken);
            TokenInspector.RequireUntrustedExecutionToken(childToken, expected.AppContainerSid);
            if (childToken.UserSid != expected.UserSid)
                throw new BrokerSecurityException("Child job-report user SID does not match its parent.");
        }
        return (unchecked((int)exitCode), inJob);
    }

    private static int SpawnSleeper(int milliseconds)
    {
        var processPath = Environment.ProcessPath
            ?? throw new BrokerSecurityException("Probe executable path is unavailable.");
        var startInfo = new ProcessStartInfo(processPath)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        startInfo.ArgumentList.Add("probe");
        startInfo.ArgumentList.Add("--sleep-ms");
        startInfo.ArgumentList.Add(milliseconds.ToString(CultureInfo.InvariantCulture));
        using var child = Process.Start(startInfo)
            ?? throw new BrokerSecurityException("Sleeper child did not start.");
        return child.Id;
    }

    private static void WriteJobChildReport(string reportPath)
    {
        if (!NativeMethods.IsProcessInJob(
            NativeMethods.GetCurrentProcess(),
            IntPtr.Zero,
            out var inJob))
            throw NativeMethods.LastError("IsProcessInJob(job-report child)");
        using var token = OpenCurrentToken();
        var payload = new
        {
            inJob,
            token = TokenInspector.Inspect(token),
        };
        File.WriteAllText(reportPath, JsonSerializer.Serialize(payload, JsonOptions), Encoding.UTF8);
    }

    private static string ReadValue(IReadOnlyList<string> arguments, ref int index, string option)
    {
        if (++index >= arguments.Count)
            throw new ArgumentException($"Missing value for {option}.");
        return arguments[index];
    }

    private static int ReadInt(IReadOnlyList<string> arguments, ref int index, string option)
    {
        var value = ReadValue(arguments, ref index, option);
        if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed))
            throw new ArgumentException($"Invalid integer for {option}.");
        return parsed;
    }

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern uint GetConsoleCP();

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern IntPtr GetStdHandle(int standardHandle);

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = false,
    };
}
