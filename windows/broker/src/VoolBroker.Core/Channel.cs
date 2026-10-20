using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Security.Principal;
using System.Text.Unicode;
using System.Text.Json;

namespace VoolBroker.Core;

public sealed class Channel(int apiPid, long apiCreationTime, byte[] secret, string journalDirectory, RootRegistry? roots = null, TimeSpan? idleTimeout = null, Journal? journal = null)
{
    /// <summary>The largest client_proof frame read before authentication.</summary>
    internal const int MaxProofBytes = 1024;
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
    /// <summary>Roots one connection may hold open at once; each holds a handle on every folder on its way.</summary>
    public const int MaxRootsPerConnection = 64;

    public string PipeName { get; } = @"\\.\pipe\vool-broker-" + Convert.ToHexString(RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();
    public string JournalDirectory { get; } = journalDirectory;
    public RootRegistry Roots { get; } = roots ?? new RootRegistry();
    public TimeSpan IdleTimeout { get; } = idleTimeout is { } value && value > TimeSpan.Zero && value <= DefaultIdleTimeout ? value : DefaultIdleTimeout;
    /// <summary>File changes; without a journal (none given) every change is refused journal_unavailable.</summary>
    private BatchRunner Batches { get { lock (_batchesLock) return _batches ??= new BatchRunner(Roots, journal ?? Journal.Unavailable(JournalDirectory)); } }
    private BatchRunner? _batches;
    private readonly object _batchesLock = new();

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
                if (error == ErrorNoData)
                {
                    // Under the cap the next instance is made before this one goes, so the name always
                    // has one; at the cap the connected instances keep it (one more would not fit).
                    if (active.Count + 1 < MaxInstances) pending = CreatePipe(false);
                    pipe.Dispose();
                    continue;
                }
                if (error != 0 && error != Native.ErrorPipeConnected)
                    throw new System.ComponentModel.Win32Exception(error);
            }
            catch
            {
                pipe.Dispose();
                throw;
            }
            // The next instance is made before this connection is served. A client the broker
            // turns away at once would otherwise close the only instance, and a client arriving
            // then would find the pipe gone (ERROR_FILE_NOT_FOUND, which WaitNamedPipe does not
            // wait out) rather than busy. At the instance cap the connected instances keep the name.
            try { if (active.Count + 1 < MaxInstances) pending = CreatePipe(false); }
            catch
            {
                pipe.Dispose();
                throw;
            }
            active.Add(StartClient(pipe, cancellationToken));
        }
        pending?.Dispose();
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
            // The proof is the only frame read before authentication: a few hundred bytes, so a
            // larger one is refused without reading it.
            using (var proofDoc = watchdog.Run(() => Protocol.ReadFrame(stream, MaxProofBytes)))
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
                    // Keyed on the nonce's bytes, so another spelling of the same hex is a replay too.
                    if (!_nonces.Add(Convert.ToHexString(clientNonce))) throw new InvalidDataException("replayed client nonce");
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
                    try { watchdog.Run(() => Protocol.WriteFrame(stream, reply)); }
                    catch (BrokerRefusal refusal) when (refusal.Code is "too_large" or "reply_unencodable")
                    {
                        // The reply could not be encoded (over 64 MiB, or text that is not valid
                        // Unicode); nothing of it was written. The request may have run, so this is
                        // never a plain refusal: the client treats it as outcome unknown.
                        var id = requestDoc.RootElement.GetProperty("id").Clone();
                        watchdog.Run(() => Protocol.WriteFrame(stream, Refusal(id, "reply_unencodable", "The reply could not be encoded; the request's outcome is unknown.")));
                    }
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
                    ExpectFields(request);
                    return new { id = idValue, ok = true };
                case "open_root":
                {
                    ExpectFields(request, "flagged_id", "rel");
                    if (connectionRoots.Count >= MaxRootsPerConnection)
                        throw new BrokerRefusal("too_many_roots", $"A connection may hold at most {MaxRootsPerConnection} roots open; close one first.");
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
                case "stat":
                {
                    ExpectFields(request, "root_id", "rel");
                    var stat = FileOps.Stat(RootOf(request, "root_id", connectionRoots), StringField(request, "rel"));
                    return new { id = idValue, ok = true, kind = stat.Kind, size = stat.Size, file_id = stat.FileId, read_only = stat.ReadOnly };
                }
                case "read":
                {
                    ExpectFields(request, "root_id", "rel", "max_bytes");
                    var (data, sha256) = FileOps.Read(RootOf(request, "root_id", connectionRoots), StringField(request, "rel"), IntField(request, "max_bytes"));
                    return new { id = idValue, ok = true, data = Convert.ToBase64String(data), size = data.Length, sha256 };
                }
                case "write_file" or "ensure_directory" or "delete" or "rename":
                {
                    var root = RootOf(request, "root_id", connectionRoots);
                    return Applied(idValue, Batches.Apply([ChangeFrom(request, root, connectionRoots, inBatch: false)]));
                }
                case "apply_batch":
                {
                    ExpectFields(request, "root_id", "ops");
                    var root = RootOf(request, "root_id", connectionRoots);
                    if (!request.TryGetProperty("ops", out var ops) || ops.ValueKind != JsonValueKind.Array)
                        throw new BrokerRefusal("invalid_request", "Field 'ops' must be an array.");
                    if (ops.GetArrayLength() is 0 or > BatchRunner.MaxOperations)
                        throw new BrokerRefusal("invalid_request", $"A batch holds 1 to {BatchRunner.MaxOperations} operations.");
                    var changes = ops.EnumerateArray().Select(element => element.ValueKind == JsonValueKind.Object
                        ? ChangeFrom(element, root, connectionRoots, inBatch: true)
                        : throw new BrokerRefusal("invalid_request", "Each batch operation must be an object.")).ToArray();
                    return Applied(idValue, Batches.Apply(changes));
                }
                case "restore_batch":
                {
                    ExpectFields(request, "batch_id");
                    var outcome = Batches.Restore(StringField(request, "batch_id"));
                    if (!outcome.Complete)
                        return new { id = idValue, ok = false, error = new { code = "restore_incomplete", message = "Some objects could not be put back." }, batch = new { batch_id = outcome.BatchId, ran = (int?)null, restored = false, left = outcome.Left } };
                    return new { id = idValue, ok = true, batch_id = outcome.BatchId, committed = outcome.Committed };
                }
                default:
                    return Refusal(idValue, "unknown_op", "Operation is not available.");
            }
        }
        catch (BrokerRefusal refusal) { return Refusal(idValue, refusal.Code, refusal.Message); }
        catch (BatchStopped stopped) { return StoppedReply(idValue, stopped); }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // Fail closed: an unexpected error refuses the request and changes nothing.
            return Refusal(idValue, "authority_check_failed", error.GetType().Name);
        }
    }

    private static object Refusal(JsonElement id, string code, string message) => new { id, ok = false, error = new { code, message } };

    /// <summary>
    /// A batch that made changes and then stopped: not a plain refusal. Restored, the code is what
    /// stopped it and nothing it changed is left changed. Not fully restored, the code is
    /// restore_incomplete (cause holds what stopped it): the workspace is not as it was, so the
    /// caller must not treat it as refused or retry it, and restore_batch can finish the restore.
    /// </summary>
    internal static object StoppedReply(JsonElement id, BatchStopped stopped) => new
    {
        id,
        ok = false,
        error = new
        {
            code = stopped.Restored ? stopped.Code : "restore_incomplete",
            message = stopped.Restored ? stopped.Message : $"The batch stopped ({stopped.Code}: {stopped.Message}) and could not be fully restored.",
            cause = stopped.Code
        },
        batch = new { batch_id = stopped.BatchId, ran = (int?)stopped.Ran, restored = stopped.Restored, left = stopped.Left }
    };

    private static object Applied(JsonElement id, BatchResult result) =>
        new { id, ok = true, batch_id = result.BatchId, results = result.Results, cleanup_left = result.CleanupLeft };

    /// <summary>A root this connection opened; another connection's root id is unknown here.</summary>
    private OpenedRoot RootOf(JsonElement request, string field, HashSet<string> connectionRoots)
    {
        var rootId = StringField(request, field);
        if (!connectionRoots.Contains(rootId)) throw new BrokerRefusal("unknown_root", "Root id is not open on this connection.");
        return Roots.GetOpenedRoot(rootId);
    }

    /// <summary>One change from a request (a single operation) or from an element of apply_batch's ops.</summary>
    private FileChange ChangeFrom(JsonElement element, OpenedRoot root, HashSet<string> connectionRoots, bool inBatch)
    {
        var op = StringField(element, "op");
        var common = inBatch ? new[] { "op", "rel" } : ["root_id", "rel"];
        switch (op)
        {
            case "write_file":
                ExpectFields(element, inBatch, [.. common, "data", "expect"]);
                return new FileChange(op, root, StringField(element, "rel"), Base64Field(element, "data"), StringField(element, "expect"));
            case "ensure_directory" or "delete":
                ExpectFields(element, inBatch, common);
                return new FileChange(op, root, StringField(element, "rel"));
            case "rename":
            {
                string[] fields = inBatch ? [.. common, "to", "replace"] : [.. common, "to", "replace", "to_root_id"];
                ExpectFields(element, inBatch, fields);
                var toRoot = !inBatch && element.TryGetProperty("to_root_id", out _) ? RootOf(element, "to_root_id", connectionRoots) : null;
                return new FileChange(op, root, StringField(element, "rel"), ToRoot: toRoot, ToPath: StringField(element, "to"), Replace: BoolField(element, "replace"));
            }
            default:
                throw new BrokerRefusal("invalid_request", $"Unknown batch operation '{op}'.");
        }
    }

    /// <summary>A batch element has no id; a single request's id and op are checked by Dispatch.</summary>
    private static void ExpectFields(JsonElement element, bool inBatch, string[] fields)
    {
        if (!inBatch) { ExpectFields(element, fields); return; }
        foreach (var property in element.EnumerateObject())
            if (!fields.Contains(property.Name, StringComparer.Ordinal)) throw new BrokerRefusal("invalid_request", $"Unexpected field '{property.Name}'.");
    }

    private static int IntField(JsonElement request, string name) =>
        request.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.Number && value.TryGetInt32(out var number)
            ? number
            : throw new BrokerRefusal("invalid_request", $"Field '{name}' must be an integer.");

    private static bool BoolField(JsonElement request, string name) =>
        request.TryGetProperty(name, out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? value.GetBoolean()
            : throw new BrokerRefusal("invalid_request", $"Field '{name}' must be true or false.");

    private static byte[] Base64Field(JsonElement request, string name)
    {
        try { return Convert.FromBase64String(StringField(request, name)); }
        catch (FormatException) { throw new BrokerRefusal("invalid_request", $"Field '{name}' must be base64."); }
    }

    private static void ExpectFields(JsonElement request, params string[] fields)
    {
        foreach (var property in request.EnumerateObject())
        {
            if (property.Name is "id" or "op" || fields.Contains(property.Name, StringComparer.Ordinal)) continue;
            throw new BrokerRefusal("invalid_request", $"Unexpected field '{property.Name}'.");
        }
    }

    /// <summary>
    /// A string field, exactly as sent. Bytes that are not UTF-8, or an escape for a UTF-16
    /// surrogate that is not half of a pair, are refused invalid_name, never decoded to U+FFFD:
    /// a name changed that way could name a different file.
    /// </summary>
    private static string StringField(JsonElement request, string name)
    {
        if (!request.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.String)
            throw new BrokerRefusal("invalid_request", $"Field '{name}' must be a string.");
        var raw = JsonMarshal.GetRawUtf8Value(value);
        if (!Utf8.IsValid(raw) || HasUnpairedSurrogateEscape(raw))
            throw new BrokerRefusal("invalid_name", $"Field '{name}' is not valid Unicode.");
        string text;
        try { text = value.GetString()!; }
        catch (InvalidOperationException) { throw new BrokerRefusal("invalid_name", $"Field '{name}' is not valid Unicode."); }
        for (var index = 0; index < text.Length; index++)
        {
            if (char.IsHighSurrogate(text[index]) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1])) { index++; continue; }
            if (char.IsSurrogate(text[index])) throw new BrokerRefusal("invalid_name", $"Field '{name}' is not valid Unicode.");
        }
        return text;
    }

    /// <summary>True when the raw JSON string holds an escaped high surrogate not followed by an escaped low one, or a low one on its own.</summary>
    private static bool HasUnpairedSurrogateEscape(ReadOnlySpan<byte> raw)
    {
        var pendingHigh = false;
        for (var index = 0; index < raw.Length; index++)
        {
            if (raw[index] != (byte)'\\') { if (pendingHigh) return true; continue; }
            if (index + 1 >= raw.Length) return true;
            if (raw[index + 1] != (byte)'u')
            {
                if (pendingHigh) return true;
                index++;
                continue;
            }
            if (index + 5 >= raw.Length || !int.TryParse(raw.Slice(index + 2, 4), System.Globalization.NumberStyles.AllowHexSpecifier, System.Globalization.CultureInfo.InvariantCulture, out var unit))
                return true;
            index += 5;
            var high = unit is >= 0xD800 and <= 0xDBFF;
            var low = unit is >= 0xDC00 and <= 0xDFFF;
            if (pendingHigh != low) return true;
            pendingHigh = high;
        }
        return pendingHigh;
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
