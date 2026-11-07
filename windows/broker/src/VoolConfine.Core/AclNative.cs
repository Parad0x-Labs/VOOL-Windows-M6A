using Microsoft.Win32.SafeHandles;
using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;

[assembly: InternalsVisibleTo("VoolConfine.Tests")]

namespace VoolConfine.Core;

/// <summary>
/// The Win32 and NT calls the ACL code uses. Every object is reached by handle: a root by
/// CreateFileW with FILE_FLAG_OPEN_REPARSE_POINT, every entry under it by NtCreateFile relative to
/// its parent's handle with OBJ_DONT_REPARSE and FILE_OPEN_REPARSE_POINT. DACLs are read with
/// GetKernelObjectSecurity and written with SetKernelObjectSecurity, which set exactly one object
/// and never propagate to children by path (SetNamedSecurityInfo, TreeSetNamedSecurityInfo and
/// SetSecurityInfo all do, through MARTA, and are never used here; docs/WINDOWS_BROKER.md 476-479).
/// </summary>
internal static class AclNative
{
    internal const uint ReadControl = 0x00020000;
    internal const uint WriteDac = 0x00040000;
    internal const uint Synchronize = 0x00100000;
    internal const uint FileListDirectory = 0x0001;
    internal const uint FileReadAttributes = 0x0080;

    internal const uint ShareRead = 1;
    internal const uint ShareWrite = 2;
    internal const uint ShareDelete = 4;
    private const uint OpenExisting = 3;
    private const uint FileFlagBackupSemantics = 0x02000000;
    private const uint FileFlagOpenReparsePoint = 0x00200000;

    internal const uint FileAttributeDirectory = 0x10;
    internal const uint FileAttributeReparsePoint = 0x400;

    private const uint FileOpen = 1;
    private const uint FileDirectoryFile = 0x1;
    private const uint FileSynchronousIoNonAlert = 0x20;
    private const uint FileOpenReparsePoint = 0x00200000;
    // OBJ_DONT_REPARSE: a reparse point anywhere in the name stops the open instead of being followed.
    // Opens are case-SENSITIVE (no OBJ_CASE_INSENSITIVE): every name opened is one the listing just
    // returned, so in a case-sensitive folder ".git" and ".GIT" stay two objects.
    private const uint ObjectDontReparse = 0x1000;

    internal const int DaclSecurityInformation = 0x4;
    internal const int ProtectedDaclSecurityInformation = unchecked((int)0x80000000);
    internal const int UnprotectedDaclSecurityInformation = 0x20000000;

    private const uint FilePersistentAcls = 0x8;
    private const uint VolumeNameDos = 0;

    private const int FileAttributeTagInfoClass = 9;
    private const int FileStandardInfoClass = 1;
    private const int FileIdInfoClass = 18;
    private const int FileIdBothDirectoryInfoClass = 10;
    private const int FileIdBothDirectoryRestartInfoClass = 11;
    private const int ErrorNoMoreFiles = 18;
    private const int ErrorFileNotFound = 2;
    private const int ErrorInsufficientBuffer = 122;

