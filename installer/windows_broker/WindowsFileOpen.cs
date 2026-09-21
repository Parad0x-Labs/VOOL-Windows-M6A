using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Vool.WindowsBroker;

// Open the source by a volume-GUID path. A DOS drive letter can be remapped by
// the user, and a path-only reparse check can race a replacement junction.
internal static class WindowsFileOpen
{
    private const uint ObjectCaseInsensitive = 0x00000040;
    private const uint ObjectDontReparse = 0x00001000;
    private const uint FileNonDirectoryFile = 0x00000040;
    private const uint FileSynchronousIoNonAlert = 0x00000020;
    private const uint FileOpen = 1;
    private const uint Synchronize = 0x00100000;
    private const uint FileReadData = 0x00000001;
    private const uint FileReadAttributes = 0x00000080;
    private const uint FileAttributeReparsePoint = 0x00000400;
    private const int FileAttributeTagInfo = 9;

    internal static FileStream OpenSourceExecutable(string path)
    {
        var fullPath = Path.GetFullPath(path);
        var root = Path.GetPathRoot(fullPath);
        if (root is null || root.Length != 3 || root[1] != ':'
            || NativeMethods.GetDriveTypeW(root) != NativeMethods.DriveTypeFixed)
            throw new BrokerSecurityException("Executable must be on a fixed local volume.");

        var volume = new StringBuilder(64);
        if (!GetVolumeNameForVolumeMountPointW(root, volume, (uint)volume.Capacity))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "GetVolumeNameForVolumeMountPointW");
        var volumeName = volume.ToString();
        if (!volumeName.StartsWith(@"\\?\Volume{", StringComparison.OrdinalIgnoreCase)
            || !volumeName.EndsWith(@"}\", StringComparison.Ordinal))
            throw new BrokerSecurityException("Local volume has no stable volume-GUID name.");

        var relativePath = fullPath[root.Length..];
        var rootHandle = CreateFileW(
            volumeName,
            FileReadAttributes | Synchronize,
            NativeMethods.FileShareRead | NativeMethods.FileShareWrite,
            IntPtr.Zero,
            NativeMethods.OpenExisting,
            0x02000000, // FILE_FLAG_BACKUP_SEMANTICS: open the directory itself.
            IntPtr.Zero);
        if (rootHandle.IsInvalid)
        {
            var error = Marshal.GetLastWin32Error();
            rootHandle.Dispose();
            throw new Win32Exception(error, "CreateFileW(volume root)");
        }
        using var volumeRootHandle = rootHandle;
        var nameBuffer = Marshal.StringToHGlobalUni(relativePath);
        var unicodeStringBuffer = IntPtr.Zero;
        try
        {
            var unicodeString = new UnicodeString
            {
                Length = checked((ushort)(relativePath.Length * sizeof(char))),
                MaximumLength = checked((ushort)((relativePath.Length + 1) * sizeof(char))),
                Buffer = nameBuffer,
            };
            unicodeStringBuffer = Marshal.AllocHGlobal(Marshal.SizeOf<UnicodeString>());
            Marshal.StructureToPtr(unicodeString, unicodeStringBuffer, false);
            var attributes = new ObjectAttributes
            {
                Length = Marshal.SizeOf<ObjectAttributes>(),
                RootDirectory = rootHandle.DangerousGetHandle(),
                ObjectName = unicodeStringBuffer,
                Attributes = ObjectCaseInsensitive | ObjectDontReparse,
            };
            var status = NtCreateFile(
                out var handle,
                FileReadData | FileReadAttributes | Synchronize,
                ref attributes,
                out _,
                IntPtr.Zero,
                0,
                NativeMethods.FileShareRead,
                FileOpen,
                FileNonDirectoryFile | FileSynchronousIoNonAlert,
                IntPtr.Zero,
                0);
            if (status < 0)
            {
                handle?.Dispose();
                throw new BrokerSecurityException($"No-follow executable open failed (NTSTATUS 0x{status:X8}).");
            }

            try
            {
                if (!GetFileInformationByHandleEx(
                        handle, FileAttributeTagInfo, out var tag, (uint)Marshal.SizeOf<FileAttributeTag>()))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "GetFileInformationByHandleEx");
                if ((tag.FileAttributes & FileAttributeReparsePoint) != 0)
                    throw new BrokerSecurityException("Reparse-point executable refused.");

                var stream = new FileStream(handle, FileAccess.Read, 64 * 1024, isAsync: false);
                if (stream.Length is <= 0 or > 1_073_741_824)
                {
                    stream.Dispose();
                    throw new BrokerSecurityException("Executable is empty or larger than the bounded limit.");
                }
                return stream;
            }
            catch
            {
                handle.Dispose();
                throw;
            }
        }
        finally
        {
            if (unicodeStringBuffer != IntPtr.Zero)
                Marshal.FreeHGlobal(unicodeStringBuffer);
            Marshal.FreeHGlobal(nameBuffer);
        }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString
    {
        internal ushort Length;
        internal ushort MaximumLength;
        internal IntPtr Buffer;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes
    {
        internal int Length;
        internal IntPtr RootDirectory;
        internal IntPtr ObjectName;
        internal uint Attributes;
        internal IntPtr SecurityDescriptor;
        internal IntPtr SecurityQualityOfService;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock
    {
        internal IntPtr Status;
        internal IntPtr Information;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileAttributeTag
    {
        internal uint FileAttributes;
        internal uint ReparseTag;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetVolumeNameForVolumeMountPointW(
        string volumeMountPoint,
        StringBuilder volumeName,
        uint bufferLength);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string fileName,
        uint desiredAccess,
        uint shareMode,
        IntPtr securityAttributes,
        uint creationDisposition,
        uint flagsAndAttributes,
        IntPtr templateFile);

    [DllImport("ntdll.dll", ExactSpelling = true)]
    private static extern int NtCreateFile(
        out SafeFileHandle fileHandle,
        uint desiredAccess,
        ref ObjectAttributes objectAttributes,
        out IoStatusBlock ioStatusBlock,
        IntPtr allocationSize,
        uint fileAttributes,
        uint shareAccess,
        uint createDisposition,
        uint createOptions,
        IntPtr eaBuffer,
        uint eaLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandleEx(
        SafeFileHandle fileHandle,
        int fileInformationClass,
        out FileAttributeTag fileInformation,
        uint bufferSize);
}
