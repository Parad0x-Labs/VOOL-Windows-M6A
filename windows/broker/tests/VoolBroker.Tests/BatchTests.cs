using System.Text.Json;
using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// apply_batch and restore_batch (design section 6): all-or-nothing for refusals, and a batch an
/// I/O error stops part way is put back by the broker itself.
/// </summary>
public sealed class BatchTests : IDisposable
{
    private readonly Workspace work = new();

    public void Dispose() => work.Dispose();

    /// <summary>A tree and a batch that between them use every kind of step: new folders, a new file, a replace, a rename and a delete.</summary>
    private FileChange[] Seed()
    {
        Directory.CreateDirectory(work.In("src"));
        File.WriteAllText(work.In("src", "keep.txt"), "keep");
        File.WriteAllText(work.In("src", "replace.txt"), "old");
        File.WriteAllText(work.In("src", "move.txt"), "move");
        File.WriteAllText(work.In("src", "gone.txt"), "gone");
        Directory.CreateDirectory(work.In("src", "empty"));
        return
        [
            work.Ensure("new\\deeper"),
            work.Write("new\\deeper\\made.txt", "made", "absent"),
            work.Write("src\\replace.txt", "new", Workspace.Sha256("old")),
            work.Rename("src\\move.txt", "new\\moved.txt"),
            work.Delete("src\\gone.txt"),
            work.Delete("src\\empty"),
        ];
    }

    // Every operation applied, and nothing extra left: the workspace holds exactly the objects the
    // batch describes (no temp or aside object) and the journal folder is empty.
    [Fact]
    public void A_batch_applies_every_operation()
    {
        var result = work.Runner.Apply(Seed());
        Assert.Equal(6, result.Results.Count);
        Assert.Equal("made", File.ReadAllText(work.In("new", "deeper", "made.txt")));
        Assert.Equal("new", File.ReadAllText(work.In("src", "replace.txt")));
        Assert.Equal("move", File.ReadAllText(work.In("new", "moved.txt")));
        Assert.False(File.Exists(work.In("src", "move.txt")));
        Assert.False(File.Exists(work.In("src", "gone.txt")));
        Assert.False(Directory.Exists(work.In("src", "empty")));
        Assert.Equal("keep", File.ReadAllText(work.In("src", "keep.txt")));
        Assert.Empty(result.CleanupLeft);
        var entries = Directory.EnumerateFileSystemEntries(work.Root, "*", SearchOption.AllDirectories)
            .Select(path => Path.GetRelativePath(work.Root, path)).Order(StringComparer.Ordinal).ToArray();
        Assert.Equal(new[] { "new", "new\\deeper", "new\\deeper\\made.txt", "new\\moved.txt", "src", "src\\keep.txt", "src\\replace.txt" }, entries);
        Assert.Empty(work.JournalFiles());
    }

    // A refusal found at the last operation: the first change was never made, and nothing extra
    // (temp object, aside object, journal file) was left.
    [Theory]
    [InlineData("stale_hash", "changed_since_check")]
    [InlineData("missing", "not_found")]
    [InlineData("escape", "path_escapes_root")]
    [InlineData("device", "device_name")]
    public void A_late_refusal_changes_nothing(string kind, string code)
    {
        var batch = Seed().ToList();
        batch.Add(kind switch
        {
            "stale_hash" => work.Write("src\\keep.txt", "x", Workspace.Sha256("not keep")),
            "missing" => work.Delete("src\\absent.txt"),
            "escape" => work.Write("..\\outside.txt", "x"),
            _ => work.Write("src\\CON", "x"),
        });
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal(code, work.Refusal([.. batch]));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
        Assert.Empty(work.JournalFiles());
    }

    // An I/O error injected before each step in turn: the broker undoes every step already made.
    [Fact]
    public void An_error_at_any_step_is_restored()
    {
        var batch = Seed();
        var steps = 0;
        BatchRunner.BeforeStepForTests = (_, _) => steps++;
        var probe = work.Runner.Apply(batch);
        BatchRunner.BeforeStepForTests = null;
        Assert.Empty(probe.CleanupLeft);
        work.Reset();
        for (var failAt = 0; failAt < steps; failAt++)
        {
            batch = Seed();
            var before = TestSupport.ContentHash(work.Root);
            var target = failAt;
            BatchRunner.BeforeStepForTests = (index, _) => { if (index == target) throw new IOException($"injected at step {target}"); };
            var stopped = work.Stopped(batch);
            BatchRunner.BeforeStepForTests = null;
            Assert.Equal("io_error", stopped.Code);
            Assert.True(stopped.Restored, $"Step {failAt}: restore left {string.Join("; ", stopped.Left)}");
            Assert.Equal(before, TestSupport.ContentHash(work.Root));
            Assert.Empty(work.BrokerNames());
            Assert.Empty(work.JournalFiles());
            work.Reset();
        }
        Assert.True(steps >= 10, $"The seed batch made only {steps} steps.");
    }

