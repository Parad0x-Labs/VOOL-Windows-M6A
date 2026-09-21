using System.ComponentModel;
using System.Runtime.InteropServices;

namespace Vool.WindowsBroker;

public sealed class WindowsExecutionBroker
{
    public static BrokerSupportReport GetSupportReport()
    {
        var isWindows = OperatingSystem.IsWindows();
        var version = Environment.OSVersion.Version;
        var is64BitOperatingSystem = Environment.Is64BitOperatingSystem;
        var is64BitProcess = Environment.Is64BitProcess;
        var supported = isWindows
            && is64BitOperatingSystem
            && is64BitProcess
            && version >= new Version(10, 0, 17763);
        var reason = supported
            ? null
            : "Requires Windows 10 1809 or newer and a win-x64 process on a 64-bit OS.";
        return new BrokerSupportReport(
            supported,
            isWindows,
            is64BitOperatingSystem,
            is64BitProcess,
            version,
            reason);
    }

    public async Task<BrokerExecutionResult> ExecuteAsync(
        BrokerExecutionRequest request,
        CancellationToken cancellationToken = default)
    {
        EnsureSupported();
        ValidateRequest(request);

        var sourcePath = Path.GetFullPath(request.ExecutablePath);
        using var source = WindowsFileOpen.OpenSourceExecutable(sourcePath);
        var userSid = TokenInspector.CurrentUserSid();
        using var appContainer = AppContainerSession.Create();

        using var scratch = ScratchWorkspace.Create(appContainer.Sid, userSid);
        var executableToRun = scratch.StageExecutable(source);
        using var job = WindowsJob.Create();
        var environment = BuildEnvironment(scratch.Path);
        using var process = NativeProcessLauncher.Start(
            appContainer.SidPointer,
            appContainer.Sid.Value,
            job,
            executableToRun,
            request.Arguments,
            scratch.Path,
            environment);

        var processResult = await process.WaitAsync(job.Handle, request.Timeout, cancellationToken)
            .ConfigureAwait(false);
        return new BrokerExecutionResult(
            processResult.ExitCode,
            processResult.TimedOut,
            processResult.Stdout,
            processResult.Stderr,
            process.ProcessId,
            StagedExecutable: true,
            process.TokenSecurity,
            job.Security,
            scratch.Security,
            new[] { "stdin", "stdout", "stderr" });
    }

    private static void EnsureSupported()
    {
        var support = GetSupportReport();
        if (!support.Supported)
            throw new BrokerSecurityException($"Refusing execution: {support.Reason}");
    }

    private static void ValidateRequest(BrokerExecutionRequest request)
    {
        if (request is null)
            throw new ArgumentNullException(nameof(request));
        if (!request.StageExecutable)
            throw new BrokerSecurityException(
                "Unstaged execution is not implemented in this bounded slice; refusing it.");
        if (string.IsNullOrWhiteSpace(request.ExecutablePath)
            || !Path.IsPathFullyQualified(request.ExecutablePath)
            || request.ExecutablePath.StartsWith(@"\\?\", StringComparison.Ordinal)
            || Path.IsPathFullyQualified(request.ExecutablePath)
                && request.ExecutablePath.StartsWith(@"\\", StringComparison.Ordinal))
        {
            throw new BrokerSecurityException(
                "The broker requires a fully qualified local non-device executable path.");
        }
        if (request.Arguments is null)
            throw new ArgumentException("Arguments must not be null.", nameof(request));
        if (request.Arguments.Any(argument => argument is null || argument.Contains('\0')))
            throw new ArgumentException("Arguments must not contain null values or NUL characters.", nameof(request));
        if (request.Timeout <= TimeSpan.Zero || request.Timeout > BrokerLimits.MaximumTimeout)
            throw new BrokerSecurityException(
                $"Timeout must be greater than zero and no more than {BrokerLimits.MaximumTimeout.TotalSeconds:0} seconds.");

        var root = Path.GetPathRoot(request.ExecutablePath);
        if (string.IsNullOrWhiteSpace(root)
            || NativeMethods.GetDriveTypeW(root) == NativeMethods.DriveTypeRemote)
        {
            throw new BrokerSecurityException("UNC and mapped/network-drive executable paths are refused.");
        }
        if (!Path.GetExtension(request.ExecutablePath).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("The bounded broker slice accepts PE .exe payloads only.");
    }

    private static IReadOnlyDictionary<string, string> BuildEnvironment(string scratchPath)
    {
        var systemRoot = Environment.GetEnvironmentVariable("SystemRoot");
        if (string.IsNullOrWhiteSpace(systemRoot))
            throw new BrokerSecurityException("SystemRoot is unavailable; refusing child launch.");

        var systemPath = Path.Combine(systemRoot, "System32") + ";" + systemRoot;
        return new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SystemRoot"] = systemRoot,
            ["WINDIR"] = systemRoot,
            ["SystemDrive"] = Path.GetPathRoot(systemRoot)?.TrimEnd(Path.DirectorySeparatorChar) ?? "C:",
            ["PATH"] = systemPath,
            ["TEMP"] = scratchPath,
            ["TMP"] = scratchPath,
            ["LOCALAPPDATA"] = scratchPath,
            ["APPDATA"] = scratchPath,
            ["USERPROFILE"] = scratchPath,
            ["HOME"] = scratchPath,
            ["VOOL_BROKER_SCRATCH"] = scratchPath,
            ["VOOL_BROKER_APP_CONTAINER"] = "1",
            ["PROCESSOR_ARCHITECTURE"] = "AMD64",
        };
    }
}
