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
    public async Task Bootstrap_refuses_a_journal_inside_a_flagged_folder_without_starting_pipe()
    {
        // F9. The journal folder is the flagged folder itself; the start is refused before any pipe exists.
        var flagged = TestSupport.NewTempDirectory();
        try
        {
            var value = new
            {
                secret = new string('a', 64), api_pid = Environment.ProcessId,
                api_creation_time = TestSupport.CurrentCreationTime,
                flagged = new[] { flagged }, journal_dir = flagged
            };
            var pipes = TestSupport.BrokerPipes();
            using var broker = TestSupport.StartBroker(value);
            var line = await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken);
            using var refusal = JsonDocument.Parse(line!);
            Assert.Equal("journal_inside_flagged", refusal.RootElement.GetProperty("error").GetProperty("code").GetString());
            Assert.False(refusal.RootElement.TryGetProperty("pipe", out _));
            await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
            Assert.NotEqual(0, broker.ExitCode);
            Assert.Empty(TestSupport.BrokerPipes().Except(pipes));
        }
        finally { TestSupport.DeleteTree(flagged); }
    }

    [Fact]
    public async Task A_request_sent_before_authentication_closes_the_connection()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            await using (var early = await TestSupport.Connect(ready.RootElement.GetProperty("pipe").GetString()!, TestContext.Current.CancellationToken))
            {
                using var challenge = await TestSupport.ReadFrame(early, TestContext.Current.CancellationToken);
                Assert.Equal("challenge", challenge.RootElement.GetProperty("type").GetString());
                await TestSupport.WriteFrame(early, new { id = "early", op = "ping" }, TestContext.Current.CancellationToken);
                Assert.True(await TestSupport.IsClosedByServer(early, TimeSpan.FromSeconds(10)), "The broker answered a request sent before authentication.");
            }
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var ping = await TestSupport.Request(client, new { id = "after", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task A_request_id_is_accepted_once_per_connection()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            await using (var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap)))
            {
                using var first = await TestSupport.Request(client, new { id = "same", op = "ping" });
                Assert.True(first.RootElement.GetProperty("ok").GetBoolean());
                using var replay = await TestSupport.Request(client, new { id = "same", op = "ping" });
                Assert.Equal("same", replay.RootElement.GetProperty("id").GetString());
                Assert.Equal("replayed_request", TestSupport.ErrorCode(replay));
                using var replayOtherOp = await TestSupport.Request(client, new { id = "same", op = "open_root", flagged_id = "x", rel = "a" });
                Assert.Equal("replayed_request", TestSupport.ErrorCode(replayOtherOp));
                using var next = await TestSupport.Request(client, new { id = "next", op = "ping" });
                Assert.True(next.RootElement.GetProperty("ok").GetBoolean());
            }
            await using var second = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var fresh = await TestSupport.Request(second, new { id = "same", op = "ping" });
            Assert.True(fresh.RootElement.GetProperty("ok").GetBoolean());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task A_connection_is_closed_after_its_request_cap()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            await using (var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap)))
            {
                for (var i = 0; i < VoolBroker.Core.Channel.MaxRequestsPerConnection; i++)
                {
                    using var ping = await TestSupport.Request(client, new { id = $"p{i}", op = "ping" });
                    Assert.True(ping.RootElement.GetProperty("ok").GetBoolean(), $"Request {i + 1} was refused.");
                }
                using var over = await TestSupport.Request(client, new { id = "over", op = "ping" });
                Assert.Equal("too_many_requests", TestSupport.ErrorCode(over));
                Assert.True(await TestSupport.IsClosedByServer(client, TimeSpan.FromSeconds(10)));
            }
            await using var next = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var fresh = await TestSupport.Request(next, new { id = "fresh", op = "ping" });
            Assert.True(fresh.RootElement.GetProperty("ok").GetBoolean());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task Idle_connections_are_closed_and_free_their_instances()
    {
        // The broker's idle timeout, shortened for the test (the variable can only shorten it).
        var bootstrap = TestSupport.DefaultBootstrap();
        var environment = new Dictionary<string, string> { ["VOOL_BROKER_IDLE_TIMEOUT_MS"] = "2000" };
        var (broker, ready) = await TestSupport.StartReady(bootstrap, environment);
        var clients = new List<System.IO.Pipes.NamedPipeClientStream>();
        try
        {
            var pipeName = ready.RootElement.GetProperty("pipe").GetString()!;
            // Fifteen authenticated clients and one that never answers the challenge fill all 16 instances.
            for (var i = 0; i < 15; i++) clients.Add(await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap)));
            var silent = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
            clients.Add(silent);
            await Task.Delay(200, TestContext.Current.CancellationToken);
            Assert.True(TestSupport.IsPipeBusy(pipeName), "Sixteen open connections did not fill the broker's instances.");
            foreach (var client in clients.Take(15))
                Assert.True(await TestSupport.IsClosedByServer(client, TimeSpan.FromSeconds(10)), "An idle authenticated connection was not closed.");
            using (var challenge = await TestSupport.ReadFrame(silent, TestContext.Current.CancellationToken))
                Assert.Equal("challenge", challenge.RootElement.GetProperty("type").GetString());
            Assert.True(await TestSupport.IsClosedByServer(silent, TimeSpan.FromSeconds(10)), "A connection that never authenticated was not closed.");
            await using var next = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var ping = await TestSupport.Request(next, new { id = "after-idle", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
            Assert.False(broker.HasExited);
        }
        finally
        {
            foreach (var client in clients) await client.DisposeAsync();
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }

    // Clients that open and close at once while 15 connections are held. One that closes before
    // the broker's ConnectNamedPipe takes the ERROR_NO_DATA path at the instance cap, which must
    // not make a 17th instance (review of 3k). Whether a given attempt hits that moment is timing.
    [Fact]
    public async Task Clients_that_leave_at_once_at_the_instance_cap_do_not_stop_the_broker()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        var clients = new List<System.IO.Pipes.NamedPipeClientStream>();
        try
        {
            var pipeName = ready.RootElement.GetProperty("pipe").GetString()!;
            for (var i = 0; i < 15; i++) clients.Add(await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap)));
            // Few, short attempts keep the run well inside the broker's 30 s idle timeout.
            for (var i = 0; i < 50; i++)
            {
                using var leaving = new System.IO.Pipes.NamedPipeClientStream(".", pipeName[@"\\.\pipe\".Length..], System.IO.Pipes.PipeDirection.InOut);
                try { leaving.Connect(200); } catch (TimeoutException) { }
            }
            Assert.False(broker.HasExited, "Clients leaving at once stopped the broker.");
            await clients[0].DisposeAsync();
            clients.RemoveAt(0);
            clients.Add(await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap)));
            using var ping = await TestSupport.Request(clients[^1], new { id = "still-served", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
        }
        finally
        {
            foreach (var client in clients) await client.DisposeAsync();
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }

    [Fact]
    public async Task A_slow_request_outlives_the_idle_timeout()
    {
        // In process, so the walk's test hook can make open_root take longer than the idle timeout.
        var flagged = TestSupport.NewTempDirectory();
        Directory.CreateDirectory(Path.Combine(flagged, "a"));
        var secret = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
        using var roots = new VoolBroker.Core.RootRegistry();
        var flaggedId = roots.AddFlaggedRoot(flagged);
        var channel = new VoolBroker.Core.Channel(Environment.ProcessId, TestSupport.CurrentCreationTime, secret, flagged, roots, TimeSpan.FromMilliseconds(300));
        using var lifetime = new CancellationTokenSource();
        var initial = channel.CreateInitialPipe();
        // Set before ServeAsync: the hook is per async flow, and connection threads take ServeAsync's.
        VoolBroker.Core.HandleWalk.AfterComponentOpenForTests = (_, _) => Thread.Sleep(1500);
        var serving = channel.ServeAsync(initial, lifetime.Token);
        try
        {
            await using var client = await TestSupport.Connect(channel.PipeName, TestContext.Current.CancellationToken);
            await TestSupport.Authenticate(client, Convert.ToHexString(secret).ToLowerInvariant());
            var watch = System.Diagnostics.Stopwatch.StartNew();
            using var opened = await TestSupport.Request(client, new { id = "slow", op = "open_root", flagged_id = flaggedId, rel = "a" });
            Assert.True(opened.RootElement.GetProperty("ok").GetBoolean());
            Assert.True(watch.Elapsed > TimeSpan.FromMilliseconds(1000), "The request was not slow; the hook did not run.");
            VoolBroker.Core.HandleWalk.AfterComponentOpenForTests = null;
            using var ping = await TestSupport.Request(client, new { id = "after-slow", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
            // Idle again: now the timeout closes the connection, and its root is released.
            Assert.True(await TestSupport.IsClosedByServer(client, TimeSpan.FromSeconds(10)));
            var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
            while (roots.OpenedCount != 0)
            {
                Assert.True(DateTime.UtcNow < deadline, "The idle connection's root was not released.");
                await Task.Delay(50, TestContext.Current.CancellationToken);
            }
        }
        finally
        {
            VoolBroker.Core.HandleWalk.AfterComponentOpenForTests = null;
            lifetime.Cancel();
            // The server thread waiting in ConnectNamedPipe on the next instance ends only when a
            // client connects; connect one so this test leaves no blocked thread or pipe behind.
            try
            {
                await using var wake = new System.IO.Pipes.NamedPipeClientStream(".", channel.PipeName[@"\\.\pipe\".Length..], System.IO.Pipes.PipeDirection.InOut);
                await wake.ConnectAsync(2000, TestContext.Current.CancellationToken);
            }
            catch (Exception error) when (error is TimeoutException or IOException) { }
            try { await serving.WaitAsync(TimeSpan.FromSeconds(10), TestContext.Current.CancellationToken); }
            catch (OperationCanceledException) { }
            initial.Dispose();
            roots.Dispose();
            TestSupport.DeleteTree(flagged);
        }
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
        // No HasExited check here: an API whose creation time does not match is not the API the
        // broker serves, so the broker's own watch ends it shortly after start (Program.cs).
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
            Assert.False(broker.HasExited, "The broker exited instead of only closing the rejected connection.");
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
    public async Task Broker_proof_is_the_hmac_of_the_client_nonce_and_a_second_server_instance_is_logged()
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
            // Whether Windows lets another server instance of the name be created is logged, not
            // asserted: the client's server-pid check is what stands against a squatter.
            Console.WriteLine($"Windows allowed a second server instance: {instanceCreated}");

            await using var client = await TestSupport.Connect(fullName, TestContext.Current.CancellationToken);
            using var challenge = await TestSupport.ReadFrame(client, TestContext.Current.CancellationToken);
            var brokerNonce = Convert.FromHexString(challenge.RootElement.GetProperty("nonce").GetString()!);
            var clientNonce = System.Security.Cryptography.RandomNumberGenerator.GetBytes(32);
            var key = Convert.FromHexString(secret);
            await TestSupport.WriteFrame(client, new { type = "client_proof", nonce = Convert.ToHexString(clientNonce).ToLowerInvariant(), hmac = Convert.ToHexString(System.Security.Cryptography.HMACSHA256.HashData(key, brokerNonce)).ToLowerInvariant() }, TestContext.Current.CancellationToken);
            using var proof = await TestSupport.ReadFrame(client, TestContext.Current.CancellationToken);
            Assert.Equal("server_proof", proof.RootElement.GetProperty("type").GetString());
            var expected = System.Security.Cryptography.HMACSHA256.HashData(key, clientNonce);
            Assert.Equal(Convert.ToHexString(expected).ToLowerInvariant(), proof.RootElement.GetProperty("hmac").GetString());
            var underAnotherSecret = System.Security.Cryptography.HMACSHA256.HashData(new byte[32], clientNonce);
            Assert.NotEqual(Convert.ToHexString(underAnotherSecret).ToLowerInvariant(), proof.RootElement.GetProperty("hmac").GetString());
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
                Assert.Equal(JsonValueKind.Null, refusal.RootElement.GetProperty("id").ValueKind);
                Assert.False(refusal.RootElement.GetProperty("ok").GetBoolean());
                Assert.True(await TestSupport.IsClosedByServer(oversized, TimeSpan.FromSeconds(10)), "The broker kept a connection open after a too_large frame.");
            }

            await using (var malformed = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                await TestSupport.Authenticate(malformed, secret);
                await malformed.WriteAsync(new byte[] { 2, 0, 0, 0, (byte)'{', (byte)'!' }, TestContext.Current.CancellationToken);
                await Assert.ThrowsAsync<EndOfStreamException>(() => TestSupport.ReadFrame(malformed, TestContext.Current.CancellationToken));
            }

            await using (var truncated = await TestSupport.Connect(name, TestContext.Current.CancellationToken))
            {
                // A frame cut short by the client going away: only the broker's survival below is checked.
                await TestSupport.Authenticate(truncated, secret);
                await truncated.WriteAsync(new byte[] { 10, 0, 0, 0, (byte)'{', (byte)'}' }, TestContext.Current.CancellationToken);
            }

            Assert.False(broker.HasExited, "A bad frame stopped the broker.");
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

    [Fact]
    public async Task Broker_pipe_handles_are_not_inheritable()
    {
        // The broker inherits some handles at birth (its stdio, and any inheritable handle the test
        // host held then). Those are in its table before it reads the bootstrap and creates its pipe.
        // Every handle the broker creates afterwards, its pipe instances included, must be
        // non-inheritable. Only the handle table is read; no broker handle is queried.
        var fileType = TestSupport.FileObjectTypeIndex();
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBrokerWithoutBootstrap();
        try
        {
            // Wait until the runtime has started and the broker sits reading its bootstrap: handles the
            // runtime makes while starting are not the broker's own (one inheritable event appeared there).
            var atBirth = await TestSupport.SettledHandleTable(broker.Id, TestContext.Current.CancellationToken);
            Assert.Contains(atBirth.Values, entry => entry.Inheritable);

            TestSupport.SendBootstrap(broker, TestSupport.Frame(bootstrap));
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var pipeName = ready.RootElement.GetProperty("pipe").GetString()!;
            await using var pipe = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
            await TestSupport.Authenticate(pipe, secret);

            var created = TestSupport.ReadHandleTable(broker.Id)
                .Where(entry => !atBirth.TryGetValue(entry.Value, out var old) || old.GrantedAccess != entry.GrantedAccess || old.TypeIndex != entry.TypeIndex)
                .ToList();
            var pipeServers = created.Count(entry => entry.TypeIndex == fileType && entry.GrantedAccess == TestSupport.DuplexPipeServerAccess);
            Assert.True(pipeServers >= 1,
                $"Expected the broker's pipe instances among its new handles; found {pipeServers} duplex file handles among: "
                + string.Join(", ", created.Select(e => $"0x{e.Value:x} access 0x{e.GrantedAccess:x} type {e.TypeIndex}")) + $" (file type {fileType}).");
            var inheritable = created.Where(entry => entry.Inheritable).ToList();
            Assert.True(inheritable.Count == 0,
                "Handles the broker created are inheritable: " + string.Join(", ", inheritable.Select(e => $"0x{e.Value:x} access 0x{e.GrantedAccess:x} type {e.TypeIndex}")));
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Broker_keeps_serving_clients_that_connect_before_it_waits()
    {
        // Each client connects as soon as the next instance exists, often before the broker calls
        // ConnectNamedPipe, which then reports ERROR_PIPE_CONNECTED. Every one must be served.
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBroker(bootstrap);
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var pipeName = ready.RootElement.GetProperty("pipe").GetString()!;
            for (var i = 0; i < 40; i++)
            {
                await using var client = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
                try { await TestSupport.Authenticate(client, secret); }
                catch (EndOfStreamException error)
                {
                    throw new InvalidOperationException($"The broker dropped client {i + 1} of 40 before its challenge.", error);
                }
                await TestSupport.WriteFrame(client, new { id = $"early-{i}", op = "ping" }, TestContext.Current.CancellationToken);
                using var response = await TestSupport.ReadFrame(client, TestContext.Current.CancellationToken);
                Assert.True(response.RootElement.GetProperty("ok").GetBoolean());
            }
            Assert.False(broker.HasExited);
        }
        finally { broker.Kill(true); await broker.WaitForExitAsync(TestContext.Current.CancellationToken); }
    }

    [Fact]
    public async Task Broker_enforces_sixteen_concurrent_instances_and_reuses_a_closed_instance()
    {
        var bootstrap = TestSupport.DefaultBootstrap();
        var secret = (string)bootstrap.GetType().GetProperty("secret")!.GetValue(bootstrap)!;
        using var broker = TestSupport.StartBroker(bootstrap);
        var clients = new List<System.IO.Pipes.NamedPipeClientStream>();
        try
        {
            using var ready = JsonDocument.Parse((await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken))!);
            var pipeName = ready.RootElement.GetProperty("pipe").GetString()!;
            for (var i = 0; i < 16; i++)
            {
                try
                {
                    var client = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
                    clients.Add(client);
                    await TestSupport.Authenticate(client, secret);
                }
                catch (Exception error)
                {
                    throw new InvalidOperationException($"Could not authenticate client {i + 1} of the required 16.", error);
                }
            }

            Assert.True(TestSupport.IsPipeBusy(pipeName), "The seventeenth connection was not refused with ERROR_PIPE_BUSY.");
            for (var i = 0; i < clients.Count; i++)
            {
                await TestSupport.WriteFrame(clients[i], new { id = $"cap-{i}", op = "ping" }, TestContext.Current.CancellationToken);
                using var response = await TestSupport.ReadFrame(clients[i], TestContext.Current.CancellationToken);
                Assert.Equal($"cap-{i}", response.RootElement.GetProperty("id").GetString());
                Assert.True(response.RootElement.GetProperty("ok").GetBoolean());
            }

            await clients[0].DisposeAsync();
            clients.RemoveAt(0);
            await using var replacement = await TestSupport.Connect(pipeName, TestContext.Current.CancellationToken);
            await TestSupport.Authenticate(replacement, secret);
            await TestSupport.WriteFrame(replacement, new { id = "replacement", op = "ping" }, TestContext.Current.CancellationToken);
            using var replacementResponse = await TestSupport.ReadFrame(replacement, TestContext.Current.CancellationToken);
            Assert.Equal("replacement", replacementResponse.RootElement.GetProperty("id").GetString());
            Assert.True(replacementResponse.RootElement.GetProperty("ok").GetBoolean());
        }
        finally
        {
            broker.Kill(true);
            await broker.WaitForExitAsync(TestContext.Current.CancellationToken);
            foreach (var client in clients) await client.DisposeAsync();
        }
    }
}
