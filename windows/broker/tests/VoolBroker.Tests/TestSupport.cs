[assembly: Xunit.v3.Parallelization(Mode = Xunit.Sdk.ParallelMode.None)]

namespace VoolBroker.Tests;

using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Security.Principal;
using System.Diagnostics.CodeAnalysis;
using VoolBroker.Core;
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

    /// <summary>
    /// Set to 1 by windows-broker.yml, which makes the volumes and the share the admin rows need
    /// (8.3 names, a mounted-volume folder, FAT32, exFAT, a mapped drive): a row whose setup is
    /// missing then fails instead of skipping.
    /// </summary>
    public static bool AdminRowsRequired => Environment.GetEnvironmentVariable("VOOL_BROKER_TEST_REQUIRE_ADMIN_ROWS") == "1";

    /// <summary>Skips an admin row with its reason, or fails with that reason when the admin rows are required.</summary>
    public static void SkipOrFailAdminRow(string reason)
    {
        if (AdminRowsRequired) Assert.Fail($"A required admin row did not run: {reason}");
        Assert.Skip(reason);
    }

    /// <summary>The folder an environment variable names for an admin row, or null when it is unset.</summary>
    public static string? AdminFolder(string variable)
    {
        var path = Environment.GetEnvironmentVariable(variable);
        if (string.IsNullOrEmpty(path)) return null;
        Assert.True(Directory.Exists(path), $"{variable} names {path}, which is not a folder.");
        return path;
    }

    /// <summary>A new empty folder for a test that needs 8.3 names: on the volume VOOL_BROKER_TEST_8DOT3_ROOT names when it is set, else under TEMP.</summary>
    public static string NewShortNameDirectory()
    {
        var parent = AdminFolder("VOOL_BROKER_TEST_8DOT3_ROOT") ?? Path.GetTempPath();
        var path = Path.Combine(parent, "vool-broker-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }

    private static string BrokerExe() =>
        Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../src/VoolBroker/bin/Debug/net10.0-windows/VoolBroker.exe"));

    /// <summary>Starts the broker and leaves it waiting for its bootstrap frame on stdin.</summary>
    public static Process StartBrokerWithoutBootstrap(IReadOnlyDictionary<string, string>? environment = null)
    {
        var start = new ProcessStartInfo(BrokerExe()) { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
        foreach (var (name, value) in environment ?? new Dictionary<string, string>()) start.Environment[name] = value;
        return Process.Start(start)!;
    }

    public static void SendBootstrap(Process process, byte[] raw)
    {
        process.StandardInput.BaseStream.Write(raw);
        process.StandardInput.BaseStream.Flush();
    }

    public static Process StartBroker(object? bootstrap = null, byte[]? raw = null, IReadOnlyDictionary<string, string>? environment = null)
    {
        var process = StartBrokerWithoutBootstrap(environment);
        SendBootstrap(process, raw ?? Frame(bootstrap ?? DefaultBootstrap()));
        return process;
    }

    public static object DefaultBootstrap(string[]? flagged = null, string? journalDirectory = null) => new
    {
        secret = Convert.ToHexString(RandomNumberGenerator.GetBytes(32)).ToLowerInvariant(),
        api_pid = Environment.ProcessId,
        api_creation_time = CurrentCreationTime,
        flagged = flagged ?? Array.Empty<string>(),
        journal_dir = journalDirectory ?? Path.GetTempPath()
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

    // ---- Build 3b: roots, the walk and the channel limits ----

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool MoveFileExW(string existingFileName, string newFileName, uint flags);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool RemoveDirectoryW(string pathName);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateHardLinkW(string newFileName, string existingFileName, nint securityAttributes);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetShortPathNameW(string longPath, StringBuilder shortPath, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DeviceIoControl(Microsoft.Win32.SafeHandles.SafeFileHandle device, uint code, byte[] input, uint inputSize,
        nint output, uint outputSize, out uint returned, nint overlapped);

    private const uint FileListDirectory = 0x1;
    private const uint FileReadData = 0x1;
    private const uint GenericWrite = 0x40000000;
    private const uint BackupSemantics = 0x02000000;
    private const uint OpenReparsePoint = 0x00200000;
    private const uint FsctlSetReparsePoint = 0x000900A4;

    public static string SecretOf(object bootstrap) => (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;

    /// <summary>The broker pipes that exist now (names only), to show a refused start created none.</summary>
    public static HashSet<string> BrokerPipes() =>
        Directory.GetFiles(@"\\.\pipe\").Select(path => Path.GetFileName(path)).Where(name => name.StartsWith("vool-broker-", StringComparison.Ordinal)).ToHashSet(StringComparer.Ordinal);

    /// <summary>Starts a broker and reads its start line; the caller kills it.</summary>
    public static async Task<(Process Broker, JsonDocument Ready)> StartReady(object bootstrap, IReadOnlyDictionary<string, string>? environment = null)
    {
        var broker = StartBroker(bootstrap, environment: environment);
        var line = await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken);
        if (line is null) { broker.Kill(true); throw new InvalidOperationException("The broker wrote no start line."); }
        var ready = JsonDocument.Parse(line);
        if (!ready.RootElement.TryGetProperty("pipe", out _))
        {
            broker.Kill(true);
            throw new InvalidOperationException($"The broker refused to start: {line}");
        }
        return (broker, ready);
    }

    /// <summary>Starts a broker that must refuse; returns its one stdout line and checks it exited non-zero.</summary>
    public static async Task<JsonDocument> StartRefused(object bootstrap)
    {
        using var broker = StartBroker(bootstrap);
        // A broker that starts instead of refusing never closes its output; fail, never hang.
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(TestContext.Current.CancellationToken);
        timeout.CancelAfter(TimeSpan.FromSeconds(30));
        string output;
        try
        {
            output = await broker.StandardOutput.ReadToEndAsync(timeout.Token);
            await broker.WaitForExitAsync(timeout.Token);
        }
        catch (OperationCanceledException) when (!TestContext.Current.CancellationToken.IsCancellationRequested)
        {
            broker.Kill(true);
            throw new InvalidOperationException("The broker did not refuse to start within 30 s.");
        }
        Assert.NotEqual(0, broker.ExitCode);
        var line = Assert.Single(output.Split('\n', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));
        var document = JsonDocument.Parse(line);
        Assert.False(document.RootElement.TryGetProperty("pipe", out _));
        return document;
    }

    public static async Task Kill(Process broker)
    {
        if (!broker.HasExited) broker.Kill(true);
        // A killed broker still holds its folders until it has fully exited. One that outlives the
        // kill fails here, by name, rather than as a locked folder at the next cleanup.
        var id = broker.Id;
        try
        {
            await broker.WaitForExitAsync(TestContext.Current.CancellationToken)
                .WaitAsync(TimeSpan.FromSeconds(30), TestContext.Current.CancellationToken);
        }
        catch (TimeoutException)
        {
            Assert.Fail($"Broker process {id} did not exit within 30 seconds of being killed.");
        }
        broker.Dispose();
    }

    public static async Task<NamedPipeClientStream> ConnectAuthenticated(JsonDocument ready, string secret)
    {
        var client = await Connect(ready.RootElement.GetProperty("pipe").GetString()!, TestContext.Current.CancellationToken);
        await Authenticate(client, secret);
        return client;
    }

    public static async Task<JsonDocument> Request(Stream stream, object request)
    {
        await WriteFrame(stream, request, TestContext.Current.CancellationToken);
        return await ReadFrame(stream, TestContext.Current.CancellationToken);
    }

    /// <summary>Sends body as one frame exactly as given (for bytes a serializer would never write) and reads the reply.</summary>
    public static async Task<JsonDocument> RawRequest(Stream stream, byte[] body)
    {
        var frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32LittleEndian(frame, (uint)body.Length);
        body.CopyTo(frame.AsSpan(4));
        await stream.WriteAsync(frame, TestContext.Current.CancellationToken);
        return await ReadFrame(stream, TestContext.Current.CancellationToken);
    }

    public static string? ErrorCode(JsonDocument response) =>
        response.RootElement.TryGetProperty("error", out var error) ? error.GetProperty("code").GetString() : null;

    /// <summary>True when the broker has closed the connection: the next read ends or fails.</summary>
    public static async Task<bool> IsClosedByServer(Stream stream, TimeSpan wait)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(TestContext.Current.CancellationToken);
        timeout.CancelAfter(wait);
        var buffer = new byte[1];
        try { return await stream.ReadAsync(buffer, timeout.Token) == 0; }
        catch (IOException) { return true; }
        catch (OperationCanceledException) when (!TestContext.Current.CancellationToken.IsCancellationRequested) { return false; }
    }

    public static bool TryRename(string source, string destination) => MoveFileExW(source, destination, 0);

    public static void Rename(string source, string destination)
    {
        if (!MoveFileExW(source, destination, 0))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), $"Rename {source} -> {destination} failed.");
    }

    public static bool TryRemoveDirectory(string path) => RemoveDirectoryW(path);

    public static void HardLink(string newName, string existing)
    {
        if (!CreateHardLinkW(newName, existing, 0))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), $"CreateHardLink {newName} failed.");
    }

    /// <summary>A handle on path with data access and no sharing at all.</summary>
    public static Microsoft.Win32.SafeHandles.SafeFileHandle HoldExclusively(string path)
    {
        var handle = CreateFileW(path, FileReadData, 0, 0, OpenExisting, BackupSemantics, 0);
        if (handle.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), $"Cannot hold {path} exclusively.");
        return handle;
    }

    /// <summary>
    /// Turns an existing empty folder into a junction to target in place, as mklink /J does after it
    /// creates the folder. Opened with read and write sharing, so it works while the walk holds the folder.
    /// </summary>
    public static void TurnIntoJunction(string folder, string target)
    {
        using var handle = CreateFileW(folder, GenericWrite, 1 | 2, 0, OpenExisting, BackupSemantics | OpenReparsePoint, 0);
        if (handle.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        // The raw UTF-16 units, so a target name holding an unpaired surrogate is kept as it is.
        var substitute = MemoryMarshal.AsBytes((@"\??\" + target).AsSpan()).ToArray();
        var print = MemoryMarshal.AsBytes(target.AsSpan()).ToArray();
        var pathBytes = substitute.Length + 2 + print.Length + 2;
        var buffer = new byte[8 + 8 + pathBytes];
        BinaryPrimitives.WriteUInt32LittleEndian(buffer, 0xA0000003);
        BinaryPrimitives.WriteUInt16LittleEndian(buffer.AsSpan(4), checked((ushort)(8 + pathBytes)));
        BinaryPrimitives.WriteUInt16LittleEndian(buffer.AsSpan(8), 0);
        BinaryPrimitives.WriteUInt16LittleEndian(buffer.AsSpan(10), checked((ushort)substitute.Length));
        BinaryPrimitives.WriteUInt16LittleEndian(buffer.AsSpan(12), checked((ushort)(substitute.Length + 2)));
        BinaryPrimitives.WriteUInt16LittleEndian(buffer.AsSpan(14), checked((ushort)print.Length));
        substitute.CopyTo(buffer.AsSpan(16));
        print.CopyTo(buffer.AsSpan(16 + substitute.Length + 2));
        if (!DeviceIoControl(handle, FsctlSetReparsePoint, buffer, (uint)buffer.Length, 0, 0, out _, 0))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }

    /// <summary>A directory junction (mount point tag; no privilege needed), made by cmd's mklink /J.</summary>
    public static void CreateJunction(string link, string target)
    {
        using var mklink = Process.Start(new ProcessStartInfo("cmd.exe") { ArgumentList = { "/d", "/c", "mklink", "/J", link, target }, UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true })!;
        var output = mklink.StandardOutput.ReadToEnd() + mklink.StandardError.ReadToEnd();
        mklink.WaitForExit();
        if (mklink.ExitCode != 0 || (File.GetAttributes(link) & FileAttributes.ReparsePoint) == 0)
            throw new InvalidOperationException($"mklink /J failed ({mklink.ExitCode}): {output}");
    }

    /// <summary>A directory symlink, or a skip with the reason when this host refuses to create one.</summary>
    public static void CreateDirectorySymlinkOrSkip(string link, string target)
    {
        try { Directory.CreateSymbolicLink(link, target); }
        catch (Exception error) when (error is UnauthorizedAccessException or IOException)
        {
            Assert.Skip($"Creating a directory symlink was refused on this host ({error.Message}); it needs Developer Mode or SeCreateSymbolicLinkPrivilege.");
        }
    }

    /// <summary>Sets or clears FILE_CS_FLAG_CASE_SENSITIVE_DIR with fsutil (no admin needed on NTFS), or skips with the reason.</summary>
    public static void SetCaseSensitiveOrSkip(string directory, bool enable)
    {
        using var fsutil = Process.Start(new ProcessStartInfo("fsutil.exe") { ArgumentList = { "file", "setCaseSensitiveInfo", directory, enable ? "enable" : "disable" }, UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true })!;
        var output = fsutil.StandardOutput.ReadToEnd() + fsutil.StandardError.ReadToEnd();
        fsutil.WaitForExit();
        if (fsutil.ExitCode != 0)
        {
            if (enable) Assert.Skip($"fsutil setCaseSensitiveInfo was refused on this host ({fsutil.ExitCode}): {output.Trim()}");
            throw new InvalidOperationException($"fsutil could not clear the case-sensitive flag on {directory}: {output}");
        }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetVolumeNameForVolumeMountPointW(string volumeMountPoint, StringBuilder volumeName, uint bufferLength);

    /// <summary>The volume-GUID spelling of an existing folder on a drive letter (\\?\Volume{...}\rest); it opens the same folder.</summary>
    public static string VolumeGuidSpelling(string folder)
    {
        var root = Path.GetPathRoot(folder)!;
        var name = new StringBuilder(64);
        if (!GetVolumeNameForVolumeMountPointW(root, name, (uint)name.Capacity)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        var spelling = name.ToString() + folder[root.Length..];
        Assert.True(Directory.Exists(spelling), $"{spelling} does not open {folder}.");
        return spelling;
    }

    /// <summary>The 8.3 short form of an existing path, or null when the volume made no short name.</summary>
    public static string? ShortPath(string path)
    {
        var buffer = new StringBuilder(32768);
        var length = GetShortPathNameW(path, buffer, (uint)buffer.Capacity);
        if (length == 0 || length >= buffer.Capacity) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        var result = buffer.ToString();
        return string.Equals(Path.GetFileName(result), Path.GetFileName(path), StringComparison.OrdinalIgnoreCase) ? null : result;
    }

    /// <summary>
    /// Puts a reparse point with a non-Microsoft, non-name-surrogate tag on an empty file, or skips
    /// with the reason when this account may not set one.
    /// </summary>
    public static void SetCustomReparsePointOrSkip(string file)
    {
        using var handle = CreateFileW(file, GenericWrite, 0, 0, OpenExisting, BackupSemantics | OpenReparsePoint, 0);
        if (handle.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        var buffer = new byte[24 + 4];
        BinaryPrimitives.WriteUInt32LittleEndian(buffer, 0x00001234);
        BinaryPrimitives.WriteUInt16LittleEndian(buffer.AsSpan(4), 4);
        Guid.NewGuid().TryWriteBytes(buffer.AsSpan(8));
        if (!DeviceIoControl(handle, FsctlSetReparsePoint, buffer, (uint)buffer.Length, 0, 0, out _, 0))
        {
            var error = Marshal.GetLastWin32Error();
            Assert.Skip($"Setting a third-party reparse tag was refused on this host (Win32 error {error}: {new System.ComponentModel.Win32Exception(error).Message}).");
        }
    }

    /// <summary>
    /// A hash of a tree: every entry's relative path, kind, attributes and last-write time, each
    /// file's bytes and each link's target. Reparse points are never followed.
    /// </summary>
    public static string TreeHash(string root)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        void Add(string text) => hash.AppendData(Encoding.UTF8.GetBytes(text + "\n"));
        void Walk(string directory)
        {
            foreach (var path in Directory.EnumerateFileSystemEntries(directory).Order(StringComparer.Ordinal))
            {
                var info = new FileInfo(path);
                var attributes = info.Attributes & ~FileAttributes.Archive;
                Add($"{Path.GetRelativePath(root, path)}|{attributes}|{info.LastWriteTimeUtc.Ticks}");
                if ((attributes & FileAttributes.ReparsePoint) != 0) { Add("link:" + (info.LinkTarget ?? "")); continue; }
                if ((attributes & FileAttributes.Directory) != 0) Walk(path);
                else hash.AppendData(File.ReadAllBytes(path));
            }
        }
        Walk(root);
        return Convert.ToHexString(hash.GetHashAndReset());
    }

    /// <summary>
    /// TreeHash without folder times: a restore puts every object back with its own bytes, name,
    /// attributes and file times, but the folders it renamed things in get new last-write times.
    /// </summary>
    public static string ContentHash(string root)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        void Add(string text) => hash.AppendData(Encoding.UTF8.GetBytes(text + "\n"));
        void Walk(string directory)
        {
            foreach (var path in Directory.EnumerateFileSystemEntries(directory).Order(StringComparer.Ordinal))
            {
                var info = new FileInfo(path);
                var attributes = info.Attributes & ~FileAttributes.Archive;
                var directoryEntry = (attributes & FileAttributes.Directory) != 0;
                Add($"{Path.GetRelativePath(root, path)}|{attributes}|{(directoryEntry ? 0 : info.LastWriteTimeUtc.Ticks)}");
                if ((attributes & FileAttributes.ReparsePoint) != 0) { Add("link:" + (info.LinkTarget ?? "")); continue; }
                if (directoryEntry) Walk(path);
                else hash.AppendData(File.ReadAllBytes(path));
            }
        }
        Walk(root);
        return Convert.ToHexString(hash.GetHashAndReset());
    }

    /// <summary>Deletes a test tree; junctions and symlinks are removed, never followed.</summary>
    public static void DeleteTree(string path)
    {
        // Directory.Delete(recursive) calls DeleteVolumeMountPoint on every junction it meets, which a
        // standard user is denied: it removes the junction but then throws. So each link is removed
        // by itself, without recursion, and only real folders are descended into.
        FileAttributes attributes;
        try { attributes = File.GetAttributes(path); }
        catch (Exception error) when (error is FileNotFoundException or DirectoryNotFoundException) { return; }
        var isDirectory = (attributes & FileAttributes.Directory) != 0;
        if ((attributes & FileAttributes.ReparsePoint) == 0 && isDirectory)
            foreach (var child in Directory.GetFileSystemEntries(path)) DeleteTree(child);
        if ((attributes & FileAttributes.ReadOnly) != 0) File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
        // Another program (a virus scanner on a hosted runner) can hold a just-written folder for a
        // moment, which fails the delete with a sharing violation. Each such failure names what holds
        // the path in the test output. A VOOL broker holding it is a bug and fails at once; any other
        // holder is waited out for a few seconds, and still fails the test, by name, after that.
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(5);
        for (var attempt = 1; ; attempt++)
        {
            try
            {
                if (isDirectory) Directory.Delete(path, recursive: false);
                else File.Delete(path);
                return;
            }
            catch (IOException error) when (error.HResult == SharingViolation)
            {
                var holders = Holders(path);
                var named = holders.Count == 0 ? "no process named by the Restart Manager" : string.Join(", ", holders.Select(holder => $"{holder.Name} (process {holder.Id})"));
                var brokers = Process.GetProcessesByName("VoolBroker");
                var running = brokers.Length == 0 ? "none" : string.Join(", ", brokers.Select(broker => broker.Id));
                foreach (var broker in brokers) broker.Dispose();
                var diagnosis = $"{path} is in use: held by {named}; VoolBroker processes running: {running}";
                TestContext.Current.TestOutputHelper?.WriteLine($"Delete attempt {attempt} at {DateTime.UtcNow:HH:mm:ss.fff}: {diagnosis}");
                if (holders.Any(holder => holder.Id == Environment.ProcessId || holder.Name.Equals("VoolBroker", StringComparison.OrdinalIgnoreCase)))
                    throw new IOException($"A VOOL broker still holds a folder it should have let go of. {diagnosis}", error);
                if (DateTime.UtcNow >= deadline)
                    throw new IOException($"Still in use after {attempt} attempts over 5 seconds. {diagnosis}", error);
                Thread.Sleep(100);
            }
        }
    }

    // HRESULT_FROM_WIN32(ERROR_SHARING_VIOLATION).
    private const int SharingViolation = unchecked((int)0x80070020);
    private const int ErrorMoreData = 234;

    // The processes the Restart Manager sees holding a path. The test process itself counts as a
    // broker here, because it hosts the in-process roots. An empty list means none were named,
    // not that nothing holds the path.
    private static List<(int Id, string Name)> Holders(string path)
    {
        var holders = new List<(int Id, string Name)>();
        if (RmStartSession(out var session, 0, new char[RmSessionKeyLength + 1]) != 0) return holders;
        try
        {
            if (RmRegisterResources(session, 1, [path], 0, 0, 0, 0) != 0) return holders;
            uint count = 0;
            RmProcessInfo[]? processes = null;
            int result;
            while ((result = RmGetList(session, out var needed, ref count, processes, out _)) == ErrorMoreData)
            {
                processes = new RmProcessInfo[needed];
                count = needed;
            }
            if (result != 0 || processes is null) return holders;
            for (var index = 0; index < count; index++)
            {
                var id = processes[index].ProcessId;
                string name;
                try { using var process = Process.GetProcessById(id); name = process.ProcessName; }
                catch (Exception lookup) when (lookup is ArgumentException or InvalidOperationException) { name = processes[index].AppName; }
                holders.Add((id, id == Environment.ProcessId ? "this test process" : name));
            }
        }
        finally
        {
            _ = RmEndSession(session);
        }
        return holders;
    }

    private const int RmSessionKeyLength = 32;

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmStartSession(out uint session, int flags, char[] sessionKey);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmRegisterResources(uint session, uint fileCount, string[] files,
        uint applicationCount, nint applications, uint serviceCount, nint services);

    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    private static extern int RmGetList(uint session, out uint needed, ref uint count,
        [In, Out] RmProcessInfo[]? processes, out uint rebootReasons);

    [DllImport("rstrtmgr.dll")]
    private static extern int RmEndSession(uint session);

    // RM_PROCESS_INFO. FILETIME is two DWORDs (4-byte aligned), so the start time is two fields.
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct RmProcessInfo
    {
        public int ProcessId;
        public uint StartTimeLow;
        public uint StartTimeHigh;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string AppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string ServiceShortName;
        public int ApplicationType;
        public uint AppStatus;
        public uint TerminalServicesSessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool Restartable;
    }
}

/// <summary>
/// In process: a flagged folder with an opened root on itself, a journal folder outside it, and a
/// batch runner, as the broker sets them up. Restart releases everything and opens it again, as a
/// broker restart does.
/// </summary>
internal sealed class Workspace : IDisposable
{
    public Workspace()
    {
        JournalFolder = Path.Combine(Outside, "journal");
        Start();
    }

    public string Root { get; } = TestSupport.NewTempDirectory();
    public string Outside { get; } = TestSupport.NewTempDirectory();
    public string JournalFolder { get; }
    public RootRegistry Registry { get; private set; }
    public OpenedRoot Opened { get; private set; }
    public Journal Journal { get; private set; }
    public BatchRunner Runner { get; private set; }

    [MemberNotNull(nameof(Registry), nameof(Opened), nameof(Journal), nameof(Runner))]
    private void Start()
    {
        Registry = new RootRegistry();
        Opened = Registry.GetOpenedRoot(Registry.OpenRoot(Registry.AddFlaggedRoot(Root), ""));
        Journal = Journal.Open(JournalFolder);
        Runner = new BatchRunner(Registry, Journal);
    }

    public void Restart()
    {
        Registry.Dispose();
        Start();
    }

    /// <summary>Restart with the flagged folder emptied; the registry holds the folder, so it is released first.</summary>
    public void Reset()
    {
        Registry.Dispose();
        TestSupport.DeleteTree(Root);
        Directory.CreateDirectory(Root);
        Start();
    }

    public string In(params string[] parts) => Path.Combine([Root, .. parts]);

    public FileChange Write(string rel, string text, string expect = "any") => new("write_file", Opened, rel, Encoding.UTF8.GetBytes(text), expect);
    public FileChange Ensure(string rel) => new("ensure_directory", Opened, rel);
    public FileChange Delete(string rel) => new("delete", Opened, rel);
    public FileChange Rename(string from, string to, bool replace = false) => new("rename", Opened, from, ToPath: to, Replace: replace);

    public string Refusal(params FileChange[] changes) => Assert.Throws<BrokerRefusal>(() => Runner.Apply(changes)).Code;
    public BatchStopped Stopped(params FileChange[] changes) => Assert.Throws<BatchStopped>(() => Runner.Apply(changes));

    public string[] JournalFiles() => Directory.Exists(JournalFolder) ? Directory.GetFiles(JournalFolder) : [];

    /// <summary>Temp and aside names the broker uses, anywhere under the flagged folder.</summary>
    public string[] BrokerNames() => Directory.EnumerateFileSystemEntries(Root, ".vool-broker-*", SearchOption.AllDirectories).ToArray();

    public static string Sha256(string text) => "sha256:" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text))).ToLowerInvariant();

    public void Dispose()
    {
        BatchRunner.BeforeStepForTests = null;
        BatchRunner.AfterPlanForTests = null;
        BatchRunner.AfterCommitRecordForTests = null;
        Registry.Dispose();
        TestSupport.DeleteTree(Root);
        TestSupport.DeleteTree(Outside);
    }
}
