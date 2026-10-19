using Microsoft.Win32.SafeHandles;

namespace VoolBroker.Core;

/// <summary>What the last component of a walk must be.</summary>
public enum WalkTarget { Directory, File, Any }

/// <summary>Whether the last component is opened to be read or to be written.</summary>
public enum WalkAccess { Read, Write }

/// <summary>
/// The handles one walk opened, held until the caller is done. FinalHandle is the
/// object the path names; it is the root's own handle (not owned here) when the path is empty.
/// </summary>
public sealed class HandleWalkResult : IDisposable
{
    private readonly List<SafeFileHandle> handles;
    internal HandleWalkResult(List<SafeFileHandle> handles, SafeFileHandle finalHandle) { this.handles = handles; FinalHandle = finalHandle; }
    public SafeFileHandle FinalHandle { get; }
    public int HeldHandles => handles.Count;
    public void Dispose() { foreach (var handle in handles) handle.Dispose(); handles.Clear(); }
}

public static class HandleWalk
{
    public const int MaxPathUnits = 32767;
    private static readonly HashSet<string> devices = BuildDevices();
    private static readonly char[] invalidNameChars = Path.GetInvalidFileNameChars();

    // The test hooks are per async flow, so one test's hook never runs in another test's walk
    // (test classes run in parallel, and the file-change tests walk too).
    private static readonly AsyncLocal<Action<int, string>?> afterComponentOpen = new();
    private static readonly AsyncLocal<Action?> beforeOpen = new();

    /// <summary>Test hook: runs after component index is opened and checked, before the next open.</summary>
    internal static Action<int, string>? AfterComponentOpenForTests { get => afterComponentOpen.Value; set => afterComponentOpen.Value = value; }
    /// <summary>Test hook: runs before every component open, so a test can count opens.</summary>
    internal static Action? BeforeOpenForTests { get => beforeOpen.Value; set => beforeOpen.Value = value; }

    /// <summary>
    /// The lexical rules (RULES-3b P1, P2, P6, P10-P13). They run before anything is opened.
    /// </summary>
    public static string[] ValidateRelativePath(string relativePath)
    {
        ArgumentNullException.ThrowIfNull(relativePath);
        if (relativePath.Length > MaxPathUnits) throw new BrokerRefusal("path_too_long", "Path exceeds 32,767 UTF-16 units.");
        if (relativePath.Length == 0 || relativePath[0] is '\\' or '/') throw new BrokerRefusal("path_escapes_root", "Path must be relative and contain components.");
        var parts = relativePath.Split(['\\', '/']);
        foreach (var part in parts)
        {
            if (part.Length == 0 || part is "." or "..") throw new BrokerRefusal("path_escapes_root", "Empty or traversal component.");
            if (part.IndexOfAny(invalidNameChars) >= 0 || part.Any(char.IsControl) || part.EndsWith('.') || part.EndsWith(' '))
                throw new BrokerRefusal("invalid_name", "Invalid Windows path component.");
            var stem = part.Split('.')[0].TrimEnd(' ').ToUpperInvariant();
            if (devices.Contains(stem)) throw new BrokerRefusal("device_name", "DOS device names are refused.");
        }
        return parts;
    }

    /// <summary>
    /// Walks relativePath from the root's held handle, one component at a time
    /// (RULES-3b P3-P5, P7-P9, P14-P16). Every opened component stays held, without delete
    /// sharing, until the result is disposed; nothing is ever reopened by path.
    /// </summary>
    public static HandleWalkResult Open(RootEntry root, string relativePath, WalkTarget target, WalkAccess access = WalkAccess.Read)
    {
        ArgumentNullException.ThrowIfNull(root);
        return Open(root, root.Handle, relativePath, target, access);
    }