    /// <summary>One folder entry: its long name, its 8.3 name ("" when it has none) and its attributes.</summary>
    internal readonly record struct Entry(string Name, string ShortName, uint Attributes)
    {
        public bool IsDirectory => (Attributes & FileAttributeDirectory) != 0;
        public bool IsReparsePoint => (Attributes & FileAttributeReparsePoint) != 0;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString { public ushort Length; public ushort MaximumLength; public nint Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes { public int Length; public nint RootDirectory; public nint ObjectName; public uint Attributes; public nint SecurityDescriptor; public nint SecurityQualityOfService; }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock { public nint Status; public nuint Information; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileAttributeTagInfo { public uint FileAttributes; public uint ReparseTag; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileStandardInfo { public long AllocationSize; public long EndOfFile; public uint NumberOfLinks; [MarshalAs(UnmanagedType.U1)] public bool DeletePending; [MarshalAs(UnmanagedType.U1)] public bool Directory; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileIdInfo { public ulong VolumeSerialNumber; [MarshalAs(UnmanagedType.ByValArray, SizeConst = 16)] public byte[] FileId; }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(string name, uint access, uint share, nint security,
        uint creation, uint flags, nint template);

    [DllImport("ntdll.dll")]
    private static extern int NtCreateFile(out SafeFileHandle handle, uint access, nint objectAttributes,
        out IoStatusBlock ioStatus, nint allocationSize, uint fileAttributes, uint shareAccess,
        uint createDisposition, uint createOptions, nint eaBuffer, uint eaLength);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetKernelObjectSecurity(SafeFileHandle handle, int information, byte[]? descriptor,
        uint length, out uint needed);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetKernelObjectSecurity(SafeFileHandle handle, int information, byte[] descriptor);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandleEx(SafeFileHandle handle, int infoClass, nint info, uint size);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandleW(SafeFileHandle handle, char[] path, uint length, uint flags);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetVolumeInformationByHandleW(SafeFileHandle handle, char[]? volumeName,
        uint volumeNameSize, out uint serial, out uint maxComponentLength, out uint flags,
        char[] filesystem, uint filesystemSize);

    private static AclException Win32Failure(string code, string what, int error) =>
        new(code, $"{what}: {new Win32Exception(error).Message} (Win32 error {error})");

    /// <summary>Opens a root folder by its path, the folder itself and never a link's target.</summary>
    internal static SafeFileHandle OpenRoot(string path, bool writeDac)
    {
        var access = ReadControl | FileReadAttributes | FileListDirectory | Synchronize | (writeDac ? WriteDac : 0);
        var handle = CreateFileW(path, access, ShareRead | ShareWrite, 0, OpenExisting,
            FileFlagBackupSemantics | FileFlagOpenReparsePoint, 0);
        if (handle.IsInvalid)
        {
            var error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw Win32Failure(error == ErrorFileNotFound || error == 3 ? "root_missing" : "root_open_failed", $"Cannot open {path}", error);
        }
        return handle;
    }

    /// <summary>
    /// Opens one entry relative to its parent folder's handle, never following a reparse point.
    /// A folder is opened with list access and without delete sharing, so while the walk holds it,
    /// it cannot be renamed or deleted, and neither can any folder above it that the walk holds.
    /// A file is opened for its security only (no data access), so a file another program holds
    /// exclusively can still be read and given its ACE.
    /// Returns null when the open is refused for access (the caller decides what that means).
    /// </summary>
    internal static SafeFileHandle? OpenChild(SafeFileHandle parent, string name, bool directory, bool writeDac)
    {
        var access = ReadControl | FileReadAttributes | Synchronize | (writeDac ? WriteDac : 0) | (directory ? FileListDirectory : 0);
        var share = directory ? ShareRead | ShareWrite : ShareRead | ShareWrite | ShareDelete;
        var options = FileSynchronousIoNonAlert | FileOpenReparsePoint | (directory ? FileDirectoryFile : 0);
        var nameMemory = Marshal.StringToHGlobalUni(name);
        var unicodeMemory = Marshal.AllocHGlobal(Marshal.SizeOf<UnicodeString>());
        var attributesMemory = Marshal.AllocHGlobal(Marshal.SizeOf<ObjectAttributes>());
        var added = false;
        try
        {
            parent.DangerousAddRef(ref added);
            var bytes = checked((ushort)(name.Length * 2));
            Marshal.StructureToPtr(new UnicodeString { Length = bytes, MaximumLength = bytes, Buffer = nameMemory }, unicodeMemory, false);
            Marshal.StructureToPtr(new ObjectAttributes
            {
                Length = Marshal.SizeOf<ObjectAttributes>(), RootDirectory = parent.DangerousGetHandle(),
                ObjectName = unicodeMemory, Attributes = ObjectDontReparse,
            }, attributesMemory, false);
            var status = NtCreateFile(out var handle, access, attributesMemory, out _, 0, 0, share, FileOpen, options, 0, 0);
            if (status >= 0) return handle;
            handle.Dispose();
            var code = unchecked((uint)status);
            if (code == 0xC0000022) return null; // STATUS_ACCESS_DENIED
            throw new AclException(code switch
            {
                0xC0000034 or 0xC000003A => "entry_vanished",
                0xC000050B or 0xC0000280 => "reparse_point",
                0xC0000043 => "in_use",
                _ => "open_failed",
            }, $"Cannot open '{name}' (NTSTATUS 0x{code:x8}).");
        }
        finally
        {
            if (added) parent.DangerousRelease();
            Marshal.FreeHGlobal(nameMemory); Marshal.FreeHGlobal(unicodeMemory); Marshal.FreeHGlobal(attributesMemory);
        }
    }

    /// <summary>The object's security descriptor, DACL part only, in self-relative form.</summary>
    internal static byte[] GetDacl(SafeFileHandle handle, string what)
    {
        GetKernelObjectSecurity(handle, DaclSecurityInformation, null, 0, out var needed);
        var error = Marshal.GetLastWin32Error();
        if (needed == 0) throw Win32Failure("acl_read_failed", $"Cannot read the DACL of {what}", error);
        for (var attempt = 0; attempt < 4; attempt++)
        {
            var buffer = new byte[needed];
            if (GetKernelObjectSecurity(handle, DaclSecurityInformation, buffer, needed, out var again)) return buffer;
            error = Marshal.GetLastWin32Error();
            if (error != ErrorInsufficientBuffer) break;
            needed = again;
        }
        throw Win32Failure("acl_read_failed", $"Cannot read the DACL of {what}", error);
    }

    /// <summary>Writes the DACL of exactly this object, keeping its protected / unprotected state.</summary>
    internal static void SetDacl(SafeFileHandle handle, byte[] descriptor, bool isProtected, string what)
    {
        var information = DaclSecurityInformation | (isProtected ? ProtectedDaclSecurityInformation : UnprotectedDaclSecurityInformation);
        if (!SetKernelObjectSecurity(handle, information, descriptor))
            throw Win32Failure("acl_write_failed", $"Cannot write the DACL of {what}", Marshal.GetLastWin32Error());
    }

    internal static uint GetAttributes(SafeFileHandle handle, string what)
    {
        var memory = Marshal.AllocHGlobal(Marshal.SizeOf<FileAttributeTagInfo>());
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileAttributeTagInfoClass, memory, (uint)Marshal.SizeOf<FileAttributeTagInfo>()))
                throw Win32Failure("open_failed", $"Cannot read the attributes of {what}", Marshal.GetLastWin32Error());
            return Marshal.PtrToStructure<FileAttributeTagInfo>(memory).FileAttributes;
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    internal static uint GetLinkCount(SafeFileHandle handle, string what)
    {
        var memory = Marshal.AllocHGlobal(Marshal.SizeOf<FileStandardInfo>());
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileStandardInfoClass, memory, (uint)Marshal.SizeOf<FileStandardInfo>()))
                throw Win32Failure("open_failed", $"Cannot read the link count of {what}", Marshal.GetLastWin32Error());
            return Marshal.PtrToStructure<FileStandardInfo>(memory).NumberOfLinks;
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    /// <summary>Volume serial and 128-bit file id, so a path is matched by the object it names, not by its spelling.</summary>
    internal static (ulong Volume, string Id) GetIdentity(SafeFileHandle handle, string what)
    {
        var size = Marshal.SizeOf<FileIdInfo>();
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            if (!GetFileInformationByHandleEx(handle, FileIdInfoClass, memory, (uint)size))
                throw Win32Failure("open_failed", $"Cannot read the file id of {what}", Marshal.GetLastWin32Error());
            var info = Marshal.PtrToStructure<FileIdInfo>(memory);
            return (info.VolumeSerialNumber, Convert.ToHexString(info.FileId));
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    /// <summary>The handle's final path in drive-letter form, without the \\?\ prefix.</summary>
    internal static string GetFinalPath(SafeFileHandle handle, string what)
    {
        var buffer = new char[32768];
        var length = GetFinalPathNameByHandleW(handle, buffer, (uint)buffer.Length, VolumeNameDos);
        if (length == 0 || length >= buffer.Length)
            throw Win32Failure("root_open_failed", $"Cannot read the final path of {what}", Marshal.GetLastWin32Error());
        var path = new string(buffer, 0, (int)length);
        return path.StartsWith(@"\\?\", StringComparison.Ordinal) ? path[4..] : path;
    }

    /// <summary>True when the volume keeps ACLs (NTFS does; FAT, exFAT and some shares do not).</summary>
    internal static bool VolumeKeepsAcls(SafeFileHandle handle, string what)
    {
        var filesystem = new char[64];
        if (!GetVolumeInformationByHandleW(handle, null, 0, out _, out _, out var flags, filesystem, (uint)filesystem.Length))
            throw Win32Failure("root_open_failed", $"Cannot read the volume of {what}", Marshal.GetLastWin32Error());
        return (flags & FilePersistentAcls) != 0;
    }

    /// <summary>Every entry but "." and ".." of the folder the handle holds, with 8.3 names, read from the handle.</summary>
    internal static List<Entry> List(SafeFileHandle handle, string what)
    {
        const int size = 65536;
        var entries = new List<Entry>();
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            for (var infoClass = FileIdBothDirectoryRestartInfoClass; ; infoClass = FileIdBothDirectoryInfoClass)
            {
                if (!GetFileInformationByHandleEx(handle, infoClass, memory, size))
                {
                    var error = Marshal.GetLastWin32Error();
                    if (error is ErrorNoMoreFiles or ErrorFileNotFound) return entries;
                    throw Win32Failure("list_failed", $"Cannot list {what}", error);
                }
                // FILE_ID_BOTH_DIR_INFO: FileAttributes at 56, FileNameLength (bytes) at 60,
                // ShortNameLength (bytes, one byte) at 68, ShortName (12 WCHAR) at 70, FileName at 104.
                for (var offset = 0; ;)
                {
                    var entry = memory + offset;
                    var name = Marshal.PtrToStringUni(entry + 104, Marshal.ReadInt32(entry, 60) / 2) ?? "";
                    var shortLength = Marshal.ReadByte(entry, 68) / 2;
                    var shortName = shortLength is > 0 and <= 12 ? Marshal.PtrToStringUni(entry + 70, shortLength) ?? "" : "";
                    if (name is not ("." or ".."))
                        entries.Add(new Entry(name, shortName, unchecked((uint)Marshal.ReadInt32(entry, 56))));
                    var next = Marshal.ReadInt32(entry);
                    if (next == 0) break;
                    offset += next;
                }
            }
        }
        finally { Marshal.FreeHGlobal(memory); }
    }
}
