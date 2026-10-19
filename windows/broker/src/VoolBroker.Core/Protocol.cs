using System.Buffers.Binary;
using System.Text.Json;

namespace VoolBroker.Core;

public sealed class BrokerRefusal(string code, string message) : Exception(message)
{
    public string Code { get; } = code;
}

public sealed class EffectOutcomeUnknown(string requestId) : Exception($"Outcome unknown for request {requestId}.")
{
    public string RequestId { get; } = requestId;
}

public static class Protocol
{
    public const int MaxMessageBytes = 64 * 1024 * 1024;

    public static async ValueTask<JsonDocument> ReadFrameAsync(Stream stream, CancellationToken cancellationToken = default)
    {
        var header = new byte[4];
        await ReadExactAsync(stream, header, cancellationToken).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadUInt32LittleEndian(header);
        if (length > MaxMessageBytes) throw new BrokerRefusal("too_large", "Message exceeds 64 MiB.");
        var body = new byte[length];
        await ReadExactAsync(stream, body, cancellationToken).ConfigureAwait(false);
        return Parse(body);
    }

    /// <summary>
    /// The synchronous form, for the broker's connection threads: every read runs on the calling
    /// thread, so CancelSynchronousIo on that thread ends a read that waits too long.
    /// </summary>
    public static JsonDocument ReadFrame(Stream stream)
    {
        var header = new byte[4];
        ReadExact(stream, header);
        var length = BinaryPrimitives.ReadUInt32LittleEndian(header);
        if (length > MaxMessageBytes) throw new BrokerRefusal("too_large", "Message exceeds 64 MiB.");
        var body = new byte[length];
        ReadExact(stream, body);
        return Parse(body);
    }

    public static async ValueTask WriteFrameAsync(Stream stream, object message, CancellationToken cancellationToken = default)
    {
        var frame = Encode(message);
        await stream.WriteAsync(frame, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    public static void WriteFrame(Stream stream, object message)
    {
        stream.Write(Encode(message));
        stream.Flush();
    }

    private static byte[] Encode(object message)
    {
        var body = JsonSerializer.SerializeToUtf8Bytes(message);
        if (body.Length > MaxMessageBytes) throw new BrokerRefusal("too_large", "Message exceeds 64 MiB.");
        var frame = new byte[4 + body.Length];
        BinaryPrimitives.WriteUInt32LittleEndian(frame, checked((uint)body.Length));
        body.CopyTo(frame.AsSpan(4));
        return frame;
    }

    private static JsonDocument Parse(byte[] body)
    {
        try
        {
            var document = JsonDocument.Parse(body);
            if (document.RootElement.ValueKind != JsonValueKind.Object)
            {
                document.Dispose();
                throw new BrokerRefusal("invalid_message", "Message must be a JSON object.");
            }
            return document;
        }
        catch (JsonException e)
        {
            throw new BrokerRefusal("invalid_message", e.Message);
        }
    }

    private static async ValueTask ReadExactAsync(Stream stream, Memory<byte> buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var count = await stream.ReadAsync(buffer[offset..], cancellationToken).ConfigureAwait(false);
            if (count == 0) throw new EndOfStreamException("Pipe closed during a frame.");
            offset += count;
        }
    }

    private static void ReadExact(Stream stream, Span<byte> buffer)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var count = stream.Read(buffer[offset..]);
            if (count == 0) throw new EndOfStreamException("Pipe closed during a frame.");
            offset += count;
        }
    }
}
