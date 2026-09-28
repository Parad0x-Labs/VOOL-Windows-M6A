using System.ComponentModel;
using System.Security.Cryptography;
using System.Runtime.InteropServices;
using System.Text;

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

    public async Task<PluginBrokerExecutionResult> ExecutePluginAsync(
        PluginBrokerRequest request,
        CancellationToken cancellationToken = default)
    {
        EnsureSupported();
        ValidatePluginRequest(request);

        var executablePath = PathSafety.RequireLocalFile(request.ExecutablePath, "plugin executable");
        using var source = WindowsFileOpen.OpenSourceExecutable(executablePath);
        var executableSha256 = Convert.ToHexString(SHA256.HashData(source)).ToLowerInvariant();
        var appContainer = AppContainerSession.Create(
            request.PluginId,
            request.PluginVersion,
            request.PluginDigest,
            request.OperationId);
        using var appContainerLifetime = appContainer;

        var grantRoots = request.ReadRoots.Select(path => (Path: path, Writable: false))
            .Concat(request.WritableRoots.Select(path => (Path: path, Writable: true)))
            .ToArray();
        using var grants = WindowsPathGrants.Create(appContainer.Sid, grantRoots);
        using var job = WindowsJob.Create();
        var environment = BuildPluginEnvironment(request);
        var argv = new[] { executablePath }.Concat(request.Arguments).ToArray();
        using var process = NativeProcessLauncher.Start(
            appContainer.SidPointer,
            appContainer.Sid.Value,
            job,
            executablePath,
            request.Arguments,
            request.WorkingDirectory,
            environment,
            request.Stdin);

        var processResult = await process.WaitAsync(job.Handle, TimeSpan.FromMilliseconds(request.TimeoutMilliseconds), cancellationToken)
            .ConfigureAwait(false);
        var result = processResult.TimedOut
            ? "timeout"
            : processResult.ExitCode == 0 ? "completed" : "handler_failed";
        var evidence = new BrokerConfinementEvidence(
            "windows",
            "appcontainer_job_object",
            request.PluginId,
            request.PluginVersion,
            request.PluginDigest,
            request.OperationId,
            appContainer.Name,
            appContainer.Sid.Value,
            process.ProcessId,
            process.JobOwned,
            executablePath,
            executableSha256,
            argv,
            grants.Evidence,
            "none",
            request.TimeoutMilliseconds,
            result,
            processResult.ExitCode,
            processResult.TimedOut,
            process.TokenSecurity,
            job.Security);
        return new PluginBrokerExecutionResult(
            !processResult.TimedOut && processResult.ExitCode == 0,
            result,
            processResult.Stdout,
            processResult.Stderr,
            processResult.ExitCode,
            processResult.TimedOut,
            evidence);
    }

    public Task<WorkspaceWriteExecutionResult> ExecuteWorkspaceWriteAsync(
        WorkspaceWriteRequest request,
        CancellationToken cancellationToken = default)
    {
        return WorkspaceWriteBroker.ExecuteAsync(request, cancellationToken);
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

    private static void ValidatePluginRequest(PluginBrokerRequest request)
    {
        if (request is null)
            throw new ArgumentNullException(nameof(request));
        if (!string.Equals(request.Schema, "vool.plugin_broker.v1", StringComparison.Ordinal))
            throw new BrokerSecurityException("Unsupported plugin broker request schema.");
        RequireIdentity(request.PluginId, "plugin_id");
        RequireIdentity(request.PluginVersion, "plugin_version");
        if (!IsSha256(request.PluginDigest))
            throw new BrokerSecurityException("plugin_digest must be a 64-character SHA-256 value.");
        RequireIdentity(request.OperationId, "operation_id");
        if (request.AllowNetwork)
            throw new BrokerSecurityException(
                "The bounded Windows plugin broker supports no-network execution only.");
        if (request.Arguments is null || request.Arguments.Any(argument => argument is null || argument.Contains('\0')))
            throw new BrokerSecurityException("Plugin arguments must not contain null values or NUL characters.");
        if (request.TimeoutMilliseconds <= 0
            || request.TimeoutMilliseconds > BrokerLimits.MaximumTimeout.TotalMilliseconds)
        {
            throw new BrokerSecurityException(
                $"Plugin timeout must be greater than zero and no more than {BrokerLimits.MaximumTimeout.TotalSeconds:0} seconds.");
        }
        if ((request.Stdin ?? "").Length > 4 * 1024 * 1024)
            throw new BrokerSecurityException("Plugin stdin exceeds the bounded request size.");

        var pluginRoot = PathSafety.RequireLocalDirectory(request.PluginRoot, "plugin root");
        var scratchRoot = PathSafety.RequireLocalDirectory(request.ScratchRoot, "plugin scratch root");
        var readRoots = (request.ReadRoots ?? Array.Empty<string>())
            .Select(path => PathSafety.RequireLocalDirectory(path, "plugin read root"))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        var writableRoots = (request.WritableRoots ?? Array.Empty<string>())
            .Select(path => PathSafety.RequireLocalDirectory(path, "plugin writable root"))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        if (!readRoots.Any(root => PathSafety.IsWithin(root, pluginRoot)))
            throw new BrokerSecurityException("The plugin root was not declared as a read root.");
        if (!readRoots.Any(root => PathSafety.IsWithin(root, scratchRoot)))
            throw new BrokerSecurityException("The scratch root was not declared as a read root.");
        foreach (var writable in writableRoots)
        {
            if (!PathSafety.IsWithin(pluginRoot, writable)
                && !PathSafety.IsWithin(scratchRoot, writable))
            {
                throw new BrokerSecurityException(
                    $"Writable root is outside the plugin or scratch scope: {writable}");
            }
        }

        var workingDirectory = PathSafety.RequireLocalDirectory(
            request.WorkingDirectory,
            "plugin working directory");
        if (!PathSafety.IsWithin(pluginRoot, workingDirectory))
            throw new BrokerSecurityException("The plugin working directory escaped the plugin root.");

        var executable = PathSafety.RequireLocalFile(request.ExecutablePath, "plugin executable");
        if (!Path.GetExtension(executable).Equals(".exe", StringComparison.OrdinalIgnoreCase))
            throw new BrokerSecurityException("The Windows plugin broker accepts PE executables only.");
        if (!PathSafety.IsWithinAny(readRoots, executable))
            throw new BrokerSecurityException("The plugin executable is outside the declared read roots.");

        foreach (var argument in request.Arguments)
        {
            if (!Path.IsPathFullyQualified(argument))
                continue;

            var absoluteArgument = Path.GetFullPath(argument);
            if (File.Exists(absoluteArgument))
                _ = PathSafety.RequireLocalFile(absoluteArgument, "plugin argument file");
            else if (Directory.Exists(absoluteArgument))
                _ = PathSafety.RequireLocalDirectory(absoluteArgument, "plugin argument directory");
            else
                PathSafety.RejectReparseAncestors(absoluteArgument);

            if (!PathSafety.IsWithinAny(readRoots.Concat(writableRoots), absoluteArgument))
            {
                throw new BrokerSecurityException(
                    $"An absolute plugin argument is outside the declared filesystem scope: {argument}");
            }
        }

        var allowlist = new HashSet<string>(
            request.EnvironmentAllowlist ?? Array.Empty<string>(),
            StringComparer.OrdinalIgnoreCase);
        foreach (var name in allowlist)
        {
            if (!IsEnvironmentName(name) || RejectedEnvironmentNames.Contains(name))
                throw new BrokerSecurityException($"Environment name is not allowed: {name}");
        }
        foreach (var pair in request.Environment ?? new Dictionary<string, string>())
        {
            if (!allowlist.Contains(pair.Key) || pair.Value is null || pair.Value.Contains('\0'))
                throw new BrokerSecurityException(
                    $"Environment value was supplied outside the explicit allowlist: {pair.Key}");
        }
    }

    private static void RequireIdentity(string value, string name)
    {
        if (string.IsNullOrWhiteSpace(value)
            || value.Length > 128
            || value.IndexOfAny(new[] { '\0', '\\', '/', ':' }) >= 0)
        {
            throw new BrokerSecurityException($"Invalid plugin broker identity field: {name}");
        }
    }

    private static bool IsSha256(string value) =>
        !string.IsNullOrWhiteSpace(value)
        && value.Length == 64
        && value.All(Uri.IsHexDigit);

    private static bool IsEnvironmentName(string value) =>
        !string.IsNullOrWhiteSpace(value)
        && value.Length <= 128
        && (char.IsLetter(value[0]) || value[0] == '_')
        && value.All(character => char.IsLetterOrDigit(character) || character == '_');

    private static readonly HashSet<string> RejectedEnvironmentNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "COMSPEC",
        "DYLD_INSERT_LIBRARIES",
        "LD_PRELOAD",
        "PATHEXT",
        "PSMODULEPATH",
        "PYTHONHOME",
        "PYTHONPATH",
    };

    private static readonly HashSet<string> ProtectedEnvironmentNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LOCALAPPDATA",
        "PATHEXT",
        "PATH",
        "PROCESSOR_ARCHITECTURE",
        "PSMODULEPATH",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
        "PYTHONUNBUFFERED",
        "PLUGIN_SCRATCH",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
        "VOOL_BROKER_APP_CONTAINER",
        "VOOL_BROKER_SCRATCH",
    };

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

    private static IReadOnlyDictionary<string, string> BuildPluginEnvironment(PluginBrokerRequest request)
    {
        var systemRoot = Environment.GetEnvironmentVariable("SystemRoot");
        if (string.IsNullOrWhiteSpace(systemRoot))
            throw new BrokerSecurityException("SystemRoot is unavailable; refusing child launch.");

        var executableDirectory = Path.GetDirectoryName(request.ExecutablePath)
            ?? throw new BrokerSecurityException("Plugin executable has no parent directory.");
        var systemPath = string.Join(
            ";",
            executableDirectory,
            Path.Combine(systemRoot, "System32"),
            systemRoot);
        var values = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["SystemRoot"] = systemRoot,
            ["WINDIR"] = systemRoot,
            ["SystemDrive"] = Path.GetPathRoot(systemRoot)?.TrimEnd(Path.DirectorySeparatorChar) ?? "C:",
            ["PATH"] = systemPath,
            ["TEMP"] = request.ScratchRoot,
            ["TMP"] = request.ScratchRoot,
            ["LOCALAPPDATA"] = request.ScratchRoot,
            ["APPDATA"] = request.ScratchRoot,
            ["USERPROFILE"] = request.ScratchRoot,
            ["HOME"] = request.ScratchRoot,
            ["PLUGIN_SCRATCH"] = request.ScratchRoot,
            ["VOOL_BROKER_SCRATCH"] = request.ScratchRoot,
            ["VOOL_BROKER_APP_CONTAINER"] = "1",
            ["PROCESSOR_ARCHITECTURE"] = "AMD64",
            ["PYTHONDONTWRITEBYTECODE"] = "1",
            ["PYTHONNOUSERSITE"] = "1",
            ["PYTHONUNBUFFERED"] = "1",
        };

        foreach (var pair in request.Environment ?? new Dictionary<string, string>())
        {
            if (!ProtectedEnvironmentNames.Contains(pair.Key))
                values[pair.Key] = pair.Value;
        }
        return values;
    }
}
