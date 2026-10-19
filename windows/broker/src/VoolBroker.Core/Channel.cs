using Microsoft.Win32.SafeHandles;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;

namespace VoolBroker.Core;

public sealed class Channel(int apiPid, long apiCreationTime, byte[] secret, string journalDirectory)
{
    private readonly HashSet<string> _nonces = new(StringComparer.Ordinal);
    private readonly object _nonceLock = new();
    private readonly byte[] _secret = secret;
    private const int MaxInstances = 16;

    public string PipeName { get; } = @"\\.\pipe\vool-broker-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
    public string JournalDirectory { get; } = journalDirectory;

    public SafeFileHandle CreateInitialPipe() => CreatePipe(true);

    public async Task ServeAsync(SafeFileHandle initialPipe, CancellationToken cancellationToken)
    {
        var active = new List<Task>();
        var first = false;
        SafeFileHandle? pending = initialPipe;
        while (!cancellationToken.IsCancellationRequested)
        {
            active.RemoveAll(task => task.IsCompleted);
            if (active.Count >= MaxInstances)
            {
                await Task.WhenAny(active).WaitAsync(cancellationToken).ConfigureAwait(false);
                continue;
            }
            var pipe = pending ?? CreatePipe(first);
            pending = null;
            first = false;
            try
            {
                var connect = Task.Run(() => Native.ConnectNamedPipe(pipe, 0));
                var canceled = Task.Delay(Timeout.Infinite, cancellationToken);
                if (await Task.WhenAny(connect, canceled).ConfigureAwait(false) != connect) { pipe.Dispose(); break; }
                if (!connect.Result && Marshal.GetLastWin32Error() != Native.ErrorPipeConnected)
                    throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            }
            catch
            {
                pipe.Dispose();
                throw;
            }
            active.Add(HandleClientAsync(pipe, cancellationToken));
        }
        await Task.WhenAll(active).ConfigureAwait(false);
    }

    private SafeFileHandle CreatePipe(bool first)
    {
        var sid = WindowsIdentity.GetCurrent().User?.Value ?? throw new InvalidOperationException("Current user SID unavailable.");
        var sddl = $"D:(D;;GA;;;NU)(A;;GA;;;{sid})";
        if (!Native.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, out var descriptor, out _))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        try
        {
            var attributes = new Native.SecurityAttributes { Length = Marshal.SizeOf<Native.SecurityAttributes>(), SecurityDescriptor = descriptor, InheritHandle = false };
            var mode = Native.PipeTypeByte | Native.PipeReadModeByte | Native.PipeWait | Native.PipeRejectRemoteClients;
            var flags = Native.PipeAccessDuplex | (first ? Native.FileFlagFirstPipeInstance : 0);
            var pipe = Native.CreateNamedPipeW(PipeName, flags, mode, MaxInstances, 65536, 65536, 0, ref attributes);
            if (pipe.IsInvalid) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
            return pipe;
        }
        finally { Native.LocalFree(descriptor); }
    }

    private async Task HandleClientAsync(SafeFileHandle handle, CancellationToken token)
    {
        await using var stream = new FileStream(handle, FileAccess.ReadWrite, 65536, isAsync: false);
        try
        {
            if (!Native.GetNamedPipeClientProcessId(handle, out var pid) || pid != apiPid || !HasCreationTime(pid, apiCreationTime))
                throw new InvalidDataException("client process identity mismatch");
            var brokerNonce = RandomNumberGenerator.GetBytes(32);
            await Protocol.WriteFrameAsync(stream, new { type = "challenge", nonce = Convert.ToHexString(brokerNonce).ToLowerInvariant() }, token);
            using var proofDoc = await Protocol.ReadFrameAsync(stream, token);
            var proof = proofDoc.RootElement;
            if (proof.GetProperty("type").GetString() != "client_proof") throw new InvalidDataException("invalid client proof");
            var clientNonceHex = proof.GetProperty("nonce").GetString() ?? "";
            var clientNonce = Convert.FromHexString(clientNonceHex);
            if (clientNonce.Length != 32) throw new InvalidDataException("invalid client nonce");
            var expected = HMACSHA256.HashData(_secret, brokerNonce);
            if (!FixedHexEquals(proof.GetProperty("hmac").GetString(), expected)) throw new InvalidDataException("client authentication failed");
            lock (_nonceLock)
            {
                if (!_nonces.Add(clientNonceHex)) throw new InvalidDataException("replayed client nonce");
            }
            var response = HMACSHA256.HashData(_secret, clientNonce);
            await Protocol.WriteFrameAsync(stream, new { type = "server_proof", hmac = Convert.ToHexString(response).ToLowerInvariant() }, token);
            JsonDocument requestDoc;
            try { requestDoc = await Protocol.ReadFrameAsync(stream, token); }
            catch (BrokerRefusal refusal) when (refusal.Code == "too_large")
            {
                await Protocol.WriteFrameAsync(stream, new { id = (string?)null, ok = false, error = new { code = "too_large", message = "Message exceeds 64 MiB." } }, token);
                return;
            }
            using (requestDoc)
            {
            var request = requestDoc.RootElement;
            if (!request.TryGetProperty("id", out var id) || !request.TryGetProperty("op", out var op)) throw new InvalidDataException("invalid request");
            var idValue = id.Clone();
            if (op.GetString() == "ping") await Protocol.WriteFrameAsync(stream, new { id = idValue, ok = true }, token);
            else await Protocol.WriteFrameAsync(stream, new { id = idValue, ok = false, error = new { code = "unknown_op", message = "Operation is not available." } }, token);
            }
        }
        catch (Exception) when (!token.IsCancellationRequested)
        {
            Console.Error.WriteLine("pipe client rejected");
        }
    }

    private static bool HasCreationTime(uint pid, long expected)
    {
        try { return ReadCreationTime(pid) == expected; }
        catch (System.ComponentModel.Win32Exception) { return false; }
    }

    public static long ReadCreationTime(uint pid)
    {
        using var process = Native.OpenProcess(Native.ProcessQueryLimitedInformation, false, pid);
        if (process.IsInvalid || !Native.GetProcessTimes(process, out var creation, out _, out _, out _))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        return creation;
    }

    private static bool FixedHexEquals(string? value, byte[] expected)
    {
        if (value is null || value.Length != expected.Length * 2) return false;
        try { return CryptographicOperations.FixedTimeEquals(Convert.FromHexString(value), expected); }
        catch (FormatException) { return false; }
    }
}
