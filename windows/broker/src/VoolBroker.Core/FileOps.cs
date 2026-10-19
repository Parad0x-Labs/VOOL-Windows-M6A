using Microsoft.Win32.SafeHandles;
using System.Security.Cryptography;

namespace VoolBroker.Core;

/// <summary>
/// One change in a batch (design section 6). Expect is "any", "absent" or "sha256:" and 64 lowercase
/// hex digits. A rename may name a second root, ToRoot; every other change stays in Root.
/// </summary>
public sealed record FileChange(string Op, OpenedRoot Root, string Path, byte[]? Data = null, string? Expect = null,
    OpenedRoot? ToRoot = null, string? ToPath = null, bool Replace = false);

/// <summary>What stat reports for one object.</summary>
public sealed record FileStat(string Kind, long Size, string FileId, bool ReadOnly);

/// <summary>The read-only operations, stat and read, on an opened root (design section 6).</summary>
public static class FileOps
{
    /// <summary>The largest read: its bytes, base64-encoded, must fit in one 64 MiB reply.</summary>
    public const int MaxReadBytes = 48 * 1024 * 1024;

    /// <summary>Stats the object relativePath names under root; an empty path is the root itself.</summary>
    public static FileStat Stat(OpenedRoot root, string relativePath)
    {
        if (relativePath.Length == 0) return Describe(root.Handle);
        using var walk = HandleWalk.Open(root.Flagged, root.Handle, relativePath, WalkTarget.Any);
        return Describe(walk.FinalHandle);
    }

    /// <summary>Reads a whole file through the walk's own handle. A file over maxBytes is refused, never cut short.</summary>
    public static (byte[] Data, string Sha256) Read(OpenedRoot root, string relativePath, int maxBytes)
    {
        if (maxBytes is < 0 or > MaxReadBytes) throw new BrokerRefusal("invalid_request", $"max_bytes must be between 0 and {MaxReadBytes}.");
        using var walk = HandleWalk.Open(root.Flagged, root.Handle, relativePath, WalkTarget.File);
        var length = Native.GetStandardInfo(walk.FinalHandle).EndOfFile;
        if (length > maxBytes) throw new BrokerRefusal("too_large", $"The file is {length} bytes, over max_bytes.");
        var data = new byte[length];
        var read = 0;
        while (read < data.Length)
        {
            var count = RandomAccess.Read(walk.FinalHandle, data.AsSpan(read), read);
            if (count == 0) throw new IOException("The file ended before its length.");
            read += count;
        }
        return (data, Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant());
    }

    /// <summary>The SHA-256 of everything handle's file holds, read through that handle.</summary>
    internal static string HashOf(SafeFileHandle handle)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[1024 * 1024];
        long offset = 0;
        while (true)
        {
            var count = RandomAccess.Read(handle, buffer, offset);
            if (count == 0) break;
            hash.AppendData(buffer, 0, count);
            offset += count;
        }
        return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    }

    internal static string FileIdOf(SafeFileHandle handle) => Convert.ToHexString(Native.GetFileIdentity(handle).FileId).ToLowerInvariant();

    private static FileStat Describe(SafeFileHandle handle)
    {
        var attributes = Native.GetAttributeTag(handle).FileAttributes;
        var directory = (attributes & Native.FileAttributeDirectory) != 0;
        return new FileStat(directory ? "directory" : "file", directory ? 0 : Native.GetStandardInfo(handle).EndOfFile,
            FileIdOf(handle), (attributes & Native.FileAttributeReadOnly) != 0);
    }
}