    [Theory]
    [InlineData("same_file_twice")]
    [InlineData("under_a_deleted_folder")]
    [InlineData("into_itself")]
    [InlineData("rename_onto_a_written_file")]
    public void An_object_named_by_two_operations_is_refused(string kind)
    {
        Directory.CreateDirectory(work.In("dir"));
        File.WriteAllText(work.In("dir", "f.txt"), "f");
        File.WriteAllText(work.In("g.txt"), "g");
        // Empty, so its delete passes the plan's own checks and the second operation meets it.
        Directory.CreateDirectory(work.In("empty"));
        FileChange[] batch = kind switch
        {
            "same_file_twice" => [work.Write("g.txt", "1"), work.Delete("g.txt")],
            "under_a_deleted_folder" => [work.Delete("empty"), work.Write("empty\\h.txt", "h")],
            "into_itself" => [work.Rename("dir", "dir\\inner")],
            _ => [work.Write("g.txt", "1"), work.Rename("dir\\f.txt", "g.txt", replace: true)],
        };
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal("invalid_request", work.Refusal(batch));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
    }

    [Fact]
    public void A_write_into_a_folder_no_earlier_operation_creates_is_refused()
    {
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal("not_found", work.Refusal(work.Write("new\\f.txt", "f"), work.Ensure("new")));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
    }

    // A restore that another program blocks is reported, its journal kept, and restore_batch finishes it later.
    [Fact]
    public void A_blocked_restore_is_reported_and_finished_by_restore_batch()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        BatchRunner.BeforeStepForTests = (_, kind) =>
        {
            if (kind != "place") return;
            // After f.txt is set aside, another program takes its name, so it cannot be put back.
            File.WriteAllText(work.In("f.txt"), "intruder");
            throw new IOException("injected");
        };
        var stopped = work.Stopped(work.Write("f.txt", "new", Workspace.Sha256("original")));
        BatchRunner.BeforeStepForTests = null;
        Assert.False(stopped.Restored);
        Assert.NotEmpty(stopped.Left);
        Assert.Single(work.JournalFiles());
        // Over the pipe this is restore_incomplete, not a plain refusal: the workspace is not as it was.
        using (var reply = JsonSerializer.SerializeToDocument(Channel.StoppedReply(JsonSerializer.SerializeToElement("b"), stopped)))
        {
            var error = reply.RootElement.GetProperty("error");
            Assert.Equal("restore_incomplete", error.GetProperty("code").GetString());
            Assert.Equal("io_error", error.GetProperty("cause").GetString());
            Assert.False(reply.RootElement.GetProperty("batch").GetProperty("restored").GetBoolean());
        }
        // Until the batch is cleared, no new change starts.
        Assert.Equal("journal_pending", work.Refusal(work.Write("other.txt", "x", "absent")));
        Assert.Equal("intruder", File.ReadAllText(work.In("f.txt")));
        var aside = Assert.Single(work.BrokerNames(), name => Path.GetFileName(name).StartsWith(".vool-broker-aside-", StringComparison.Ordinal));
        Assert.Equal("original", File.ReadAllText(aside));

        Assert.False(work.Runner.Restore(stopped.BatchId).Complete);
        File.Delete(work.In("f.txt"));
        var outcome = work.Runner.Restore(stopped.BatchId);
        Assert.True(outcome.Complete, string.Join("; ", outcome.Left));
        Assert.Equal("original", File.ReadAllText(work.In("f.txt")));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
        Assert.Equal("unknown_batch", Assert.Throws<BrokerRefusal>(() => work.Runner.Restore(stopped.BatchId)).Code);
        Assert.Equal("invalid_request", Assert.Throws<BrokerRefusal>(() => work.Runner.Restore("..\\x")).Code);
        work.Runner.Apply([work.Write("other.txt", "x", "absent")]);
        Assert.Equal("x", File.ReadAllText(work.In("other.txt")));
    }

    // Over the pipe, a stopped batch is not a plain refusal: the reply carries what ran and whether it was restored.
    [Fact]
    public async Task A_stopped_batch_over_the_pipe_says_what_it_left()
    {
        var flagged = TestSupport.NewTempDirectory();
        var journalParent = TestSupport.NewTempDirectory();
        var bootstrap = TestSupport.DefaultBootstrap([flagged], Path.Combine(journalParent, "journal"));
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            // Another program holds a file inside folder a, so NTFS refuses to rename a: the batch
            // stops after its first operation, and the broker removes the folder that operation made.
            Directory.CreateDirectory(Path.Combine(flagged, "a"));
            File.WriteAllText(Path.Combine(flagged, "a", "f.txt"), "f");
            using (TestSupport.HoldExclusively(Path.Combine(flagged, "a", "f.txt")))
            {
                using var reply = await TestSupport.Request(client, new
                {
                    id = "b", op = "apply_batch", root_id = rootId,
                    ops = new object[] { new { op = "ensure_directory", rel = "b" }, new { op = "rename", rel = "a", to = "b\\a", replace = false } }
                });
                Assert.False(reply.RootElement.GetProperty("ok").GetBoolean());
                var error = reply.RootElement.GetProperty("error");
                Assert.Equal(error.GetProperty("cause").GetString(), error.GetProperty("code").GetString());
                var batch = reply.RootElement.GetProperty("batch");
                Assert.Equal(1, batch.GetProperty("ran").GetInt32());
                Assert.True(batch.GetProperty("restored").GetBoolean(), reply.RootElement.GetRawText());
                Assert.Equal(JsonValueKind.Array, batch.GetProperty("left").ValueKind);
            }
            Assert.False(Directory.Exists(Path.Combine(flagged, "b")));
            Assert.Equal("f", File.ReadAllText(Path.Combine(flagged, "a", "f.txt")));
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
            TestSupport.DeleteTree(flagged);
            TestSupport.DeleteTree(journalParent);
        }
    }
}
