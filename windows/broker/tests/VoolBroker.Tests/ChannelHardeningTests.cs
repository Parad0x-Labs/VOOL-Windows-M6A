using System.Buffers.Binary;
using System.Security.Cryptography;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// The connection's own limits before authentication (review F10): the only frame read before
/// the client has proved itself is small, and a client nonce is used once whatever its spelling.
/// </summary>
public sealed class ChannelHardeningTests : IDisposable
{
    private readonly string flagged = TestSupport.NewTempDirectory();
    private readonly string journalParent = TestSupport.NewTempDirectory();

    public void Dispose()
    {
        TestSupport.DeleteTree(flagged);
        TestSupport.DeleteTree(journalParent);
    }

    // A client_proof frame over 1 KiB is refused from its length alone: the connection closes at
    // once, without the broker waiting for (or reading) its body.
    [Fact]
    public async Task A_large_frame_before_authentication_closes_the_connection_at_once()
    {
        var bootstrap = TestSupport.DefaultBootstrap([flagged], Path.Combine(journalParent, "journal"));
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            await using var client = await TestSupport.Connect(ready.RootElement.GetProperty("pipe").GetString()!, TestContext.Current.CancellationToken);
            using (var challenge = await TestSupport.ReadFrame(client, TestContext.Current.CancellationToken))
                Assert.Equal("challenge", challenge.RootElement.GetProperty("type").GetString());
            var header = new byte[4];
            BinaryPrimitives.WriteUInt32LittleEndian(header, 1025);
            await client.WriteAsync(header, TestContext.Current.CancellationToken);
            await client.FlushAsync(TestContext.Current.CancellationToken);
            // Well inside the 30-second idle timeout, which would close a connection waiting for a body.
            Assert.True(await TestSupport.IsClosedByServer(client, TimeSpan.FromSeconds(5)), "The broker waited for the body of an oversized proof frame.");

            await using var next = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var ping = await TestSupport.Request(next, new { id = "p", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }

    // A nonce already used is refused when it comes back in upper case.
    [Fact]
    public async Task A_client_nonce_is_used_once_in_any_spelling()
    {
        var bootstrap = TestSupport.DefaultBootstrap([flagged], Path.Combine(journalParent, "journal"));
        var secret = TestSupport.SecretOf(bootstrap);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var pipe = ready.RootElement.GetProperty("pipe").GetString()!;
            var nonce = RandomNumberGenerator.GetBytes(32);
            await using (var first = await TestSupport.Connect(pipe, TestContext.Current.CancellationToken))
                await TestSupport.AuthenticateWithNonce(first, secret, nonce);

            await using var second = await TestSupport.Connect(pipe, TestContext.Current.CancellationToken);
            using var challenge = await TestSupport.ReadFrame(second, TestContext.Current.CancellationToken);
            var serverNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
            var key = Convert.FromHexString(secret);
            await TestSupport.WriteFrame(second, new
            {
                type = "client_proof",
                nonce = Convert.ToHexString(nonce).ToUpperInvariant(),
                hmac = Convert.ToHexString(HMACSHA256.HashData(key, serverNonce)).ToLowerInvariant()
            }, TestContext.Current.CancellationToken);
            Assert.True(await TestSupport.IsClosedByServer(second, TimeSpan.FromSeconds(5)), "A nonce already used was accepted in upper case.");
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }
}