    /// <summary>
    /// The same walk, starting from start: a folder held under root (an opened root). Every
    /// component is still checked against root, the flagged folder.
    /// </summary>
    public static HandleWalkResult Open(RootEntry root, SafeFileHandle start, string relativePath, WalkTarget target, WalkAccess access = WalkAccess.Read)
    {
        ArgumentNullException.ThrowIfNull(root);
        ArgumentNullException.ThrowIfNull(start);
        var parts = ValidateRelativePath(relativePath);
        var rootPath = RootPath(root);
        var handles = new List<SafeFileHandle>();
        var parent = start;
        try
        {
            for (var index = 0; index < parts.Length; index++)
            {
                var last = index == parts.Length - 1;
                var options = !last || target == WalkTarget.Directory ? Native.FileDirectoryFile
                    : target == WalkTarget.File ? Native.FileNonDirectoryFile : 0u;
                var extra = last && access == WalkAccess.Write && target != WalkTarget.Directory
                    ? Native.FileWriteData | Native.FileWriteAttributes : 0u;
                BeforeOpenForTests?.Invoke();
                var handle = Native.OpenRelative(parent, parts[index], extra, options);
                handles.Add(handle);
                CheckComponent(root, rootPath, handle, parts[index], last);
                AfterComponentOpenForTests?.Invoke(index, parts[index]);
                parent = handle;
            }
            return new HandleWalkResult(handles, handles[^1]);
        }
        catch
        {
            foreach (var handle in handles) handle.Dispose();
            throw;
        }
    }

    /// <summary>The flagged folder's final path by volume GUID, the prefix every component must have (P16).</summary>
    internal static string RootPath(RootEntry root) => Native.GetFinalPath(root.Handle, Native.VolumeNameGuid).TrimEnd('\\');

    /// <summary>
    /// Every check one opened component must pass (P3-P5, P7-P9, P14, P16). File operations call it
    /// on the objects they open themselves, so those meet the same rules as the walk.
    /// </summary>
    internal static void CheckComponent(RootEntry root, string rootPath, SafeFileHandle handle, string component, bool last)
    {
        var attribute = Native.GetAttributeTag(handle);
        // P3, P4: a reparse point of any tag, on the way or at the end.
        if ((attribute.FileAttributes & Native.FileAttributeReparsePoint) != 0 || attribute.ReparseTag != 0)
            throw new BrokerRefusal("reparse_point", $"Component '{component}' is a reparse point.");
        var isDirectory = (attribute.FileAttributes & Native.FileAttributeDirectory) != 0;
        if (!last && !isDirectory) throw new BrokerRefusal("not_a_directory", $"Component '{component}' is not a directory.");
        // P7, P8: the object's own name, compared case-insensitively. An 8.3 alias opens the same
        // object under a different name, so it does not match.
        var objectName = Native.GetObjectName(handle);
        var separator = objectName.LastIndexOf('\\');
        var actualName = separator >= 0 ? objectName[(separator + 1)..] : objectName;
        if (!actualName.Equals(component, StringComparison.OrdinalIgnoreCase))
            throw new BrokerRefusal("short_name", $"Component '{component}' is not the object's long name.");
        // P9.
        if (isDirectory && (Native.GetCaseFlags(handle) & Native.FileCaseSensitiveSearch) != 0)
            throw new BrokerRefusal("case_sensitive_dir", $"Directory '{component}' is case-sensitive.");
        // P14.
        if (Native.GetFileIdentity(handle).VolumeSerial != root.VolumeSerial)
            throw new BrokerRefusal("path_escapes_root", $"Component '{component}' is on another volume.");
        // P5, for a read walk and a write walk alike.
        if (!isDirectory && Native.GetStandardInfo(handle).NumberOfLinks > 1)
            throw new BrokerRefusal("blocked_by_hard_link", $"File '{component}' has more than one name.");
        // P16: defence in depth; the held handles are the real check.
        var finalPath = Native.GetFinalPath(handle, Native.VolumeNameGuid).TrimEnd('\\');
        if (!finalPath.StartsWith(rootPath + "\\", StringComparison.OrdinalIgnoreCase))
            throw new BrokerRefusal("path_escapes_root", $"Component '{component}' is outside the flagged folder.");
    }

    private static HashSet<string> BuildDevices()
    {
        var result = new HashSet<string>(["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"], StringComparer.Ordinal);
        for (var i = 0; i <= 9; i++) { result.Add($"COM{i}"); result.Add($"LPT{i}"); }
        foreach (var digit in new[] { '¹', '²', '³' }) { result.Add($"COM{digit}"); result.Add($"LPT{digit}"); }
        return result;
    }
}
