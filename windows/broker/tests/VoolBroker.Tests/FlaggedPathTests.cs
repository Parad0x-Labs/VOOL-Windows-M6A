using System.Text.Json;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// One list for gate and broker (section 4, Phase C slice C1): the API asks the broker, before it
/// hands a call over, for the current final path of the flagged folder the gate checked, and
/// refuses the call on a mismatch. flagged_path answers that from the folder handle the broker
/// holds, never by reopening the path.
/// </summary>
public sealed class FlaggedPathTests : IDisposable
{
    private readonly string home = TestSupport.NewTempDirectory();

    public void Dispose() => TestSupport.DeleteTree(home);

    [Fact]
    public async Task Flagged_path_reports_the_held_folders_final_path_as_the_start_line_does()
    {
        var flagged = Path.Combine(home, "flagged");
        Directory.CreateDirectory(flagged);
        var bootstrap = TestSupport.DefaultBootstrap([flagged], Path.Combine(home, "broker", "journal"));
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var entry = ready.RootElement.GetProperty("flagged")[0];
            var flaggedId = entry.GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var answer = await TestSupport.Request(client, new { id = "p", op = "flagged_path", flagged_id = flaggedId });
            Assert.True(answer.RootElement.GetProperty("ok").GetBoolean(), answer.RootElement.GetRawText());
            Assert.Equal(entry.GetProperty("path").GetString(), answer.RootElement.GetProperty("path").GetString());
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }

    [Fact]
    public async Task Flagged_path_refuses_an_id_the_broker_did_not_report()
    {
        var flagged = Path.Combine(home, "flagged");
        Directory.CreateDirectory(flagged);
        var bootstrap = TestSupport.DefaultBootstrap([flagged], Path.Combine(home, "broker", "journal"));
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var answer = await TestSupport.Request(client, new { id = "p", op = "flagged_path", flagged_id = new string('0', 48) });
            Assert.Equal("unknown_root", TestSupport.ErrorCode(answer));
            using var missing = await TestSupport.Request(client, new { id = "q", op = "flagged_path" });
            Assert.Equal("invalid_request", TestSupport.ErrorCode(missing));
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }
}
