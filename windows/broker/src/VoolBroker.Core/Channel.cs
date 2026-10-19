using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Json;

namespace VoolBroker.Core;

public sealed class Channel(int apiPid, long apiCreationTime, byte[] secret, string journalDirectory, RootRegistry? roots = null, TimeSpan? idleTimeout = null)
{
    private readonly HashSet<string> _nonces = new(StringComparer.Ordinal);
    private readonly object _nonceLock = new();
    private readonly byte[] _secret = secret;
    private const int MaxInstances = 16;
    // ERROR_NO_DATA: the client connected and closed before ConnectNamedPipe; drop that instance.
    private const int ErrorNoData = 232;

    /// <summary>How long the broker waits for a client's next frame (or for a write to it) before it closes the connection.</summary>
    public static readonly TimeSpan DefaultIdleTimeout = TimeSpan.FromSeconds(30);
    /// <summary>Requests one connection may make; the next one is refused and the connection closed.</summary>
    public const int MaxRequestsPerConnection = 4096;

    public string PipeName { get; } = @"\\.\pipe\vool-broker-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
    public string JournalDirectory { get; } = journalDirectory;
    public RootRegistry Roots { get; } = roots ?? new RootRegistry();
    public TimeSpan IdleTimeout { get; } = idleTimeout is { } value && value > TimeSpan.Zero && value <= DefaultIdleTimeout ? value : DefaultIdleTimeout;

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
                // The last error is read on the thread that called ConnectNamedPipe: it is per thread,
                // and reading it after the await saw another thread's value, so a client that connected
                // before ConnectNamedPipe (ERROR_PIPE_CONNECTED) stopped the whole server.
                var connect = Task.Run(() => Native.ConnectNamedPipe(pipe, 0) ? 0 : Marshal.GetLastWin32Error());
                var canceled = Task.Delay(Timeout.Infinite, cancellationToken);
                if (await Task.WhenAny(connect, canceled).ConfigureAwait(false) != connect) { pipe.Dispose(); break; }
                var error = connect.Result;
                if (error == ErrorNoData) { pipe.Dispose(); continue; }
                if (error != 0 && error != Native.ErrorPipeConnected)
                    throw new System.ComponentModel.Win32Exception(error);
            }
            catch
            {
                pipe.Dispose();
                throw;
            }
            active.Add(StartClient(pipe, cancellationToken));
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

    /// <summary>
    /// Each connection runs on its own thread with synchronous pipe I/O, so the idle watchdog can
    /// end a read or write that waits too long with CancelSynchronousIo on that thread.
    /// </summary>
    private Task StartClient(SafeFileHandle handle, CancellationToken token)
    {
        var done = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var thread = new Thread(() =>
        {
            try { HandleClient(handle, token); }
            finally { done.SetResult(); }
        }) { IsBackground = true, Name = "vool-broker-connection" };
        thread.Start();
        return done.Task;
    }

    private void HandleClient(SafeFileHandle handle, CancellationToken token)
    {
        var connectionRoots = new HashSet<string>(StringComparer.Ordinal);
        try
        {
            using var watchdog = new IdleWatchdog(IdleTimeout, token);
            using var stream = new FileStream(handle, FileAccess.ReadWrite, 0, isAsync: false);
            if (!Native.GetNamedPipeClientProcessId(handle, out var pid) || pid != apiPid || !HasCreationTime(pid, apiCreationTime))
                throw new InvalidDataException("client process identity mismatch");
            var brokerNonce = RandomNumberGenerator.GetBytes(32);
            watchdog.Run(() => Protocol.WriteFrame(stream, new { type = "challenge", nonce = Convert.ToHexString(brokerNonce).ToLowerInvariant() }));
            using (var proofDoc = watchdog.Run(() => Protocol.ReadFrame(stream)))
            {
                // Anything but a client_proof (a request sent before authentication included) closes the connection.
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
                watchdog.Run(() => Protocol.WriteFrame(stream, new { type = "server_proof", hmac = Convert.ToHexString(response).ToLowerInvariant() }));
            }
            // The connection is authenticated once; every later frame on it is a request. Request
            // ids are unique per connection, and the number of requests is capped.
            var requestIds = new HashSet<string>(StringComparer.Ordinal);
            for (var count = 0; ; count++)
            {
                JsonDocument requestDoc;
                try { requestDoc = watchdog.Run(() => Protocol.ReadFrame(stream)); }
                catch (BrokerRefusal refusal) when (refusal.Code == "too_large")
                {
                    watchdog.Run(() => Protocol.WriteFrame(stream, new { id = (string?)null, ok = false, error = new { code = "too_large", message = "Message exceeds 64 MiB." } }));
                    return;
                }
                using (requestDoc)
                {
                    var reply = Dispatch(requestDoc.RootElement, count, requestIds, connectionRoots, out var close);
                    watchdog.Run(() => Protocol.WriteFrame(stream, reply));
                    if (close) return;
                }
            }
        }
        catch (Exception error)
        {
            if (!token.IsCancellationRequested) Console.Error.WriteLine($"pipe client closed ({error.GetType().Name})");
        }
        finally
        {
            foreach (var rootId in connectionRoots)
            {
                try { Roots.CloseRoot(rootId); } catch (BrokerRefusal) { }
            }
        }
    }

    private object Dispatch(JsonElement request, int count, HashSet<string> requestIds, HashSet<string> connectionRoots, out bool close)
    {
        close = false;
        if (!request.TryGetProperty("id", out var id) || id.ValueKind is not (JsonValueKind.String or JsonValueKind.Number) ||
            !request.TryGetProperty("op", out var op) || op.ValueKind != JsonValueKind.String)
            throw new InvalidDataException("invalid request");
        var idValue = id.Clone();
        var idKey = id.GetRawText();
        if (idKey.Length > 256) throw new InvalidDataException("request id too long");
        if (count >= MaxRequestsPerConnection)
        {
            close = true;
            return Refusal(idValue, "too_many_requests", $"A connection may make at most {MaxRequestsPerConnection} requests.");
        }
        if (!requestIds.Add(idKey)) return Refusal(idValue, "replayed_request", "This request id was already used on this connection.");
        try
        {
            switch (op.GetString())
            {
                case "ping":
                    return new { id = idValue, ok = true };
                case "open_root":
                {
                    ExpectFields(request, "flagged_id", "rel");
                    var rootId = Roots.OpenRoot(StringField(request, "flagged_id"), StringField(request, "rel"));
                    connectionRoots.Add(rootId);
                    return new { id = idValue, ok = true, root_id = rootId };
                }
                case "close_root":
                {
                    ExpectFields(request, "root_id");
                    var rootId = StringField(request, "root_id");
                    if (!connectionRoots.Remove(rootId)) throw new BrokerRefusal("unknown_root", "Root id is not open on this connection.");
                    Roots.CloseRoot(rootId);
                    return new { id = idValue, ok = true };
                }
                default:
                    return Refusal(idValue, "unknown_op", "Operation is not available.");
            }
        }
        catch (BrokerRefusal refusal) { return Refusal(idValue, refusal.Code, refusal.Message); }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // Fail closed: an unexpected error refuses the request and changes nothing.
            return Refusal(idValue, "authority_check_failed", error.GetType().Name);
        }
    }

    private static object Refusal(JsonElement id, string code, string message) => new { id, ok = false, error = new { code, message } };

    private static void ExpectFields(JsonElement request, params string[] fields)
    {
        foreach (var property in request.EnumerateObject())
        {
            if (property.Name is "id" or "op" || fields.Contains(property.Name, StringComparer.Ordinal)) continue;
            throw new BrokerRefusal("invalid_request", $"Unexpected field '{property.Name}'.");
        }
    }

    private static string StringField(JsonElement request, string name) =>
        request.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()!
            : throw new BrokerRefusal("invalid_request", $"Field '{name}' must be a string.");

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

    /// <summary>
    /// Ends a connection's pipe read or write that has waited longer than the idle timeout, or any
    /// wait once the broker stops. It runs only around I/O with the client, never while a request
    /// is being worked on, so a long operation is not cut off. Created on the connection's thread.
    /// </summary>
    private sealed class IdleWatchdog : IDisposable
    {
        private readonly object sync = new();
        private readonly SafeWaitHandle thread;
        private readonly long timeoutMilliseconds;
        private readonly CancellationToken token;
        private readonly Timer timer;
        private bool waiting;
        private bool fired;
        private bool disposed;
        private long deadline;

        public IdleWatchdog(TimeSpan timeout, CancellationToken token)
        {
            thread = Native.OpenThread(Native.ThreadTerminate, false, Native.GetCurrentThreadId());
            if (thread.IsInvalid)
            {
                var error = Marshal.GetLastWin32Error();
                thread.Dispose();
                throw new System.ComponentModel.Win32Exception(error);
            }
            timeoutMilliseconds = (long)timeout.TotalMilliseconds;
            this.token = token;
            timer = new Timer(_ => Tick(), null, 50, 50);
        }

        public T Run<T>(Func<T> io)
        {
            Begin();
            try { return io(); }
            finally { End(); }
        }

        public void Run(Action io)
        {
            Begin();
            try { io(); }
            finally { End(); }
        }

        private void Begin()
        {
            lock (sync)
            {
                if (fired || token.IsCancellationRequested) throw new TimeoutException("The connection was closed by the idle watchdog.");
                waiting = true;
                deadline = Environment.TickCount64 + timeoutMilliseconds;
            }
        }

        private void End() { lock (sync) waiting = false; }

        private void Tick()
        {
            lock (sync)
            {
                if (disposed || !waiting) return;
                if (!token.IsCancellationRequested && Environment.TickCount64 < deadline) return;
                fired = true;
                // Repeated on every tick while the thread waits: a cancel that lands just before the
                // read starts has nothing to cancel, and the next tick catches the read itself.
                Native.CancelSynchronousIo(thread);
            }
        }

        public void Dispose()
        {
            lock (sync) disposed = true;
            timer.Dispose();
            thread.Dispose();
        }
    }
}
