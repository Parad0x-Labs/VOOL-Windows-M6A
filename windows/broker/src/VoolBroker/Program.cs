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
            if (root.GetProperty("flagged").GetArrayLength() != 0) return Refuse("roots_not_implemented");
            var secret = Convert.FromHexString(secretText);
            channel = new Channel(pid, creationTime, secret, root.GetProperty("journal_dir").GetString()!);
            using var initialPipe = channel.CreateInitialPipe();
            using var lifetime = new CancellationTokenSource();
            var brokerPid = Environment.ProcessId;
            var brokerCreationTime = Channel.ReadCreationTime((uint)brokerPid);
            Console.Out.WriteLine(JsonSerializer.Serialize(new { pipe = channel.PipeName, broker_pid = brokerPid, broker_creation_time = brokerCreationTime }));
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
