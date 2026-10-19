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
    internal const uint PipeAccessDuplex = 0x00000003;
    internal const uint FileFlagOverlapped = 0x40000000;
    internal const uint FileFlagFirstPipeInstance = 0x00080000;
    internal const uint PipeTypeByte = 0;
    internal const uint PipeReadModeByte = 0;
    internal const uint PipeWait = 0;
    internal const uint PipeRejectRemoteClients = 0x00000008;
    internal const int ErrorPipeConnected = 535;
    internal const uint ProcessQueryLimitedInformation = 0x1000;

    [StructLayout(LayoutKind.Sequential)]
    internal struct SecurityAttributes
    {
        public int Length;
        public nint SecurityDescriptor;
        [MarshalAs(UnmanagedType.Bool)] public bool InheritHandle;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern SafeFileHandle CreateNamedPipeW(string name, uint openMode, uint pipeMode,
        uint maxInstances, uint outBufferSize, uint inBufferSize, uint defaultTimeout,
        ref SecurityAttributes securityAttributes);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool ConnectNamedPipe(SafeFileHandle pipe, nint overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetNamedPipeClientProcessId(SafeFileHandle pipe, out uint clientProcessId);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern SafeFileHandle OpenProcess(uint access, bool inheritHandle, uint processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetProcessTimes(SafeFileHandle process, out long creation, out long exit,
        out long kernel, out long user);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool ConvertStringSecurityDescriptorToSecurityDescriptorW(string sddl,
        uint revision, out nint descriptor, out uint size);

    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool LocalFree(nint memory);

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
