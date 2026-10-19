using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;

namespace VoolBroker.Core;

internal static class Native
{
    internal const uint FileFlagBackupSemantics = 0x02000000;
    internal const uint FileFlagOpenReparsePoint = 0x00200000;
    internal const uint ShareRead = 1;
    internal const uint ShareWrite = 2;
    internal const uint OpenExisting = 3;
    internal const uint GenericRead = 0x80000000;
    internal const uint GenericWrite = 0x40000000;
    internal const uint FileReadAttributes = 0x0080;
    internal const uint FileWriteAttributes = 0x0100;
    internal const uint FileReadData = 1;
    internal const uint FileWriteData = 2;
    internal const uint Delete = 0x00010000;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern SafeFileHandle CreateFileW(string name, uint access, uint share, nint security,
        uint creation, uint flags, nint template);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetVolumeInformationW(string root, System.Text.StringBuilder? volumeName,
        uint volumeNameSize, out uint serial, out uint maxComponentLength, out uint flags,
        System.Text.StringBuilder filesystem, uint filesystemSize);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetFileInformationByHandleEx(SafeFileHandle handle, int infoClass,
        nint info, uint size);
}
