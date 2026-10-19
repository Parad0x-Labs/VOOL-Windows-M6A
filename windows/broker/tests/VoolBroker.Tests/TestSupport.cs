namespace VoolBroker.Tests;

using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Security.Principal;
using Xunit;

internal static class TestSupport
{
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetProcessTimes(nint process, out long creation, out long exit, out long kernel, out long user);

    [DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint GetSecurityInfo(nint handle, int objectType, int securityInformation,
        out nint owner, out nint group, out nint dacl, out nint sacl, out nint descriptor);

    [StructLayout(LayoutKind.Sequential)]
    private struct AclSizeInformation { public uint AceCount; public uint AclBytesInUse; public uint AclBytesFree; }

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetAclInformation(nint acl, out AclSizeInformation info, uint length, int informationClass);

    [DllImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetAce(nint acl, uint index, out nint ace);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool ConvertSidToStringSidW(nint sid, out nint stringSid);

    [DllImport("kernel32.dll")]
    private static extern nint LocalFree(nint memory);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetNamedPipeServerProcessId(nint pipe, out uint processId);

    [DllImport("ntdll.dll")]
    private static extern int NtQuerySystemInformation(int informationClass, nint information, int length, out int returnLength);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFileW(
        string name, uint access, uint shareMode, nint securityAttributes, uint creationDisposition, uint flags, nint template);

    // SYSTEM_HANDLE_TABLE_ENTRY_INFO_EX (x64). The test only reads this table: it never duplicates
    // or queries a broker handle, because a query on a synchronous pipe handle with a pending
    // ConnectNamedPipe or read blocks until that I/O ends (the hang seen in build 3a2).
    [StructLayout(LayoutKind.Sequential)]
    private struct SystemHandleTableEntryInfoEx
    {
        public nint Object;
        public nuint UniqueProcessId;
        public nuint HandleValue;
        public uint GrantedAccess;
        public ushort CreatorBackTraceIndex;
        public ushort ObjectTypeIndex;
        public uint HandleAttributes;
        public uint Reserved;
    }

    internal readonly record struct HandleEntry(ulong Value, uint GrantedAccess, ushort TypeIndex, bool Inheritable);

    private const int SystemExtendedHandleInformation = 64;
    private const int StatusInfoLengthMismatch = unchecked((int)0xC0000004);
    private const uint ObjInherit = 0x2;
    private const uint GenericReadWrite = 0xC0000000;
    private const uint OpenExisting = 3;
    private const uint FileAttributeNormal = 0x80;

    /// <summary>FILE_GENERIC_READ | FILE_GENERIC_WRITE: the access a PIPE_ACCESS_DUPLEX server handle has.</summary>
    internal const uint DuplexPipeServerAccess = 0x0012019F;

    public static long CurrentCreationTime
    {
        get
        {
            if (!GetProcessTimes(Process.GetCurrentProcess().Handle, out var creation, out _, out _, out _))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return creation;
        }
    }

    public static long CreationTime(Process process)
    {
        if (!GetProcessTimes(process.Handle, out var creation, out _, out _, out _))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        return creation;
    }

    public static (byte Type, string Sid)[] ReadPipeDacl(NamedPipeClientStream pipe)
    {
        var result = GetSecurityInfo(pipe.SafePipeHandle.DangerousGetHandle(), 6, 4, out _, out _, out var dacl, out _, out var descriptor);
        if (result != 0) throw new System.ComponentModel.Win32Exception((int)result);
        try
        {
            if (!GetAclInformation(dacl, out var info, (uint)Marshal.SizeOf<AclSizeInformation>(), 2))
                throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            var entries = new (byte Type, string Sid)[info.AceCount];
            for (uint i = 0; i < info.AceCount; i++)
            {
                if (!GetAce(dacl, i, out var ace)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                var type = Marshal.ReadByte(ace);
                var sid = new nint(ace.ToInt64() + 8);
                if (!ConvertSidToStringSidW(sid, out var text)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
                try { entries[i] = (type, Marshal.PtrToStringUni(text)!); }
                finally { LocalFree(text); }
            }
            return entries;
        }
        finally { LocalFree(descriptor); }
    }

    public static uint PipeServerPid(NamedPipeClientStream pipe)
    {
        if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle.DangerousGetHandle(), out var pid))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        return pid;
    }

    /// <summary>
    /// The handle table entries of one process, read from SystemExtendedHandleInformation. No handle
    /// is opened, duplicated or queried. Fails (never returns empty) when the table cannot be read.
    /// </summary>
    public static List<HandleEntry> ReadHandleTable(int processId)
    {
        var size = 4 * 1024 * 1024;
        while (true)
        {
            var buffer = Marshal.AllocHGlobal(size);
            try
            {
                var status = NtQuerySystemInformation(SystemExtendedHandleInformation, buffer, size, out var required);
                if (status == StatusInfoLengthMismatch)
                {
                    if (size >= 512 * 1024 * 1024)
                        throw new InvalidOperationException("The system handle table did not fit in 512 MiB.");
                    size = Math.Max(size * 2, required + 1024 * 1024);
                    continue;
                }
                if (status != 0)
                    throw new InvalidOperationException($"NtQuerySystemInformation(SystemExtendedHandleInformation) failed with NTSTATUS 0x{status:x8}.");

                var count = (long)(ulong)Marshal.ReadIntPtr(buffer);
                var header = 2 * IntPtr.Size;
                var entrySize = Marshal.SizeOf<SystemHandleTableEntryInfoEx>();
                if (entrySize != 40)
                    throw new InvalidOperationException($"Unexpected handle entry size {entrySize}; this test supports x64 only.");
                if (count <= 0 || count > (size - header) / entrySize)
                    throw new InvalidOperationException($"The handle table reported {count} entries for a {size}-byte buffer.");

                var result = new List<HandleEntry>();
                for (long i = 0; i < count; i++)
                {
                    var entry = Marshal.PtrToStructure<SystemHandleTableEntryInfoEx>(buffer + header + checked((int)(i * entrySize)));
                    if (entry.UniqueProcessId != (nuint)processId) continue;
                    result.Add(new HandleEntry(entry.HandleValue, entry.GrantedAccess, entry.ObjectTypeIndex,
                        (entry.HandleAttributes & ObjInherit) != 0));
                }
                if (result.Count == 0)
                    throw new InvalidOperationException($"The handle table holds no entries for process {processId}.");
                return result;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }
    }

    /// <summary>
    /// The process's handle table once it stops changing: the same entries in reads 200 ms apart, three
    /// times running. Used to wait out the .NET runtime's start-up before a test changes anything.
    /// </summary>
    public static async Task<Dictionary<ulong, HandleEntry>> SettledHandleTable(int processId, CancellationToken token)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(15);
        var previous = ReadHandleTable(processId).ToHashSet();
        var stable = 0;
        while (stable < 3)
        {
            if (DateTime.UtcNow > deadline)
                throw new InvalidOperationException($"The handle table of process {processId} did not settle within 15 s.");
            await Task.Delay(200, token);
            var current = ReadHandleTable(processId).ToHashSet();
            stable = current.SetEquals(previous) ? stable + 1 : 0;
            previous = current;
        }
        return previous.ToDictionary(entry => entry.Value);
    }

    /// <summary>The object type index of a file handle, learned from a handle this process opens itself.</summary>
    public static ushort FileObjectTypeIndex()
    {
        var path = Path.Combine(Path.GetTempPath(), "vool-broker-type-probe-" + Guid.NewGuid().ToString("N"));
        using var probe = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None, 1, FileOptions.DeleteOnClose);
        var value = (ulong)probe.SafeFileHandle.DangerousGetHandle();
        foreach (var entry in ReadHandleTable(Environment.ProcessId))
            if (entry.Value == value) return entry.TypeIndex;
        throw new InvalidOperationException("The test's own file handle is not in the handle table.");
    }

    public static bool IsPipeBusy(string pipeName)
    {
        using var pipe = CreateFileW(pipeName, GenericReadWrite, 0, 0, OpenExisting, FileAttributeNormal, 0);
        if (!pipe.IsInvalid) return false;
        var error = Marshal.GetLastWin32Error();
        if (error == 231) return true;
        throw new System.ComponentModel.Win32Exception(error, $"Opening the seventeenth connection failed with Win32 error {error}, not ERROR_PIPE_BUSY.");
    }

    public static string NewTempDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), "vool-broker-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }

