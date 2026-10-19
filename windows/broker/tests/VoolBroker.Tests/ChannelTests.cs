using System.Buffers.Binary;
using Xunit;
using VoolBroker.Core;
using System.Diagnostics;
using System.Text.Json;
using System.Security.Principal;

namespace VoolBroker.Tests;

public sealed class ChannelTests
{
    [Fact]
    public async Task Real_broker_authenticates_and_answers_ping_and_unknown_operation()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBroker(bootstrap);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var pipeName = ready.RootElement.GetProperty("pipe").GetString()!;
            await using var pipe = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
            await TestSupport.Authenticate(pipe, secret);

            await using var second = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
            await TestSupport.Authenticate(second, secret);
            await TestSupport.WriteFrame(pipe, new { id = "ping-1", op = "ping" }, TestContext.Current.CancellationToken);
            await TestSupport.WriteFrame(second, new { id = "unknown-1", op = "not_yet" }, TestContext.Current.CancellationToken);
            using var ping = await TestSupport.ReadFrame(pipe, TestContext.Current.CancellationToken);
            Assert.Equal("ping-1", ping.RootElement.GetProperty("id").GetString());
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
            using var refused = await TestSupport.ReadFrame(second, TestContext.Current.CancellationToken);
            Assert.Equal("unknown_op", refused.RootElement.GetProperty("error").GetProperty("code").GetString());
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Bootstrap_refuses_nonempty_roots_without_starting_pipe()
    {
        var value = new
        {
            secret = new string('a', 64), api_pid = Environment.ProcessId,
            api_creation_time = TestSupport.CurrentCreationTime,
            flagged = new[] { Path.GetTempPath() }, journal_dir = Path.GetTempPath()
        };
        using var broker = TestSupport.StartBroker(value);
        var line = await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken);
        using var refusal = JsonDocument.Parse(line!);
        Assert.Equal("roots_not_implemented", refusal.RootElement.GetProperty("error").GetProperty("code").GetString());
        await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
        Assert.NotEqual(0, broker.ExitCode);
    }

    [Fact]
    public async Task Bootstrap_refuses_missing_or_extra_properties()
    {
        foreach (var value in new object[]
        {
            new { secret = new string('a', 64) },
            new { secret = new string('a', 64), api_pid = Environment.ProcessId,
                api_creation_time = Process.GetCurrentProcess().StartTime.ToUniversalTime().ToFileTimeUtc(),
                flagged = Array.Empty<string>(), journal_dir = Path.GetTempPath(), extra = true }
        })
        {
            using var broker = TestSupport.StartBroker(value);
            using var refusal = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            Assert.Equal("invalid_bootstrap", refusal.RootElement.GetProperty("error").GetProperty("code").GetString());
            await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
            Assert.NotEqual(0, broker.ExitCode);
        }
    }

    [Fact]
    public async Task Bootstrap_refuses_short_secret_and_oversized_frame()
    {
        var malformed = new
        {
            secret = "abcd", api_pid = Environment.ProcessId, api_creation_time = TestSupport.CurrentCreationTime,
            flagged = Array.Empty<string>(), journal_dir = Path.GetTempPath()
        };
        using (var broker = TestSupport.StartBroker(malformed))
        {
            using var refusal = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            Assert.Equal("invalid_bootstrap", refusal.RootElement.GetProperty("error").GetProperty("code").GetString());
            await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
        }

        var header = new byte[4];
        BinaryPrimitives.WriteUInt32LittleEndian(header, 64 * 1024 + 1);
        using var oversized = TestSupport.StartBroker(raw: header);
        using var oversizeRefusal = JsonDocument.Parse((await oversized.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
        Assert.Equal("invalid_bootstrap", oversizeRefusal.RootElement.GetProperty("error").GetProperty("code").GetString());
        await oversized.WaitForExitAsync(TestContext.Current.CancellationToken);
    }

    [Fact]
    public async Task Wrong_creation_time_is_rejected_before_challenge()
    {
        var value = new
        {
            secret = new string('a', 64), api_pid = Environment.ProcessId,
            api_creation_time = TestSupport.CurrentCreationTime + 1, flagged = Array.Empty<string>(), journal_dir = Path.GetTempPath()
        };
        using var broker = TestSupport.StartBroker(value);
        using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
        await using var client = await TestSupport.Connect(ready.RootElement.GetProperty("pipe").GetString()!, TestContext.Current.CancellationToken);
        await Assert.ThrowsAsync<EndOfStreamException>(() => TestSupport.ReadFrame(client, TestContext.Current.CancellationToken));
        broker.Kill(true);
        await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
    }

    [Fact]
    public async Task Client_in_another_process_is_rejected()
    {
        using var api = Process.Start(new ProcessStartInfo("cmd.exe", "/c timeout /t 30 /nobreak > nul") { UseShellExecute = false, CreateNoWindow = true })!;
        var value = new
        {
            secret = new string('a', 64), api_pid = api.Id, api_creation_time = TestSupport.CreationTime(api),
            flagged = Array.Empty<string>(), journal_dir = Path.GetTempPath()
        };
        using var broker = TestSupport.StartBroker(value);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            await using var client = await TestSupport.Connect(ready.RootElement.GetProperty("pipe").GetString()!, TestContext.Current.CancellationToken);
            await Assert.ThrowsAsync<EndOfStreamException>(() => TestSupport.ReadFrame(client, TestContext.Current.CancellationToken));
        }
        finally
        {
            if (!api.HasExited) api.Kill(true);
            await api.WaitForExitAsync(TestContext.Current.CancellationToken);
            broker.Kill(true);
            await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
        }
    }

    [Fact]
    public async Task Pipe_acl_has_only_current_user_allow_and_network_deny()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        using var broker = TestSupport.StartBroker(bootstrap);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            await using var client = await TestSupport.Connect(ready.RootElement.GetProperty("pipe").GetString()!, TestContext.Current.CancellationToken);
            Assert.Equal(ready.RootElement.GetProperty("broker_pid").GetUInt32(), TestSupport.PipeServerPid(client));
            var acl = TestSupport.ReadPipeDacl(client);
            Assert.Equal(2, acl.Length);
            Assert.Contains(acl, ace => ace.Type == 1 && ace.Sid == "S-1-5-2");
            Assert.Contains(acl, ace => ace.Type == 0 && ace.Sid == WindowsIdentity.GetCurrent().User!.Value);
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Broker_proof_does_not_match_a_different_secret_and_windows_instance_creation_is_observed()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBroker(bootstrap);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var fullName = ready.RootElement.GetProperty("pipe").GetString()!;
            var name = fullName[@"\\.\pipe\".Length..];
            var instanceCreated = false;
            try
            {
                using var secondInstance = new System.IO.Pipes.NamedPipeServerStream(name, System.IO.Pipes.PipeDirection.InOut, 16, System.IO.Pipes.PipeTransmissionMode.Byte, System.IO.Pipes.PipeOptions.None);
                instanceCreated = true;
            }
            catch (IOException) { }
            Console.WriteLine($"Windows allowed a second server instance: {instanceCreated}");
            Assert.True(instanceCreated, "Windows refused a second instance of the existing pipe name.");

            await using var client = await TestSupport.Connect(fullName, TestContext.Current.CancellationToken);
            using var challenge = await TestSupport.ReadFrame(client, TestContext.Current.CancellationToken);
            var brokerNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
            var clientNonce = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
            var key = Convert.FromHexString(secret);
            await TestSupport.WriteFrame(client, new { type = "client_proof", nonce = Convert.ToHexString(clientNonce).ToLowerInvariant(), hmac = Convert.ToHexString(System.Security.Cryptography.HMACSHA256.HashData(key, brokerNonce)).ToLowerInvariant() }, TestContext.Current.CancellationToken);
            using var proof = await TestSupport.ReadFrame(client, TestContext.Current.CancellationToken);
            var expectedWrong = System.Security.Cryptography.HMACSHA256.HashData(new byte[32], clientNonce);
            Assert.NotEqual(Convert.ToHexString(expectedWrong).ToLowerInvariant(), proof.RootElement.GetProperty("hmac").GetString());
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Broker_exits_when_api_process_exits()
    {
        using var api = Process.Start(new ProcessStartInfo("cmd.exe", "/c timeout /t 3 /nobreak > nul") { UseShellExecute = false, CreateNoWindow = true })!;
        var value = new
        {
            secret = new string('a', 64), api_pid = api.Id, api_creation_time = TestSupport.CreationTime(api),
            flagged = Array.Empty<string>(), journal_dir = Path.GetTempPath()
        };
        using var broker = TestSupport.StartBroker(value);
        try
        {
            _ = await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken);
            await api.WaitForExitAsync(TestContext.Current.CancellationToken);
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(timeout.Token, TestContext.Current.CancellationToken);
            await broker.WaitForExitAsync(linked.Token);
            Assert.Equal(0, broker.ExitCode);
        }
        finally
        {
            if (!api.HasExited) api.Kill(true);
            await api.WaitForExitAsync(TestContext.Current.CancellationToken);
            if (!broker.HasExited) { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
        }
    }

    [Fact]
    public async Task Wrong_secret_and_replayed_client_nonce_are_rejected()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBroker(bootstrap);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var name = ready.RootElement.GetProperty("pipe").GetString()!;
            var key = Convert.FromHexString(secret);
            var nonce = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);

            await using (var wrong = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                using var challenge = await TestSupport.ReadFrame(wrong, TestContext.Current.CancellationToken);
                var brokerNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
                await TestSupport.WriteFrame(wrong, new { type = "client_proof", nonce = Convert.ToHexString(nonce).ToLowerInvariant(), hmac = Convert.ToHexString(System.Security.Cryptography.HMACSHA256.HashData(new byte[32], brokerNonce)).ToLowerInvariant() }, TestContext.Current.CancellationToken);
                await Assert.ThrowsAsync<EndOfStreamException>(() => TestSupport.ReadFrame(wrong, TestContext.Current.CancellationToken));
            }

            await using (var first = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                await TestSupport.AuthenticateWithNonce(first, secret, nonce);
                await TestSupport.WriteFrame(first, new { id = "once", op = "ping" }, TestContext.Current.CancellationToken);
                using var response = await TestSupport.ReadFrame(first, TestContext.Current.CancellationToken);
                Assert.True(response.RootElement.GetProperty("ok").GetBoolean());
            }
            await using (var replay = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                using var challenge = await TestSupport.ReadFrame(replay, TestContext.Current.CancellationToken);
                var brokerNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
                await TestSupport.WriteFrame(replay, new { type = "client_proof", nonce = Convert.ToHexString(nonce).ToLowerInvariant(), hmac = Convert.ToHexString(System.Security.Cryptography.HMACSHA256.HashData(key, brokerNonce)).ToLowerInvariant() }, TestContext.Current.CancellationToken);
                await Assert.ThrowsAsync<EndOfStreamException>(() => TestSupport.ReadFrame(replay, TestContext.Current.CancellationToken));
            }
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Oversized_request_is_refused_without_body_and_bad_frames_only_close_the_client()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBroker(bootstrap);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var name = ready.RootElement.GetProperty("pipe").GetString()!;
            await using (var oversized = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                await TestSupport.Authenticate(oversized, secret);
                var header = new byte[4];
                BinaryPrimitives.WriteUInt32LittleEndian(header, 64 * 1024 * 1024 + 1);
                await oversized.WriteAsync(header, TestContext.Current.CancellationToken);
                using var refusal = await TestSupport.ReadFrame(oversized, TestContext.Current.CancellationToken);
                Assert.Equal("too_large", refusal.RootElement.GetProperty("error").GetProperty("code").GetString());
            }

            await using (var malformed = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                await TestSupport.Authenticate(malformed, secret);
                await malformed.WriteAsync(new byte[] { 2, 0, 0, 0, (byte)'{', (byte)'!' }, TestContext.Current.CancellationToken);
                await Assert.ThrowsAsync<EndOfStreamException>(() => TestSupport.ReadFrame(malformed, TestContext.Current.CancellationToken));
            }

            await using (var truncated = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                await TestSupport.Authenticate(truncated, secret);
                var pendingRead = TestSupport.ReadFrame(truncated, TestContext.Current.CancellationToken);
                await truncated.WriteAsync(new byte[] { 10, 0, 0, 0, (byte)'{', (byte)'}' }, TestContext.Current.CancellationToken);
                await truncated.DisposeAsync();
                await Assert.ThrowsAsync<EndOfStreamException>(() => pendingRead);
            }

            await using var healthy = await TestSupport.Connect(name, TestContext.Current.CancellationToken);
            await TestSupport.Authenticate(healthy, secret);
            await TestSupport.WriteFrame(healthy, new { id = "after-bad-frame", op = "ping" }, TestContext.Current.CancellationToken);
            using var response = await TestSupport.ReadFrame(healthy, TestContext.Current.CancellationToken);
            Assert.True(response.RootElement.GetProperty("ok").GetBoolean());
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Broker_exits_when_bootstrap_stdin_closes()
    {
        using var broker = TestSupport.StartBroker();
        _ = await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken);
        broker.StandardInput.Close();
        await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
        Assert.Equal(0, broker.ExitCode);
    }

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
