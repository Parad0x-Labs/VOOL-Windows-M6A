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
