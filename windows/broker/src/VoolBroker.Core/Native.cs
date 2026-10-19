using Microsoft.Win32.SafeHandles;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Runtime.CompilerServices;

[assembly: InternalsVisibleTo("VoolBroker.Tests")]

namespace VoolBroker.Core;

internal static class Native
{
    internal const uint FileFlagBackupSemantics = 0x02000000;
    internal const uint FileFlagOpenReparsePoint = 0x00200000;
    internal const uint ShareRead = 1;
    internal const uint ShareWrite = 2;
    internal const uint ShareDelete = 4;
    internal const uint OpenExisting = 3;
    internal const uint GenericRead = 0x80000000;
    internal const uint GenericWrite = 0x40000000;
    internal const uint FileReadAttributes = 0x0080;
    internal const uint FileWriteAttributes = 0x0100;
    internal const uint FileReadData = 1;
    internal const uint FileListDirectory = 1;
    internal const uint FileWriteData = 2;
    internal const uint Delete = 0x00010000;
    internal const uint Synchronize = 0x00100000;
    internal const uint PipeAccessDuplex = 0x00000003;
    internal const uint FileFlagOverlapped = 0x40000000;
    internal const uint FileFlagFirstPipeInstance = 0x00080000;
    internal const uint PipeTypeByte = 0;
    internal const uint PipeReadModeByte = 0;
    internal const uint PipeWait = 0;
    internal const uint PipeRejectRemoteClients = 0x00000008;
    internal const int ErrorPipeConnected = 535;
    internal const uint ProcessQueryLimitedInformation = 0x1000;
    internal const uint ThreadTerminate = 0x0001;
    internal const uint FileAttributeDirectory = 0x10;
    internal const uint FileAttributeReparsePoint = 0x400;
    internal const uint FileOpenReparsePoint = 0x00200000;
    internal const uint FileOpen = 1;
    internal const uint FileCreate = 2;
    internal const uint FileAttributeReadOnly = 0x1;
    internal const uint FileAttributeNormal = 0x80;
    internal const uint FileDirectoryFile = 0x1;
    internal const uint FileSynchronousIoNonAlert = 0x20;
    internal const uint FileNonDirectoryFile = 0x40;
    internal const uint ObjectCaseInsensitive = 0x40;
    // OBJ_DONT_REPARSE: the kernel refuses to follow any reparse point while it parses the name, so
    // a link swapped in between two component opens cannot redirect the open itself.
    internal const uint ObjectDontReparse = 0x1000;
    internal const uint FileCaseSensitiveSearch = 0x1;
    internal const int FileAttributeTagInfoClass = 9;
    internal const int FileStandardInfoClass = 1;
    internal const int FileNameInfoClass = 2;
    internal const int FileCaseSensitiveInfoClass = 23;
    internal const int FileIdInfoClass = 18;
    internal const uint VolumeNameDos = 0;
    internal const uint VolumeNameGuid = 1;
    private const int FileRenameInformationClass = 10;
    private const int FileDispositionInformationExClass = 64;
    private const int FileFullDirectoryInfoClass = 14;
    private const int FileFullDirectoryRestartInfoClass = 15;
    // FILE_DISPOSITION_DELETE | FILE_DISPOSITION_POSIX_SEMANTICS | FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE
    private const uint DispositionDeleteNow = 0x1 | 0x2 | 0x10;
    private const int ErrorNoMoreFiles = 18;

    [StructLayout(LayoutKind.Sequential)]
    internal struct FileAttributeTagInfo { public uint FileAttributes; public uint ReparseTag; }
    [StructLayout(LayoutKind.Sequential)]
    internal struct FileStandardInfo { public long AllocationSize; public long EndOfFile; public uint NumberOfLinks; [MarshalAs(UnmanagedType.U1)] public bool DeletePending; [MarshalAs(UnmanagedType.U1)] public bool Directory; }
    [StructLayout(LayoutKind.Sequential)]
    internal struct FileCaseSensitiveInfo { public uint Flags; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileIdInfo { public ulong VolumeSerialNumber; [MarshalAs(UnmanagedType.ByValArray, SizeConst = 16)] public byte[] FileId; }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString { public ushort Length; public ushort MaximumLength; public nint Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes { public int Length; public nint RootDirectory; public nint ObjectName; public uint Attributes; public nint SecurityDescriptor; public nint SecurityQualityOfService; }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock { public nint Status; public nuint Information; }

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
    internal static extern SafeWaitHandle OpenThread(uint access, [MarshalAs(UnmanagedType.Bool)] bool inheritHandle, uint threadId);

