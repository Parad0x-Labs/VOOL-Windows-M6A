using Microsoft.Win32.SafeHandles;
using System.Security.Cryptography;
using System.Text;
using System.Runtime.InteropServices;

namespace VoolBroker.Core;

public sealed class RootRegistry : IDisposable
{
    private readonly Dictionary<string, SafeFileHandle> roots = new(StringComparer.Ordinal);
    public IReadOnlyDictionary<string, SafeFileHandle> Handles => roots;

    public string AddFlaggedRoot(string path)
    {
        if (!Path.IsPathFullyQualified(path)) throw new BrokerRefusal("invalid_root", "Root must be absolute.");
        var handle = Native.CreateFileW(path, Native.FileReadAttributes, Native.ShareRead | Native.ShareWrite,
            0, Native.OpenExisting, Native.FileFlagBackupSemantics | Native.FileFlagOpenReparsePoint, 0);
        if (handle.IsInvalid) throw new BrokerRefusal("invalid_root", new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error()).Message);
        try
        {
            var fs = new StringBuilder(32);
            var volumeRoot = Path.GetPathRoot(path) ?? throw new BrokerRefusal("invalid_root", "No volume root.");
            if (!Native.GetVolumeInformationW(volumeRoot, null, 0, out _, out _, out _, fs, (uint)fs.Capacity))
                throw new BrokerRefusal("authority_check_failed", "Cannot identify root filesystem.");
            if (!fs.ToString().Equals("NTFS", StringComparison.OrdinalIgnoreCase))
                throw new BrokerRefusal("unsupported_filesystem", fs.ToString());
            var id = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(Path.GetFullPath(path).ToUpperInvariant())));
            if (!roots.TryAdd(id, handle)) throw new BrokerRefusal("duplicate_root", "Root was already registered.");
            return id;
        }
        catch { handle.Dispose(); throw; }
    }

    public SafeFileHandle Get(string id) => roots.TryGetValue(id, out var handle)
        ? handle : throw new BrokerRefusal("unknown_root", "Root id is not registered.");

    public void Dispose() { foreach (var handle in roots.Values) handle.Dispose(); roots.Clear(); }
}
