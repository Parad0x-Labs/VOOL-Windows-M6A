using System.Buffers.Binary;
using Xunit;
using VoolBroker.Core;

namespace VoolBroker.Tests;

public sealed class ChannelTests
{
    [Fact]
    public async Task Frame_uses_little_endian_length_and_json_object()
    {
        using var stream = new MemoryStream();
        await Protocol.WriteFrameAsync(stream, new { type = "test" }, TestContext.Current.CancellationToken);
        var bytes = stream.ToArray();
        Assert.Equal((uint)(bytes.Length - 4), BinaryPrimitives.ReadUInt32LittleEndian(bytes));
        stream.Position = 0;
        using var document = await Protocol.ReadFrameAsync(stream, TestContext.Current.CancellationToken);
        Assert.Equal("test", document.RootElement.GetProperty("type").GetString());
    }

    [Fact]
    public async Task Frame_rejects_oversize_and_truncation()
    {
        using var oversize = new MemoryStream(new byte[] { 1, 0, 0, 4 });
        var error = await Assert.ThrowsAsync<BrokerRefusal>(async () => await Protocol.ReadFrameAsync(oversize, TestContext.Current.CancellationToken));
        Assert.Equal("too_large", error.Code);
        using var truncated = new MemoryStream(new byte[] { 3, 0, 0, 0, (byte)'{', (byte)'}' });
        await Assert.ThrowsAsync<EndOfStreamException>(async () => await Protocol.ReadFrameAsync(truncated, TestContext.Current.CancellationToken));
    }
}