    [DllImport("kernel32.dll")]
    internal static extern uint GetCurrentThreadId();

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool CancelSynchronousIo(SafeWaitHandle thread);

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

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandleEx(SafeFileHandle handle, int infoClass,
        out FileAttributeTagInfo info, uint size);

    [StructLayout(LayoutKind.Sequential)]
    private struct FileIdDescriptor { public uint Size; public int Type; public ulong IdLow; public ulong IdHigh; }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeFileHandle OpenFileById(SafeFileHandle volumeHint, ref FileIdDescriptor id, uint access,
        uint share, nint security, uint flags);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FlushFileBuffers(SafeFileHandle handle);

    [DllImport("ntdll.dll")]
    private static extern int NtSetInformationFile(SafeFileHandle handle, out IoStatusBlock ioStatus, nint info,
        uint length, int infoClass);

    [DllImport("ntdll.dll")]
    private static extern int NtCreateFile(out SafeFileHandle handle, uint access, nint objectAttributes,
        out IoStatusBlock ioStatus, nint allocationSize, uint fileAttributes, uint shareAccess,
        uint createDisposition, uint createOptions, nint eaBuffer, uint eaLength);

    internal static FileAttributeTagInfo GetAttributeTag(SafeFileHandle handle)
    {
        if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfoClass, out var info, (uint)Marshal.SizeOf<FileAttributeTagInfo>()))
            throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
        return info;
    }

