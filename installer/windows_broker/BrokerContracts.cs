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

public sealed class WorkspaceWriteRequest
{
    [JsonPropertyName("schema")]
    public string Schema { get; init; } = "";

    [JsonPropertyName("effect")]
    public string Effect { get; init; } = "";

    [JsonPropertyName("tool")]
    public string Tool { get; init; } = "";

    [JsonPropertyName("approval_id")]
    public string ApprovalId { get; init; } = "";

    [JsonPropertyName("approval_status")]
    public string ApprovalStatus { get; init; } = "";

    [JsonPropertyName("proposal_id")]
    public string ProposalId { get; init; } = "";

    [JsonPropertyName("operation_id")]
    public string OperationId { get; init; } = "";

    [JsonPropertyName("runtime_id")]
    public string RuntimeId { get; init; } = "";

    [JsonPropertyName("workspace_root")]
    public string WorkspaceRoot { get; init; } = "";

    [JsonPropertyName("relative_target")]
    public string RelativeTarget { get; init; } = "";

    [JsonPropertyName("target_path")]
    public string TargetPath { get; init; } = "";

    [JsonPropertyName("prior_state")]
    public string PriorState { get; init; } = "";

    [JsonPropertyName("prior_sha256")]
    public string PriorSha256 { get; init; } = "";

    [JsonPropertyName("content_sha256")]
    public string ContentSha256 { get; init; } = "";

    [JsonPropertyName("content_length")]
    public long ContentLength { get; init; }

    [JsonPropertyName("content_base64")]
    public string ContentBase64 { get; init; } = "";

    [JsonPropertyName("approved_effect_identity")]
    public string ApprovedEffectIdentity { get; init; } = "";

    [JsonPropertyName("timeout_ms")]
    public int TimeoutMilliseconds { get; init; } = 30_000;
}

public sealed record WorkspaceWriteWorkerResult(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("prior_state")] string PriorState,
    [property: JsonPropertyName("prior_sha256")] string PriorSha256,
    [property: JsonPropertyName("final_sha256")] string FinalSha256,
    [property: JsonPropertyName("content_length")] long ContentLength,
    [property: JsonPropertyName("atomic_installed")] bool AtomicInstalled,
    [property: JsonPropertyName("error")] string Error = "");

public sealed record FilesystemGrantEvidence(
    [property: JsonPropertyName("path")] string Path,
    [property: JsonPropertyName("access")] string Access,
    [property: JsonPropertyName("rights")] int Rights,
    [property: JsonPropertyName("inheritance")] string Inheritance,
    [property: JsonPropertyName("propagation")] string Propagation);

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

public sealed record WorkspaceWriteConfinementEvidence(
    [property: JsonPropertyName("platform")] string Platform,
    [property: JsonPropertyName("broker_type")] string BrokerType,
    [property: JsonPropertyName("effect")] string Effect,
    [property: JsonPropertyName("tool")] string Tool,
    [property: JsonPropertyName("approval_id")] string ApprovalId,
    [property: JsonPropertyName("approval_status")] string ApprovalStatus,
    [property: JsonPropertyName("proposal_id")] string ProposalId,
    [property: JsonPropertyName("operation_id")] string OperationId,
    [property: JsonPropertyName("runtime_id")] string RuntimeId,
    [property: JsonPropertyName("workspace_root")] string WorkspaceRoot,
    [property: JsonPropertyName("relative_target")] string RelativeTarget,
    [property: JsonPropertyName("target_path")] string TargetPath,
    [property: JsonPropertyName("prior_state")] string PriorState,
    [property: JsonPropertyName("prior_sha256")] string PriorSha256,
    [property: JsonPropertyName("content_sha256")] string ContentSha256,
    [property: JsonPropertyName("content_length")] long ContentLength,
    [property: JsonPropertyName("appcontainer_name")] string AppContainerName,
    [property: JsonPropertyName("appcontainer_sid")] string AppContainerSid,
    [property: JsonPropertyName("process_id")] int ProcessId,
    [property: JsonPropertyName("job_owned")] bool JobOwned,
    [property: JsonPropertyName("filesystem_grants")] IReadOnlyList<FilesystemGrantEvidence> FilesystemGrants,
    [property: JsonPropertyName("network_policy")] string NetworkPolicy,
    [property: JsonPropertyName("timeout_ms")] int TimeoutMilliseconds,
    [property: JsonPropertyName("result")] string Result,
    [property: JsonPropertyName("verified")] bool Verified,
    [property: JsonPropertyName("exit_code")] int ExitCode,
    [property: JsonPropertyName("timed_out")] bool TimedOut,
    [property: JsonPropertyName("token")] TokenSecuritySnapshot Token,
    [property: JsonPropertyName("job")] JobSecuritySnapshot Job);

public sealed record WorkspaceWriteExecutionResult(
    [property: JsonPropertyName("ok")] bool Ok,
    [property: JsonPropertyName("status")] string Status,
    [property: JsonPropertyName("prior_state")] string PriorState,
    [property: JsonPropertyName("prior_sha256")] string PriorSha256,
    [property: JsonPropertyName("final_sha256")] string FinalSha256,
    [property: JsonPropertyName("content_sha256")] string ContentSha256,
    [property: JsonPropertyName("content_length")] long ContentLength,
    [property: JsonPropertyName("atomic_installed")] bool AtomicInstalled,
    [property: JsonPropertyName("error")] string Error,
    [property: JsonPropertyName("confinement")] WorkspaceWriteConfinementEvidence? Confinement);

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
