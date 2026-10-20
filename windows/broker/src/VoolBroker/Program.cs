using System.Buffers.Binary;
using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using VoolBroker.Core;

namespace VoolBroker;

internal static class Program
{
    private const int MaxBootstrapBytes = 64 * 1024;

    private static async Task<int> Main()
    {
        Channel? channel = null;
        RootRegistry? roots = null;
        try
        {
            var bootstrap = await ReadBootstrapAsync().ConfigureAwait(false);
            using var document = JsonDocument.Parse(bootstrap);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !HasExactProperties(root, "secret", "api_pid", "api_creation_time", "flagged", "journal_dir"))
                return Refuse("invalid_bootstrap");
            var secretText = root.GetProperty("secret").GetString();
            if (secretText is null || secretText.Length != 64 || secretText.Any(c => !(c is >= '0' and <= '9' or >= 'a' and <= 'f')))
                return Refuse("invalid_bootstrap");
            var pidElement = root.GetProperty("api_pid");
            var timeElement = root.GetProperty("api_creation_time");
            if (!pidElement.TryGetInt32(out var pid) || pid <= 0 || !timeElement.TryGetInt64(out var creationTime) || creationTime <= 0 ||
                root.GetProperty("flagged").ValueKind != JsonValueKind.Array || root.GetProperty("journal_dir").ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(root.GetProperty("journal_dir").GetString()))
                return Refuse("invalid_bootstrap");
            roots = new RootRegistry();
            RootRegistry.ValidatePlatform();
            // One start-line entry per bootstrap entry, in bootstrap order (two spellings of one
            // folder share an id), so the API can map each of its entries to the broker's id.
            var reported = new List<Dictionary<string, string>>();
            foreach (var flagged in root.GetProperty("flagged").EnumerateArray())
            {
                if (flagged.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(flagged.GetString())) return RefuseEntry("", "invalid_flagged");
                var entry = flagged.GetString()!;
                try
                {
                    var id = roots.AddFlaggedRoot(entry);
                    reported.Add(new Dictionary<string, string> { ["id"] = id, ["path"] = roots.GetEntry(id).Path });
                }
                catch (BrokerRefusal refusal) { return RefuseEntry(entry, refusal.Code); }
            }
            var journalDirectory = root.GetProperty("journal_dir").GetString()!;
            try { roots.ValidateJournalOutsideFlagged(journalDirectory); }
            catch (BrokerRefusal refusal) { return RefuseEntry(journalDirectory, refusal.Code); }
            // The journal folder is created if needed. Without it the broker still starts, and every
            // file change is refused journal_unavailable. Batches a crash left are finished or undone
            // now, before the pipe exists; any it cannot finish stay in the journal and are reported.
            BatchRunner.PauseAtFromEnvironment();
            var journal = Journal.Open(journalDirectory);
            // Checked again now the folder exists: Open created any missing part of it, and a
            // junction made in between would have been followed.
            try { roots.ValidateJournalOutsideFlagged(journalDirectory); }
            catch (BrokerRefusal refusal) { return RefuseEntry(journalDirectory, refusal.Code); }
            var recovered = journal.RecoverAll(roots);
            var journalReport = new
            {
                available = journal.Available,
                recovered = recovered.Count(outcome => outcome.Complete),
                pending = recovered.Where(outcome => !outcome.Complete).Select(outcome => new { batch_id = outcome.BatchId, left = outcome.Left }).ToArray(),
                // Last records whose write never finished, cut off and kept in a file of their own.
                torn = recovered.Where(outcome => outcome.Torn is not null).Select(outcome => new { batch_id = outcome.BatchId, bytes = outcome.Torn!.Bytes, kept_in = outcome.Torn.KeptIn }).ToArray()
            };
            var secret = Convert.FromHexString(secretText);
            channel = new Channel(pid, creationTime, secret, journalDirectory, roots, IdleTimeoutFromEnvironment(), journal);
            using var initialPipe = channel.CreateInitialPipe();
            using var lifetime = new CancellationTokenSource();
            var brokerPid = Environment.ProcessId;
            var brokerCreationTime = Channel.ReadCreationTime((uint)brokerPid);
            Console.Out.WriteLine(JsonSerializer.Serialize(new { pipe = channel.PipeName, broker_pid = brokerPid, broker_creation_time = brokerCreationTime, flagged = reported, journal = journalReport }));
            Console.Out.Flush();
            var stdinClosed = WaitForStdinCloseAsync(lifetime.Token);
            var apiExited = WaitForApiExitAsync(pid, creationTime, lifetime.Token);
            var serving = channel.ServeAsync(initialPipe, lifetime.Token);
            await Task.WhenAny(stdinClosed, apiExited, serving).ConfigureAwait(false);
            lifetime.Cancel();
            try { await serving.ConfigureAwait(false); } catch (OperationCanceledException) { }
            return 0;
        }
        catch (Exception e) when (e is IOException or JsonException or InvalidDataException or FormatException or System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            return Refuse("invalid_bootstrap");
        }
        catch (BrokerRefusal refusal) { return Refuse(refusal.Code); }
        finally { roots?.Dispose(); }
    }

    /// <summary>
    /// VOOL_BROKER_IDLE_TIMEOUT_MS can only shorten the idle timeout (tests use it); a missing,
    /// malformed or longer value keeps the 30-second default.
    /// </summary>
    private static TimeSpan? IdleTimeoutFromEnvironment()
    {
        var text = Environment.GetEnvironmentVariable("VOOL_BROKER_IDLE_TIMEOUT_MS");
        return int.TryParse(text, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out var milliseconds)
            && milliseconds >= 100 && milliseconds < Channel.DefaultIdleTimeout.TotalMilliseconds
            ? TimeSpan.FromMilliseconds(milliseconds) : null;
    }

    private static async Task<byte[]> ReadBootstrapAsync()
    {
        var input = Console.OpenStandardInput();
        var header = new byte[4];
        await ReadExactAsync(input, header).ConfigureAwait(false);
        var size = BinaryPrimitives.ReadUInt32LittleEndian(header);
        if (size == 0 || size > MaxBootstrapBytes) throw new InvalidDataException("bootstrap length refused");
        var body = new byte[size];
        await ReadExactAsync(input, body).ConfigureAwait(false);
        return body;
    }

    private static async Task ReadExactAsync(Stream stream, byte[] data)
    {
        var offset = 0;
        while (offset < data.Length)
        {
            var read = await stream.ReadAsync(data.AsMemory(offset)).ConfigureAwait(false);
            if (read == 0) throw new EndOfStreamException();
            offset += read;
        }
    }

    private static bool HasExactProperties(JsonElement root, params string[] expected)
    {
        var actual = root.EnumerateObject().Select(property => property.Name).ToArray();
        return actual.Length == expected.Length && expected.All(name => actual.Count(value => value == name) == 1);
    }

    private static int Refuse(string code)
    {
        Console.Out.WriteLine(JsonSerializer.Serialize(new { error = new { code } }));
        Console.Out.Flush();
        return 2;
    }

    private static int RefuseEntry(string entry, string code)
    {
        Console.Out.WriteLine(JsonSerializer.Serialize(new { error = new { entry, code } }));
        Console.Out.Flush();
        return 2;
    }

    private static async Task WaitForStdinCloseAsync(CancellationToken token)
    {
        var buffer = new byte[1];
        try { while (await Console.OpenStandardInput().ReadAsync(buffer, token).ConfigureAwait(false) != 0) { } }
        catch (OperationCanceledException) { }
    }

    private static async Task WaitForApiExitAsync(int pid, long expectedCreationTime, CancellationToken token)
    {
        try
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    using var process = Process.GetProcessById(pid);
                    if (Channel.ReadCreationTime((uint)pid) != expectedCreationTime || process.HasExited) return;
                }
                catch (ArgumentException) { return; }
                await Task.Delay(100, token).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) { }
    }
}
