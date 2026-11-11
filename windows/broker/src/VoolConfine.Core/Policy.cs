using System.Text.Json;

namespace VoolConfine.Core;

/// <summary>Thrown for a policy that breaks a schema rule (docs/WINDOWS_CONFINEMENT.md section 3); exit 126.</summary>
public sealed class PolicyException(string message) : Exception(message);

/// <summary>The launcher's exit codes (docs/WINDOWS_CONFINEMENT.md section 2).</summary>
public static class ExitCodes
{
    public const int NewRepository = 123;
    public const int Timeout = 124;
    public const int SetupFailed = 125;
    public const int PolicyInvalid = 126;
    /// <summary>Human-readable stderr line only; the child shares stderr, so callers never trust it.</summary>
    public const string RefusedMarker = "VOOLCONFINE-REFUSED:";
    public const string StatusSchema = "vool.windows_confinement.status.v1";
}

/// <summary>Policy schema vool.windows_confinement.v1. Parse validates every rule; unknown keys are refused.</summary>
public sealed record Policy(
    IReadOnlyList<string> WriteRoots,
    IReadOnlyList<string> ReadRoots,
    IReadOnlyList<string> JobReadRoots,
    IReadOnlyList<string> DenyWritePaths,
    IReadOnlyList<string> DenyAllPaths,
    bool DenyWriteGit,
    bool Network,
    int MaxSeconds,
    int MemoryMb,
    int MaxProcesses,
    string TempDir,
    string StatusPath)
{
    public const string Schema = "vool.windows_confinement.v1";

    /// <summary>
    /// Folders this job may stat and nothing more: one explicit, non-inherited allow of
    /// FILE_READ_ATTRIBUTES | SYNCHRONIZE on the folder itself, for that folder's own stat capability,
    /// which only a job naming it carries. The run form puts it in place before the child starts and
    /// takes it off once the job's tree is gone. No listing, no read of anything inside. The caller
    /// names the parent of a pytest job's folder here, which pytest 9.1.1 stats on every run.
    /// </summary>
    public IReadOnlyList<string> StatOnlyPaths { get; init; } = [];

    static readonly HashSet<string> Keys =
    [
        "schema", "write_roots", "read_roots", "job_read_roots", "deny_write_paths", "deny_all_paths", "deny_write_git",
        "network", "max_seconds", "memory_mb", "max_processes", "temp_dir", "status_path", "stat_only_paths",
    ];

    public static Policy Parse(string json)
    {
        JsonDocument doc;
        try { doc = JsonDocument.Parse(json); }
        catch (JsonException e) { throw new PolicyException($"policy is not JSON: {e.Message}"); }
        using (doc)
        {
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object) throw new PolicyException("policy must be a JSON object");
            foreach (var prop in root.EnumerateObject())
                if (!Keys.Contains(prop.Name)) throw new PolicyException($"unknown policy key: {prop.Name}");
            if (!root.TryGetProperty("schema", out var schema) || schema.ValueKind != JsonValueKind.String || schema.GetString() != Schema)
                throw new PolicyException($"schema must be {Schema}");
            var write = Paths(root, "write_roots", required: true);
            if (write.Count == 0) throw new PolicyException("write_roots must not be empty");
            var read = Paths(root, "read_roots", required: false);
            var jobRead = Paths(root, "job_read_roots", required: false);
            var deny = Paths(root, "deny_write_paths", required: false);
            var denyAll = Paths(root, "deny_all_paths", required: false);
            var statOnly = Paths(root, "stat_only_paths", required: false);
            foreach (var path in statOnly)
            {
                if (path.Length == 3) throw new PolicyException($"stat_only_paths entry must not be a volume root: {path}");
                if (write.Concat(read).Concat(jobRead).Any(r => Inside(path, r)))
                    throw new PolicyException($"stat_only_paths entry lies inside a root, which already reaches it: {path}");
                if (denyAll.Any(d => Inside(path, d)))
                    throw new PolicyException($"stat_only_paths entry lies inside a denied path: {path}");
            }
            if (!root.TryGetProperty("temp_dir", out var tempEl) || tempEl.ValueKind != JsonValueKind.String)
                throw new PolicyException("temp_dir is required");
            var temp = CheckPath(tempEl.GetString()!, "temp_dir");
            if (!write.Any(r => Inside(temp, r))) throw new PolicyException("temp_dir must sit inside a write root");
            if (!root.TryGetProperty("status_path", out var statusEl) || statusEl.ValueKind != JsonValueKind.String)
                throw new PolicyException("status_path is required");
            var status = CheckPath(statusEl.GetString()!, "status_path");
            if (write.Any(r => Inside(status, r))) throw new PolicyException("status_path must sit outside every write root");
            return new Policy(write, read, jobRead, deny, denyAll,
                Bool(root, "deny_write_git", true), Bool(root, "network", false),
                Positive(root, "max_seconds", 120), Positive(root, "memory_mb", 512), Positive(root, "max_processes", 64),
                temp, status) { StatOnlyPaths = statOnly };
        }
    }

    static List<string> Paths(JsonElement root, string key, bool required)
    {
        if (!root.TryGetProperty(key, out var el))
        {
            if (required) throw new PolicyException($"{key} is required");
            return [];
        }
        if (el.ValueKind != JsonValueKind.Array) throw new PolicyException($"{key} must be a list of strings");
        var list = new List<string>();
        foreach (var item in el.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String) throw new PolicyException($"{key} must be a list of strings");
            list.Add(CheckPath(item.GetString()!, key));
        }
        return list;
    }

    static bool Bool(JsonElement root, string key, bool fallback)
    {
        if (!root.TryGetProperty(key, out var el)) return fallback;
        return el.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => throw new PolicyException($"{key} must be true or false"),
        };
    }

    static int Positive(JsonElement root, string key, int fallback)
    {
        if (!root.TryGetProperty(key, out var el)) return fallback;
        if (el.ValueKind != JsonValueKind.Number || !el.TryGetInt32(out var value) || value <= 0)
            throw new PolicyException($"{key} must be a positive integer");
        return value;
    }

    /// <summary>Win32 maps these names to devices in any folder, with or without an extension.</summary>
    static readonly HashSet<string> DeviceNames = new(StringComparer.Ordinal)
    {
        "CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    };

    const string BadSegmentChars = "<>\"|?*~:/";

    /// <summary>
    /// One canonical spelling only. Win32 rewrites trailing dots and spaces, '.' segments, 8.3 names and
    /// device names before it opens a path, so the lexical containment check in Inside() is sound only when
    /// no such spelling gets in. The Python twin is sandbox.windows_confinement._check_path; keep them identical.
    /// </summary>
    public static string CheckPath(string path, string what)
    {
        if (path.StartsWith(@"\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal))
            throw new PolicyException($"{what} must be a drive-letter path, not a UNC or device path: {path}");
        if (path.Length < 3 || !char.IsAsciiLetter(path[0]) || path[1] != ':' || path[2] != '\\')
            throw new PolicyException($"{what} must be an absolute drive-letter path with backslashes: {path}");
        if (path.Length == 3) return path;
        foreach (var part in path[3..].Split('\\'))
        {
            if (part is "" or "." or "..")
                throw new PolicyException($"{what} must be resolved (no empty, '.' or '..' segment): {path}");
            if (part[^1] is '.' or ' ')
                throw new PolicyException($"{what} has a segment ending in a dot or space: {path}");
            if (part.Any(c => BadSegmentChars.Contains(c) || c < ' '))
                throw new PolicyException($"{what} has a short name, stream or reserved character: {path}");
            var dot = part.IndexOf('.');
            var stem = (dot < 0 ? part : part[..dot]).TrimEnd(' ');
            if (DeviceNames.Contains(AsciiUpper(stem)))
                throw new PolicyException($"{what} names a device: {path}");
        }
        return path;
    }

    static string AsciiUpper(string s) =>
        string.Create(s.Length, s, (span, src) =>
        {
            for (var i = 0; i < src.Length; i++) span[i] = src[i] is >= 'a' and <= 'z' ? (char)(src[i] - 32) : src[i];
        });

    /// <summary>
    /// Whether child is root or under it, case-insensitively. NTFS folds case with its own table, so this
    /// counts a match under EITHER invariant upper- or lower-casing: "inside" is the side that refuses a
    /// status_path, and a disagreement between the two sides can only refuse.
    /// </summary>
    static bool Inside(string child, string root)
    {
        static bool Under(string c, string r) => c == r || c.StartsWith(r + "\\", StringComparison.Ordinal);
        var c = child.TrimEnd('\\');
        var r = root.TrimEnd('\\');
        return Under(c.ToUpperInvariant(), r.ToUpperInvariant()) || Under(c.ToLowerInvariant(), r.ToLowerInvariant());
    }
}
