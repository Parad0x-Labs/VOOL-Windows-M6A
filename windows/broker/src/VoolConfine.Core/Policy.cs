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

    static readonly HashSet<string> Keys =
    [
        "schema", "write_roots", "read_roots", "deny_write_paths", "deny_all_paths", "deny_write_git",
        "network", "max_seconds", "memory_mb", "max_processes", "temp_dir", "status_path",
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
            var deny = Paths(root, "deny_write_paths", required: false);
            var denyAll = Paths(root, "deny_all_paths", required: false);
            if (!root.TryGetProperty("temp_dir", out var tempEl) || tempEl.ValueKind != JsonValueKind.String)
                throw new PolicyException("temp_dir is required");
            var temp = CheckPath(tempEl.GetString()!, "temp_dir");
            if (!write.Any(r => Inside(temp, r))) throw new PolicyException("temp_dir must sit inside a write root");
            if (!root.TryGetProperty("status_path", out var statusEl) || statusEl.ValueKind != JsonValueKind.String)
                throw new PolicyException("status_path is required");
            var status = CheckPath(statusEl.GetString()!, "status_path");
            if (write.Any(r => Inside(status, r))) throw new PolicyException("status_path must sit outside every write root");
            return new Policy(write, read, deny, denyAll,
                Bool(root, "deny_write_git", true), Bool(root, "network", false),
                Positive(root, "max_seconds", 120), Positive(root, "memory_mb", 512), Positive(root, "max_processes", 64),
                temp, status);
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

    /// <summary>Absolute drive-letter path, no UNC/device prefix, no '..' segment.</summary>
    public static string CheckPath(string path, string what)
    {
        if (path.StartsWith(@"\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal))
            throw new PolicyException($"{what} must be a drive-letter path, not a UNC or device path: {path}");
        if (path.Length < 3 || !char.IsAsciiLetter(path[0]) || path[1] != ':' || (path[2] != '\\' && path[2] != '/'))
            throw new PolicyException($"{what} must be an absolute drive-letter path: {path}");
        if (path.Split('\\', '/').Any(part => part == ".."))
            throw new PolicyException($"{what} must be resolved (no '..'): {path}");
        return path;
    }

    static bool Inside(string child, string root)
    {
        static string Norm(string p) => p.Replace('/', '\\').TrimEnd('\\').ToUpperInvariant();
        var c = Norm(child);
        var r = Norm(root);
        return c == r || c.StartsWith(r + "\\", StringComparison.Ordinal);
    }
}
