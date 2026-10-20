using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;
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

    /// <summary>The last record of the one batch the journal holds, read while the batch still writes it.</summary>
    private JsonElement LastRecord()
    {
        using var stream = new FileStream(Assert.Single(work.JournalFiles()), FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        using var reader = new StreamReader(stream);
        var last = reader.ReadToEnd().TrimEnd('\n').Split('\n')[^1];
        using var document = JsonDocument.Parse(last);
        return document.RootElement.Clone();
    }

    // The commit record is written, then its flush fails: the batch is committed, so it is
    // reported done and finished, never "restored" with its originals already deleted (review R1).
    [Fact]
    public void A_batch_whose_commit_flush_fails_is_reported_committed()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        BatchRunner.AfterCommitRecordForTests = () => throw new IOException("injected flush failure");
        var result = work.Runner.Apply([work.Write("f.txt", "new", Workspace.Sha256("original"))]);
        BatchRunner.AfterCommitRecordForTests = null;
        Assert.Empty(result.CleanupLeft);
        Assert.Equal("new", File.ReadAllText(work.In("f.txt")));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    // Changes run one at a time: a change asked for while a batch runs waits for it, and when that
    // batch ends not fully restored, the waiting change is refused journal_pending (review R2).
    [Fact]
    public async Task A_change_waits_for_a_running_batch_and_then_sees_it_pending()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        Task<string>? second = null;
        var ranMeanwhile = false;
        BatchRunner.BeforeStepForTests = (_, kind) =>
        {
            if (kind != "place" || second is not null) return;
            second = Task.Run(() =>
            {
                BatchRunner.BeforeStepForTests = null;
                return Assert.Throws<BrokerRefusal>(() => work.Runner.Apply([work.Write("other.txt", "x", "absent")])).Code;
            });
            ranMeanwhile = ((IAsyncResult)second).AsyncWaitHandle.WaitOne(TimeSpan.FromMilliseconds(500));
            // Then this batch stops in a way it cannot fully undo: another program takes f.txt's name.
            File.WriteAllText(work.In("f.txt"), "intruder");
            throw new IOException("injected");
        };
        var stopped = work.Stopped(work.Write("f.txt", "new", Workspace.Sha256("original")));
        BatchRunner.BeforeStepForTests = null;
        Assert.False(ranMeanwhile, "A second change finished while the first batch was running.");
        Assert.False(stopped.Restored);
        Assert.Equal("journal_pending", await second!.WaitAsync(TimeSpan.FromSeconds(30), TestContext.Current.CancellationToken));
        Assert.False(File.Exists(work.In("other.txt")));
    }

    // Another program takes a temp name after its record is written and before the create: the
    // create fails, and that object is never removed by the restore (review R3).
    [Fact]
    public void An_object_already_under_a_temp_name_is_never_removed()
    {
        string? taken = null;
        BatchRunner.BeforeStepForTests = (_, kind) =>
        {
            if (kind != "temp" || taken is not null) return;
            taken = work.In(LastRecord().GetProperty("name").GetString()!);
            File.WriteAllText(taken, "someone else's");
        };
        var stopped = work.Stopped(work.Write("f.txt", "new", "absent"));
        BatchRunner.BeforeStepForTests = null;
        Assert.Equal("destination_exists", stopped.Code);
        Assert.True(stopped.Restored, string.Join("; ", stopped.Left));
        Assert.Equal("someone else's", File.ReadAllText(taken!));
        Assert.False(File.Exists(work.In("f.txt")));
        Assert.Empty(work.JournalFiles());
    }

    // After a crash, the journal does not know the id of a temp it never created: an object under
    // that name that is not what a create leaves (empty, one link) is left and reported (review R3).
    [Fact]
    public void After_a_crash_an_unknown_object_under_a_temp_name_is_left_and_reported()
    {
        string? taken = null;
        BatchRunner.BeforeStepForTests = (_, kind) =>
        {
            if (kind != "temp") return;
            taken = work.In(LastRecord().GetProperty("name").GetString()!);
            File.WriteAllText(taken, "someone else's");
            throw new BatchCrashForTests();
        };
        Assert.Throws<BatchCrashForTests>(() => work.Runner.Apply([work.Write("f.txt", "new", "absent")]));
        BatchRunner.BeforeStepForTests = null;
        work.Restart();
        var outcome = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.False(outcome.Complete);
        Assert.Contains(outcome.Left, line => line.StartsWith("temp ", StringComparison.Ordinal) && line.EndsWith("changed_since_check", StringComparison.Ordinal));
        Assert.Equal("someone else's", File.ReadAllText(taken!));
        Assert.Single(work.JournalFiles());
    }

    // A new folder is placed, the batch then stops, and another program holds a file inside that
    // folder, so NTFS refuses to move it back yet. The temp step it moved waits for it instead of
    // counting as undone, so restore_batch later leaves no temp folder behind (review R6). The file
    // is inside the folder because the plan's own handle on a new file (write access, read sharing)
    // stops any other program holding that file exclusively while the batch runs.
    [Fact]
    public void A_temp_a_later_step_moved_is_not_counted_undone_before_that_step()
    {
        SafeFileHandle? held = null;
        BatchRunner.BeforeStepForTests = (index, kind) =>
        {
            if (index != 2) return;
            Assert.Equal("temp", kind);
            File.WriteAllText(work.In("d", "x.txt"), "x");
            held = TestSupport.HoldExclusively(work.In("d", "x.txt"));
            throw new IOException("injected");
        };
        BatchStopped stopped;
        try { stopped = work.Stopped(work.Ensure("d"), work.Write("g.txt", "g", "absent")); }
        finally { BatchRunner.BeforeStepForTests = null; }
        using (held)
        {
            Assert.NotNull(held);
            Assert.Equal("io_error", stopped.Code);
            Assert.False(stopped.Restored);
            Assert.Contains(stopped.Left, line => line.StartsWith("place d:", StringComparison.Ordinal));
            Assert.Contains(stopped.Left, line => line.StartsWith("temp ", StringComparison.Ordinal) && line.EndsWith("changed_since_check", StringComparison.Ordinal));
        }
        File.Delete(work.In("d", "x.txt"));
        var outcome = work.Runner.Restore(stopped.BatchId);
        Assert.True(outcome.Complete, string.Join("; ", outcome.Left));
        Assert.False(Directory.Exists(work.In("d")));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    // A read-only file is never deleted, and never replaced by a rename.
    [Fact]
    public void Read_only_files_are_not_deleted_or_replaced()
    {
        File.WriteAllText(work.In("ro.txt"), "ro");
        File.WriteAllText(work.In("f.txt"), "f");
        File.SetAttributes(work.In("ro.txt"), FileAttributes.ReadOnly);
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal("read_only", work.Refusal(work.Delete("ro.txt")));
        Assert.Equal("read_only", work.Refusal(work.Rename("f.txt", "ro.txt", replace: true)));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
        Assert.Empty(work.JournalFiles());
        File.SetAttributes(work.In("ro.txt"), FileAttributes.Normal);
    }

    // One operation over the limit refuses the whole batch before anything is planned.
    [Fact]
    public void A_batch_over_the_operation_limit_changes_nothing()
    {
        var changes = Enumerable.Range(0, BatchRunner.MaxOperations + 1).Select(n => work.Write($"f{n}.txt", "x", "absent")).ToArray();
        Assert.Equal("invalid_request", work.Refusal(changes));
        Assert.Empty(Directory.GetFileSystemEntries(work.Root));
        Assert.Empty(work.JournalFiles());
    }

    // Over the pipe: apply_batch's own limit and field checks, and the largest read. A read of
    // FileOps.MaxReadBytes whose base64 is all '+' (the worst case for a JSON encoder) arrives
    // whole in one reply (review R5).
    [Fact]
    public async Task Limits_over_the_pipe()
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

            var tooMany = Enumerable.Range(0, BatchRunner.MaxOperations + 1)
                .Select(n => (object)new { op = "write_file", rel = $"f{n}.txt", data = "eA==", expect = "absent" }).ToArray();
            using (var reply = await TestSupport.Request(client, new { id = "many", op = "apply_batch", root_id = rootId, ops = tooMany }))
                Assert.Equal("invalid_request", TestSupport.ErrorCode(reply));
            File.WriteAllText(Path.Combine(flagged, "x.txt"), "x");
            using (var reply = await TestSupport.Request(client, new { id = "extra", op = "apply_batch", root_id = rootId, ops = new object[] { new { op = "delete", rel = "x.txt", force = true } } }))
                Assert.Equal("invalid_request", TestSupport.ErrorCode(reply));
            Assert.Equal("x", File.ReadAllText(Path.Combine(flagged, "x.txt")));
            Assert.Equal("x.txt", Path.GetFileName(Assert.Single(Directory.GetFileSystemEntries(flagged))));

            using (var reply = await TestSupport.Request(client, new { id = "over", op = "read", root_id = rootId, rel = "x.txt", max_bytes = FileOps.MaxReadBytes + 1 }))
                Assert.Equal("invalid_request", TestSupport.ErrorCode(reply));
            var data = new byte[FileOps.MaxReadBytes];
            byte[] plus = [0xFB, 0xEF, 0xBE];
            for (var index = 0; index < data.Length; index++) data[index] = plus[index % 3];
            File.WriteAllBytes(Path.Combine(flagged, "big.bin"), data);
            using (var reply = await TestSupport.Request(client, new { id = "big", op = "read", root_id = rootId, rel = "big.bin", max_bytes = FileOps.MaxReadBytes }))
            {
                Assert.True(reply.RootElement.GetProperty("ok").GetBoolean(), TestSupport.ErrorCode(reply));
                Assert.Equal(data.Length, reply.RootElement.GetProperty("size").GetInt32());
                Assert.Equal(Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant(), reply.RootElement.GetProperty("sha256").GetString());
                Assert.True(data.AsSpan().SequenceEqual(Convert.FromBase64String(reply.RootElement.GetProperty("data").GetString()!)));
            }
            using var ping = await TestSupport.Request(client, new { id = "after", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
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
