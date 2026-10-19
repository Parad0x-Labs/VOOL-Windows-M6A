namespace VoolBroker.Tests;

using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Security.Principal;
using Xunit;

internal static class TestSupport
{
    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool GetProcessTimes(nint process, out long creation, out long exit, out long kernel, out long user);

    [System.Runtime.InteropServices.DllImport("advapi32.dll", SetLastError = true)]
    private static extern uint GetSecurityInfo(nint handle, int objectType, int securityInformation,
        out nint owner, out nint group, out nint dacl, out nint sacl, out nint descriptor);

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct AclSizeInformation { public uint AceCount; public uint AclBytesInUse; public uint AclBytesFree; }

    [System.Runtime.InteropServices.DllImport("advapi32.dll", SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool GetAclInformation(nint acl, out AclSizeInformation info, uint length, int informationClass);

    [System.Runtime.InteropServices.DllImport("advapi32.dll", SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool GetAce(nint acl, uint index, out nint ace);

    [System.Runtime.InteropServices.DllImport("advapi32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool ConvertSidToStringSidW(nint sid, out nint stringSid);

    [System.Runtime.InteropServices.DllImport("kernel32.dll")]
    private static extern nint LocalFree(nint memory);

    [System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)]
    [return: System.Runtime.InteropServices.MarshalAs(System.Runtime.InteropServices.UnmanagedType.Bool)]
    private static extern bool GetNamedPipeServerProcessId(nint pipe, out uint processId);

    public static long CurrentCreationTime
    {
        get
        {
            if (!GetProcessTimes(Process.GetCurrentProcess().Handle, out var creation, out _, out _, out _))
                throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
            return creation;
        }
    }

    public static long CreationTime(Process process)
    {
        if (!GetProcessTimes(process.Handle, out var creation, out _, out _, out _))
            throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
        return creation;
    }

    public static (byte Type, string Sid)[] ReadPipeDacl(NamedPipeClientStream pipe)
    {
        var result = GetSecurityInfo(pipe.SafePipeHandle.DangerousGetHandle(), 6, 4, out _, out _, out var dacl, out _, out var descriptor);
        if (result != 0) throw new System.ComponentModel.Win32Exception((int)result);
        try
        {
            if (!GetAclInformation(dacl, out var info, (uint)System.Runtime.InteropServices.Marshal.SizeOf<AclSizeInformation>(), 2))
                throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
            var entries = new (byte Type, string Sid)[info.AceCount];
            for (uint i = 0; i < info.AceCount; i++)
            {
                if (!GetAce(dacl, i, out var ace)) throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
                var type = System.Runtime.InteropServices.Marshal.ReadByte(ace);
                var sid = new nint(ace.ToInt64() + 8);
                if (!ConvertSidToStringSidW(sid, out var text)) throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
                try { entries[i] = (type, System.Runtime.InteropServices.Marshal.PtrToStringUni(text)!); }
                finally { LocalFree(text); }
            }
            return entries;
        }
        finally { LocalFree(descriptor); }
    }

    public static uint PipeServerPid(NamedPipeClientStream pipe)
    {
        if (!GetNamedPipeServerProcessId(pipe.SafePipeHandle.DangerousGetHandle(), out var pid))
            throw new System.ComponentModel.Win32Exception(System.Runtime.InteropServices.Marshal.GetLastWin32Error());
        return pid;
    }
    public static string NewTempDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), "vool-broker-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }

    public static Process StartBroker(object? bootstrap = null, byte[]? raw = null)
    {
        var exe = Path.GetFullPath(Path.Combine(AppContext.BaseDirectory, "../../../../../src/VoolBroker/bin/Debug/net10.0-windows/VoolBroker.exe"));
        var process = Process.Start(new ProcessStartInfo(exe) { UseShellExecute = false, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true })!;
        raw ??= Frame(bootstrap ?? DefaultBootstrap());
        process.StandardInput.BaseStream.Write(raw);
        process.StandardInput.BaseStream.Flush();
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