    private static string BrokerExe() =>
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../src/VoolBroker/bin/Debug/net10.0-windows/VoolBroker.exe"));

    /// <summary>Starts the broker and leaves it waiting for its bootstrap frame on stdin.</summary>
    public static Process StartBrokerWithoutBootstrap() =>
        Process.Start(new ProcessStartInfo(BrokerExe()) { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true })!;

    public static void SendBootstrap(Process process, byte[] raw)
    {
        process.StandardInput.BaseStream.Write(raw);
        process.StandardInput.BaseStream.Flush();
    }

    public static Process StartBroker(object? bootstrap = null, byte[]? raw = null)
    {
        var process = StartBrokerWithoutBootstrap();
        SendBootstrap(process, raw ?? Frame(bootstrap ?? DefaultBootstrap()));
        return process;
    }

    public static object DefaultBootstrap() => new
    {
        secret = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant(),
        api_pid = Environment.ProcessId,
        api_creation_time = CurrentCreationTime,
        flagged = Array.Empty<string>(),
        journal_dir = Path.GetTempPath()
    };

    public static byte[] Frame(object value)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(value);
        var result = new byte[4 + payload.Length];
        BinaryPrimitives.WriteUInt32LittleEndian(result, (uint)payload.Length);
        payload.CopyTo(result.AsSpan(4));
        return result;
    }

    public static async Task<JsonDocument> ReadFrame(Stream stream, CancellationToken token = default)
    {
        var header = new byte[4];
        await ReadExact(stream, header, token);
        var body = new byte[BinaryPrimitives.ReadUInt32LittleEndian(header)];
        await ReadExact(stream, body, token);
        return JsonDocument.Parse(body);
    }

    public static async Task WriteFrame(Stream stream, object value, CancellationToken token = default) => await stream.WriteAsync(Frame(value), token);

    public static async Task<NamedPipeClientStream> Connect(string name, CancellationToken token = default)
    {
        var pipe = new NamedPipeClientStream(".", name[@"\\.\pipe\".Length..], PipeDirection.InOut, PipeOptions.Asynchronous);
        await pipe.ConnectAsync(5000, token);
        return pipe;
    }

    public static async Task Authenticate(Stream stream, string secret)
    {
        using var challenge = await ReadFrame(stream);
        var serverNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
        var clientNonce = RandomNumberGenerator.GetBytes(32);
        var key = Convert.FromHexString(secret);
        await WriteFrame(stream, new { type = "client_proof", nonce = Convert.ToHexString(clientNonce).ToLowerInvariant(), hmac = Convert.ToHexString(HMACSHA256.HashData(key, serverNonce)).ToLowerInvariant() });
        using var response = await ReadFrame(stream);
        Assert.Equal(Convert.ToHexString(HMACSHA256.HashData(key, clientNonce)).ToLowerInvariant(), response.RootElement.GetProperty("hmac").GetString());
    }

    public static async Task AuthenticateWithNonce(Stream stream, string secret, byte[] clientNonce)
    {
        using var challenge = await ReadFrame(stream, TestContext.Current.CancellationToken);
        var serverNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
        var key = Convert.FromHexString(secret);
        await WriteFrame(stream, new { type = "client_proof", nonce = Convert.ToHexString(clientNonce).ToLowerInvariant(), hmac = Convert.ToHexString(HMACSHA256.HashData(key, serverNonce)).ToLowerInvariant() }, TestContext.Current.CancellationToken);
        using var response = await ReadFrame(stream, TestContext.Current.CancellationToken);
        Assert.Equal(Convert.ToHexString(HMACSHA256.HashData(key, clientNonce)).ToLowerInvariant(), response.RootElement.GetProperty("hmac").GetString());
    }

    private static async Task ReadExact(Stream stream, byte[] buffer, CancellationToken token)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var count = await stream.ReadAsync(buffer.AsMemory(offset), token);
            if (count == 0) throw new EndOfStreamException();
            offset += count;
        }
    }
}