    internal static FileStandardInfo GetStandardInfo(SafeFileHandle handle)
    {
        var memory = Marshal.AllocHGlobal(Marshal.SizeOf<FileStandardInfo>());
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileStandardInfoClass, memory, (uint)Marshal.SizeOf<FileStandardInfo>()))
                throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
            return Marshal.PtrToStructure<FileStandardInfo>(memory);
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    internal static uint GetCaseFlags(SafeFileHandle handle)
    {
        var memory = Marshal.AllocHGlobal(sizeof(uint));
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileCaseSensitiveInfoClass, memory, sizeof(uint)))
                throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
            return unchecked((uint)Marshal.ReadInt32(memory));
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    internal static (ulong VolumeSerial, byte[] FileId) GetFileIdentity(SafeFileHandle handle)
    {
        var size = Marshal.SizeOf<FileIdInfo>();
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileIdInfoClass, memory, (uint)size))
                throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
            var id = Marshal.PtrToStructure<FileIdInfo>(memory);
            return (id.VolumeSerialNumber, id.FileId);
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    internal static string GetFinalPath(SafeFileHandle handle, uint flags)
    {
        var buffer = new System.Text.StringBuilder(32768);
        var length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Capacity, flags);
        if (length == 0 || length >= buffer.Capacity) throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
        return buffer.ToString();
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, System.Text.StringBuilder path, uint length, uint flags);

    internal static string GetVolumePath(string path)
    {
        var buffer = new System.Text.StringBuilder(32768);
        if (!GetVolumePathNameW(path, buffer, (uint)buffer.Capacity)) throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
        return buffer.ToString();
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetVolumePathNameW(string fileName, System.Text.StringBuilder volumePath, uint bufferLength);

    /// <summary>
    /// Opens one component relative to parent, never following a reparse point
    /// (FILE_OPEN_REPARSE_POINT and OBJ_DONT_REPARSE), never sharing delete, not inheritable (no
    /// OBJ_INHERIT). The access always includes FILE_READ_DATA / FILE_LIST_DIRECTORY (the same bit):
    /// NTFS records share access only for opens with data or delete access, so a handle with
    /// attribute access alone would not stop a rename or delete of the object it holds.
    /// </summary>
    internal static SafeFileHandle OpenRelative(SafeFileHandle parent, string name, uint access, uint createOptions) =>
        OpenRelative(parent, name, access, createOptions, ShareRead | ShareWrite, FileOpen);

    /// <summary>
    /// OpenRelative with a chosen share mode and disposition (FILE_OPEN, or FILE_CREATE for a new
    /// temp file or folder). Delete is never shared, whatever the caller passes.
    /// </summary>
    internal static SafeFileHandle OpenRelative(SafeFileHandle parent, string name, uint access, uint createOptions, uint share, uint disposition)
    {
        var nameMemory = Marshal.StringToHGlobalUni(name);
        var unicodeMemory = Marshal.AllocHGlobal(Marshal.SizeOf<UnicodeString>());
        var attributesMemory = Marshal.AllocHGlobal(Marshal.SizeOf<ObjectAttributes>());
        var added = false;
        try
        {
            parent.DangerousAddRef(ref added);
            Marshal.StructureToPtr(new UnicodeString { Length = checked((ushort)(name.Length * 2)), MaximumLength = checked((ushort)(name.Length * 2)), Buffer = nameMemory }, unicodeMemory, false);
            Marshal.StructureToPtr(new ObjectAttributes { Length = Marshal.SizeOf<ObjectAttributes>(), RootDirectory = parent.DangerousGetHandle(), ObjectName = unicodeMemory, Attributes = ObjectCaseInsensitive | ObjectDontReparse }, attributesMemory, false);
            var attributes = disposition == FileCreate && (createOptions & FileDirectoryFile) == 0 ? FileAttributeNormal : 0;
            var status = NtCreateFile(out var handle, access | FileReadData | FileReadAttributes | Synchronize, attributesMemory, out _, 0,
                attributes, share & (ShareRead | ShareWrite), disposition, createOptions | FileOpenReparsePoint | FileSynchronousIoNonAlert, 0, 0);
            if (status < 0)
            {
                handle.Dispose();
                throw OpenFailure(name, unchecked((uint)status));
            }
            return handle;
        }
        finally
        {
            if (added) parent.DangerousRelease();
            Marshal.FreeHGlobal(nameMemory); Marshal.FreeHGlobal(unicodeMemory); Marshal.FreeHGlobal(attributesMemory);
        }
    }

    private static BrokerRefusal OpenFailure(string name, uint status) => status switch
    {
        0xC0000035 => new BrokerRefusal("destination_exists", $"Component '{name}' already exists."),
        0xC0000101 => new BrokerRefusal("not_empty", $"Folder '{name}' is not empty."),
        0xC0000121 => new BrokerRefusal("read_only", $"Component '{name}' is read-only."),
        0xC00000D4 => new BrokerRefusal("cross_volume", $"Component '{name}' is on another volume."),
        0xC0000043 or 0xC0000056 => new BrokerRefusal("in_use", $"Component '{name}' is held by another program."),
        0xC0000034 or 0xC000003A => new BrokerRefusal("not_found", $"Component '{name}' does not exist."),
        0xC0000103 => new BrokerRefusal("not_a_directory", $"Component '{name}' is not a directory."),
        0xC00000BA => new BrokerRefusal("is_a_directory", $"Component '{name}' is a directory."),
        // 0xC0000280 (STATUS_REPARSE_POINT_NOT_RESOLVED): the parent handle's folder became a
        // reparse point after its check, and OBJ_DONT_REPARSE stopped the open under it.
        0xC000050B or 0xC0000280 => new BrokerRefusal("reparse_point", $"Component '{name}' is reached through a reparse point."),
        0xC0000022 => new BrokerRefusal("access_denied", $"Access to component '{name}' is denied."),
        0xC0000033 => new BrokerRefusal("invalid_name", $"Component '{name}' is not a valid name."),
        _ => new BrokerRefusal("authority_check_failed", $"Cannot open component '{name}' (NTSTATUS 0x{status:x8}).")
    };

    /// <summary>
    /// Renames the object handle holds (it needs DELETE access) to newName in the folder newParent
    /// holds, never replacing an existing object (FileRenameInformation, ReplaceIfExists false).
    /// </summary>
    internal static void RenameInto(SafeFileHandle handle, SafeFileHandle newParent, string newName)
    {
        var nameBytes = checked(newName.Length * 2);
        var size = 20 + nameBytes + 2;
        var memory = Marshal.AllocHGlobal(size);
        var added = false;
        try
        {
            newParent.DangerousAddRef(ref added);
            Marshal.Copy(new byte[size], 0, memory, size);
            Marshal.WriteIntPtr(memory, 8, newParent.DangerousGetHandle());
            Marshal.WriteInt32(memory, 16, nameBytes);
            Marshal.Copy(newName.ToCharArray(), 0, memory + 20, newName.Length);
            var status = NtSetInformationFile(handle, out _, memory, (uint)size, FileRenameInformationClass);
            if (status < 0) throw OpenFailure(newName, unchecked((uint)status));
        }
        finally
        {
            if (added) newParent.DangerousRelease();
            Marshal.FreeHGlobal(memory);
        }
    }

    /// <summary>Deletes the object handle holds (it needs DELETE access) at once, read-only or not.</summary>
    internal static void DeleteNow(SafeFileHandle handle, string name)
    {
        var memory = Marshal.AllocHGlobal(sizeof(uint));
        try
        {
            Marshal.WriteInt32(memory, unchecked((int)DispositionDeleteNow));
            var status = NtSetInformationFile(handle, out _, memory, sizeof(uint), FileDispositionInformationExClass);
            if (status < 0) throw OpenFailure(name, unchecked((uint)status));
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    internal static void Flush(SafeFileHandle handle)
    {
        if (!FlushFileBuffers(handle)) throw new IOException("Flushing a temp file failed.", Marshal.GetLastWin32Error());
    }

    /// <summary>True when the folder handle holds has no entry but "." and "..".</summary>
    internal static bool IsDirectoryEmpty(SafeFileHandle handle)
    {
        const int size = 65536;
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            for (var infoClass = FileFullDirectoryRestartInfoClass; ; infoClass = FileFullDirectoryInfoClass)
            {
                if (!GetFileInformationByHandleEx(handle, infoClass, memory, size))
                {
                    var error = Marshal.GetLastWin32Error();
                    if (error == ErrorNoMoreFiles) return true;
                    throw new BrokerRefusal("authority_check_failed", new Win32Exception(error).Message);
                }
                for (var offset = 0; ;)
                {
                    var entry = memory + offset;
                    var nameLength = Marshal.ReadInt32(entry, 60);
                    var name = Marshal.PtrToStringUni(entry + 68, nameLength / 2);
                    if (name is not ("." or "..")) return false;
                    var next = Marshal.ReadInt32(entry);
                    if (next == 0) break;
                    offset += next;
                }
            }
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    /// <summary>
    /// Opens the object with the given 128-bit file id on the volume hint is on, without following
    /// a reparse point and without delete sharing. Any failure refuses: a caller cannot tell a
    /// deleted object from another error here, so it never treats a failure as "already gone".
    /// </summary>
    internal static SafeFileHandle OpenById(SafeFileHandle volumeHint, byte[] fileId, uint access)
    {
        if (fileId.Length != 16) throw new BrokerRefusal("authority_check_failed", "A file id must be 16 bytes.");
        var descriptor = new FileIdDescriptor
        {
            Size = (uint)Marshal.SizeOf<FileIdDescriptor>(), Type = 2,
            IdLow = BitConverter.ToUInt64(fileId, 0), IdHigh = BitConverter.ToUInt64(fileId, 8)
        };
        var handle = OpenFileById(volumeHint, ref descriptor, access | FileReadAttributes | Synchronize, ShareRead | ShareWrite, 0,
            FileFlagBackupSemantics | FileFlagOpenReparsePoint);
        if (handle.IsInvalid)
        {
            var error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new BrokerRefusal("authority_check_failed", $"Opening a journalled folder by its file id failed (Win32 error {error}).");
        }
        return handle;
    }

    internal static string GetObjectName(SafeFileHandle handle)
    {
        var memory = Marshal.AllocHGlobal(65540);
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileNameInfoClass, memory, 65540))
                throw new BrokerRefusal("authority_check_failed", new Win32Exception(Marshal.GetLastWin32Error()).Message);
            var length = Marshal.ReadInt32(memory);
            if (length < 0 || length > 65532 || (length & 1) != 0) throw new BrokerRefusal("authority_check_failed", "Invalid file name information.");
            return Marshal.PtrToStringUni(memory + sizeof(uint), length / 2) ?? "";
        }
        finally { Marshal.FreeHGlobal(memory); }
    }
}
