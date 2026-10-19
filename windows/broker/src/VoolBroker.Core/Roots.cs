using Microsoft.Win32.SafeHandles;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;

namespace VoolBroker.Core;

public sealed record FlaggedRoot(string Id, string Path);

/// <summary>
/// The flagged folders, opened once at start and held for the broker's lifetime (RULES-3b F1-F10),
/// and the roots opened from them by OpenRoot.
/// </summary>
public sealed class RootRegistry : IDisposable
{
    private readonly Dictionary<string, RootEntry> flagged = new(StringComparer.Ordinal);
    private readonly Dictionary<string, HandleWalkResult> opened = new(StringComparer.Ordinal);
    private readonly object sync = new();

    public IReadOnlyCollection<FlaggedRoot> Flagged => flagged.Values.Select(value => new FlaggedRoot(value.Id, value.Path)).ToArray();

    public int OpenedCount { get { lock (sync) return opened.Count; } }

    public static void ValidatePlatform()
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763)) throw new BrokerRefusal("unsupported_windows", "Windows 10 version 1809 or later is required.");
    }

    /// <summary>Opens and checks one flagged entry; returns its id (the same id for another spelling of the same folder).</summary>
    public string AddFlaggedRoot(string path)
    {
        ValidatePlatform();
        ArgumentNullException.ThrowIfNull(path);
        if (!Path.IsPathFullyQualified(path)) throw new BrokerRefusal("invalid_flagged", "Flagged folders must be absolute.");
        if (path.StartsWith("\\\\.\\", StringComparison.Ordinal) ||
            path.StartsWith("\\\\", StringComparison.Ordinal) && !path.StartsWith("\\\\?\\", StringComparison.Ordinal) ||
            path.StartsWith("\\\\?\\UNC\\", StringComparison.OrdinalIgnoreCase))
            throw new BrokerRefusal("unsupported_filesystem", "Network and device roots are unsupported.");
        // FILE_LIST_DIRECTORY, not attribute access alone: NTFS records share access only for opens
        // with data or delete access, and this handle is what stops the folder being renamed or
        // deleted while the broker runs. No FILE_SHARE_DELETE; not inheritable (no security attributes).
        var handle = Native.CreateFileW(path, Native.FileListDirectory | Native.FileReadAttributes | Native.Synchronize,
            Native.ShareRead | Native.ShareWrite, 0, Native.OpenExisting,
            Native.FileFlagBackupSemantics | Native.FileFlagOpenReparsePoint, 0);
        if (handle.IsInvalid) { var error = Marshal.GetLastWin32Error(); handle.Dispose(); throw FlaggedOpenFailure(path, error); }
        try
        {
            var attribute = Native.GetAttributeTag(handle);
            if ((attribute.FileAttributes & Native.FileAttributeReparsePoint) != 0) throw new BrokerRefusal("reparse_point", "Flagged folder is a reparse point.");
            if ((attribute.FileAttributes & Native.FileAttributeDirectory) == 0) throw new BrokerRefusal("invalid_flagged", "Flagged entry is not a directory.");
            if ((Native.GetCaseFlags(handle) & Native.FileCaseSensitiveSearch) != 0) throw new BrokerRefusal("case_sensitive_dir", "Case-sensitive directories are refused.");
            var finalPath = Native.GetFinalPath(handle, Native.VolumeNameDos);
            if (finalPath.StartsWith("\\\\?\\UNC\\", StringComparison.OrdinalIgnoreCase) || finalPath.StartsWith("\\\\", StringComparison.Ordinal) && !finalPath.StartsWith("\\\\?\\", StringComparison.Ordinal))
                throw new BrokerRefusal("unsupported_filesystem", "Network roots are unsupported.");
            var volumePath = Native.GetVolumePath(finalPath);
            if (DisplayPath(finalPath).TrimEnd('\\').Equals(DisplayPath(volumePath).TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                throw new BrokerRefusal("invalid_flagged", "A volume root cannot be flagged.");
            var fileSystem = new StringBuilder(64);
            if (!Native.GetVolumeInformationW(volumePath, null, 0, out _, out _, out _, fileSystem, (uint)fileSystem.Capacity))
                throw new BrokerRefusal("authority_check_failed", "Cannot identify the flagged volume.");
            if (!string.Equals(fileSystem.ToString(), "NTFS", StringComparison.OrdinalIgnoreCase))
                throw new BrokerRefusal("unsupported_filesystem", $"Flagged folder is on {fileSystem}, not NTFS.");
            var identity = Native.GetFileIdentity(handle);
            var id = $"{identity.VolumeSerial:x16}{Convert.ToHexString(identity.FileId).ToLowerInvariant()}";
            if (flagged.ContainsKey(id)) { handle.Dispose(); return id; }
            flagged.Add(id, new RootEntry(id, DisplayPath(finalPath).TrimEnd('\\'), handle, identity.VolumeSerial, identity.FileId));
            return id;
        }
        catch { handle.Dispose(); throw; }
    }

    /// <summary>
    /// F9: the journal folder must not be inside a flagged folder. The deepest part of the path
    /// that exists is resolved to its final path first, so a junction, subst drive or 8.3 spelling
    /// cannot hide where it is.
    /// </summary>
    public void ValidateJournalOutsideFlagged(string journalPath)
    {
        if (!Path.IsPathFullyQualified(journalPath)) throw new BrokerRefusal("invalid_bootstrap", "journal_dir must be absolute.");
        var existing = Path.TrimEndingDirectorySeparator(Path.GetFullPath(journalPath));
        var missing = new Stack<string>();
        while (!Directory.Exists(existing))
        {
            var parent = Path.GetDirectoryName(existing);
            if (parent is null) throw new BrokerRefusal("invalid_bootstrap", "journal_dir has no existing folder above it.");
            missing.Push(Path.GetFileName(existing));
            existing = Path.TrimEndingDirectorySeparator(parent);
        }
        // Follows reparse points on purpose: this resolves where the journal really is.
        using var handle = Native.CreateFileW(existing, Native.FileReadAttributes,
            Native.ShareRead | Native.ShareWrite | Native.ShareDelete, 0, Native.OpenExisting, Native.FileFlagBackupSemantics, 0);
        if (handle.IsInvalid) throw new BrokerRefusal("invalid_bootstrap", $"journal_dir cannot be resolved: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");
        var resolved = DisplayPath(Native.GetFinalPath(handle, Native.VolumeNameDos)).TrimEnd('\\');
        foreach (var part in missing) resolved += "\\" + part;
        foreach (var entry in flagged.Values)
        {
            if (resolved.Equals(entry.Path, StringComparison.OrdinalIgnoreCase) || resolved.StartsWith(entry.Path + "\\", StringComparison.OrdinalIgnoreCase))
                throw new BrokerRefusal("journal_inside_flagged", $"Journal folder is inside flagged entry '{entry.Path}'.");
        }
    }

    public RootEntry GetEntry(string id) => flagged.TryGetValue(id, out var entry)
        ? entry : throw new BrokerRefusal("unknown_root", "Flagged root id is not registered.");

    /// <summary>
    /// Walks relativePath (empty for the flagged folder itself) and holds every
    /// folder on the way until CloseRoot. Returns an opaque random token.
    /// </summary>
    public string OpenRoot(string flaggedId, string relativePath)
    {
        var entry = GetEntry(flaggedId);
        var walk = relativePath.Length == 0
            ? new HandleWalkResult([], entry.Handle)
            : HandleWalk.Open(entry, relativePath, WalkTarget.Directory);
        try
        {
            if ((Native.GetAttributeTag(walk.FinalHandle).FileAttributes & Native.FileAttributeDirectory) == 0)
                throw new BrokerRefusal("not_a_directory", "open_root target must be a directory.");
            lock (sync)
            {
                while (true)
                {
                    var token = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant();
                    if (opened.TryAdd(token, walk)) return token;
                }
            }
        }
        catch { walk.Dispose(); throw; }
    }

    public SafeFileHandle GetOpenedRoot(string id)
    {
        lock (sync) return opened.TryGetValue(id, out var walk)
            ? walk.FinalHandle : throw new BrokerRefusal("unknown_root", "Root id is not registered.");
    }

    public void CloseRoot(string id)
    {
        HandleWalkResult? walk;
        lock (sync) if (!opened.Remove(id, out walk)) throw new BrokerRefusal("unknown_root", "Root id is not registered.");
        walk!.Dispose();
    }

    public void Dispose()
    {
        lock (sync) { foreach (var walk in opened.Values) walk.Dispose(); opened.Clear(); }
        foreach (var entry in flagged.Values) entry.Handle.Dispose();
        flagged.Clear();
    }

    private static BrokerRefusal FlaggedOpenFailure(string path, int error) => error switch
    {
        2 or 3 or 123 or 267 => new BrokerRefusal("invalid_flagged", $"Flagged entry '{path}' could not be opened: {new Win32Exception(error).Message}"),
        5 => new BrokerRefusal("access_denied", $"Flagged entry '{path}' could not be opened: {new Win32Exception(error).Message}"),
        32 => new BrokerRefusal("in_use", $"Flagged entry '{path}' is held by another program."),
        _ => new BrokerRefusal("authority_check_failed", $"Flagged entry '{path}' could not be opened: {new Win32Exception(error).Message}")
    };

    /// <summary>A drive-letter final path without its \\?\ prefix (the form Python's realpath gives).</summary>
    internal static string DisplayPath(string path) =>
        path.Length >= 7 && path.StartsWith("\\\\?\\", StringComparison.Ordinal) && char.IsAsciiLetter(path[4]) && path[5] == ':' && path[6] == '\\'
            ? path[4..] : path;
}

public sealed class RootEntry(string id, string path, SafeFileHandle handle, ulong volumeSerial, byte[] fileId)
{
    public string Id { get; } = id;
    public string Path { get; } = path;
    public SafeFileHandle Handle { get; } = handle;
    public ulong VolumeSerial { get; } = volumeSerial;
    public byte[] FileId { get; } = fileId;
}
