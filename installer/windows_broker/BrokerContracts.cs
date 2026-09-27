using System.Text.Json.Serialization;

namespace Vool.WindowsBroker;

public sealed record BrokerExecutionRequest(
    string ExecutablePath,
    IReadOnlyList<string> Arguments,
    TimeSpan Timeout,
    bool StageExecutable = true);

public sealed record TokenSecuritySnapshot(
    bool IsAppContainer,
    bool HasRestrictions,
    int IntegrityLevelRid,
    string IntegrityLevel,
    string UserSid,
    string AppContainerSid,
    int RestrictedSidCount,
    IReadOnlyList<string> Capabilities);

public sealed record JobSecuritySnapshot(
    uint LimitFlags,
    uint ActiveProcessLimit,
    ulong ProcessMemoryLimitBytes,
    ulong JobMemoryLimitBytes,
    long PerProcessUserTimeLimit100Ns,
    uint CpuControlFlags,
    uint CpuRate);

public sealed record ScratchSecuritySnapshot(
    string Path,
    bool AccessRulesProtected,
    string AppContainerSid,
    IReadOnlyList<string> AllowedSids,
    bool AppContainerCanWrite);

public sealed record BrokerExecutionResult(
    int ExitCode,
    bool TimedOut,
    string Stdout,
    string Stderr,
    int ProcessId,
    bool StagedExecutable,
    TokenSecuritySnapshot Token,
    JobSecuritySnapshot Job,
    ScratchSecuritySnapshot Scratch,
    IReadOnlyList<string> InheritedHandleAllowlist);

public sealed class PluginBrokerRequest
{
    [JsonPropertyName("schema")]
    public string Schema { get; init; } = "";

    [JsonPropertyName("plugin_id")]
    public string PluginId { get; init; } = "";

    [JsonPropertyName("plugin_version")]
    public string PluginVersion { get; init; } = "";

    [JsonPropertyName("plugin_digest")]
    public string PluginDigest { get; init; } = "";

    [JsonPropertyName("operation_id")]
    public string OperationId { get; init; } = "";

    [JsonPropertyName("executable_path")]
    public string ExecutablePath { get; init; } = "";

    [JsonPropertyName("arguments")]
    public IReadOnlyList<string> Arguments { get; init; } = Array.Empty<string>();

    [JsonPropertyName("working_directory")]
    public string WorkingDirectory { get; init; } = "";

    [JsonPropertyName("plugin_root")]
    public string PluginRoot { get; init; } = "";

    [JsonPropertyName("scratch_root")]
    public string ScratchRoot { get; init; } = "";

    [JsonPropertyName("read_roots")]
    public IReadOnlyList<string> ReadRoots { get; init; } = Array.Empty<string>();

    [JsonPropertyName("writable_roots")]
    public IReadOnlyList<string> WritableRoots { get; init; } = Array.Empty<string>();

    [JsonPropertyName("allow_network")]
    public bool AllowNetwork { get; init; }

    [JsonPropertyName("timeout_ms")]
    public int TimeoutMilliseconds { get; init; }

    [JsonPropertyName("environment_allowlist")]
    public IReadOnlyList<string> EnvironmentAllowlist { get; init; } = Array.Empty<string>();

    [JsonPropertyName("environment")]
    public IReadOnlyDictionary<string, string> Environment { get; init; } =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

    [JsonPropertyName("stdin")]
    public string Stdin { get; init; } = "";
}

public sealed record FilesystemGrantEvidence(
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("access")] string Access);

public sealed record BrokerConfinementEvidence(
    [property: JsonPropertyName("platform")] string Platform,
    [property: JsonPropertyName("broker_type")] string BrokerType,
    [property: JsonPropertyName("plugin_id")] string PluginId,
    [property: JsonPropertyName("plugin_version")] string PluginVersion,
    [property: JsonPropertyName("plugin_digest")] string PluginDigest,
    [property: JsonPropertyName("operation_id")] string OperationId,
    [property: JsonPropertyName("appcontainer_name")] string AppContainerName,
    [property: JsonPropertyName("appcontainer_sid")] string AppContainerSid,
    [property: JsonPropertyName("process_id")] int ProcessId,
    [property: JsonPropertyName("job_owned")] bool JobOwned,
    [property: JsonPropertyName("executable_path")] string ExecutablePath,
    [property: JsonPropertyName("executable_sha256")] string ExecutableSha256,
    [property: JsonPropertyName("argv")] IReadOnlyList<string> Argv,
    [property: JsonPropertyName("filesystem_grants")] IReadOnlyList<FilesystemGrantEvidence> FilesystemGrants,
    [property: JsonPropertyName("network_policy")] string NetworkPolicy,
    [property: JsonPropertyName("timeout_ms")] int TimeoutMilliseconds,
    [property: JsonPropertyName("result")] string Result,
    [property: JsonPropertyName("exit_code")] int ExitCode,
    [property: JsonPropertyName("timed_out")] bool TimedOut,
    [property: JsonPropertyName("token")] TokenSecuritySnapshot Token,
    [property: JsonPropertyName("job")] JobSecuritySnapshot Job);

public sealed record PluginBrokerExecutionResult(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("child_stdout")] string ChildStdout,
    [property: JsonPropertyName("child_stderr")] string ChildStderr,
    [property: JsonPropertyName("exit_code")] int ExitCode,
    [property: JsonPropertyName("timed_out")] bool TimedOut,
    [property: JsonPropertyName("confinement")] BrokerConfinementEvidence? Confinement,
    [property: JsonPropertyName("error")] string Error = "");

public sealed record BrokerSupportReport(
    bool Supported,
    bool IsWindows,
    bool Is64BitOperatingSystem,
    bool Is64BitProcess,
    Version OperatingSystemVersion,
    string? Reason);

public sealed class BrokerSecurityException : Exception
{
    public BrokerSecurityException(string message) : base(message)
    {
    }

    public BrokerSecurityException(string message, Exception innerException)
        : base(message, innerException)
    {
    }
}

internal static class BrokerLimits
{
    internal const uint ActiveProcessLimit = 16;
    internal const ulong ProcessMemoryLimitBytes = 256UL * 1024UL * 1024UL;
    internal const ulong JobMemoryLimitBytes = 512UL * 1024UL * 1024UL;
    internal const long PerProcessUserTimeLimit100Ns = 30L * 10_000_000L;
    internal const uint CpuRate = 5_000;
    internal static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(30);
    internal static readonly TimeSpan MaximumTimeout = TimeSpan.FromMinutes(2);
    internal const int MaximumCapturedOutputBytes = 1024 * 1024;
}
