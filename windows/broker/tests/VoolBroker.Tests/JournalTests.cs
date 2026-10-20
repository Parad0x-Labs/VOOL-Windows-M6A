using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// The journal (design 7.4 as it applies to section 6): a batch a crash stopped is finished or
/// undone at the next start, from the journal alone, by file id and only inside flagged folders.
/// Crashes are real: the broker process is stopped at an exact journal boundary and killed. The
/// in-process tests below only set up journals for the cases a kill cannot reach (a name taken
/// over by another object, a folder no longer flagged, a damaged journal file).
/// </summary>
public sealed class JournalTests : IDisposable
{
    private readonly Workspace work = new();

    public void Dispose() => work.Dispose();

    /// <summary>A tree and a batch using every kind of step: new folders, a new file, a replace, a delete and a rename.</summary>
    private FileChange[] Seed()
    {
        File.WriteAllText(work.In("keep.txt"), "old");
        File.WriteAllText(work.In("gone.txt"), "gone");
        File.WriteAllText(work.In("move.txt"), "move");
        return
        [
            work.Ensure("new\\deeper"),
            work.Write("new\\deeper\\f.txt", "made", "absent"),
            work.Write("keep.txt", "new", Workspace.Sha256("old")),
            work.Delete("gone.txt"),
            work.Rename("move.txt", "new\\moved.txt"),
        ];
    }

    /// <summary>The same batch as Seed, as apply_batch sends it over the pipe.</summary>
    private static object[] SeedOps() =>
    [
        new { op = "ensure_directory", rel = "new\\deeper" },
        new { op = "write_file", rel = "new\\deeper\\f.txt", data = Base64("made"), expect = "absent" },
        new { op = "write_file", rel = "keep.txt", data = Base64("new"), expect = Workspace.Sha256("old") },
        new { op = "delete", rel = "gone.txt" },
        new { op = "rename", rel = "move.txt", to = "new\\moved.txt", replace = false },
    ];

    private static string Base64(string text) => Convert.ToBase64String(Encoding.UTF8.GetBytes(text));

    private static void CrashAt(int step) =>
        BatchRunner.BeforeStepForTests = (index, _) => { if (index == step) throw new BatchCrashForTests(); };

    private void AssertAfterState()
    {
        Assert.Equal("made", File.ReadAllText(work.In("new", "deeper", "f.txt")));
        Assert.Equal("new", File.ReadAllText(work.In("keep.txt")));
        Assert.False(File.Exists(work.In("gone.txt")));
        Assert.False(File.Exists(work.In("move.txt")));
        Assert.Equal("move", File.ReadAllText(work.In("new", "moved.txt")));
        Assert.Empty(work.BrokerNames());
    }

    /// <summary>Starts a real broker that stops at boundary, sends it the seed batch, waits until it has stopped there, and kills it.</summary>
    private async Task KillBrokerAt(string boundary)
    {
        var bootstrap = TestSupport.DefaultBootstrap([work.Root], work.JournalFolder);
        var environment = new Dictionary<string, string> { ["VOOL_BROKER_TEST_PAUSE_AT"] = boundary };
        var (broker, ready) = await TestSupport.StartReady(bootstrap, environment);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            await TestSupport.WriteFrame(client, new { id = "b", op = "apply_batch", root_id = rootId, ops = SeedOps() }, TestContext.Current.CancellationToken);
            var line = await broker.StandardOutput.ReadLineAsync(TestContext.Current.CancellationToken).AsTask()
                .WaitAsync(TimeSpan.FromSeconds(30), TestContext.Current.CancellationToken);
            Assert.Equal(JsonSerializer.Serialize(new { paused = boundary }), line);
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }

    // The broker killed at every journal boundary of every step (before the step's record is
    // flushed, between the flush and the step, between the step and its done record), and just
    // before and after the commit: the next start puts the workspace back exactly as it was, or,
    // once the commit record is on disk, finishes the batch. Either way nothing extra is left.
    [Fact]
    public async Task A_broker_killed_at_any_journal_boundary_is_restored_or_finished_at_the_next_start()
    {
        var steps = 0;
        BatchRunner.BeforeStepForTests = (_, _) => steps++;
        work.Runner.Apply(Seed());
        BatchRunner.BeforeStepForTests = null;
        Assert.True(steps >= 9, $"The seed batch made only {steps} steps.");
        var boundaries = Enumerable.Range(0, steps)
            .SelectMany(index => new[] { $"before_record:{index}", $"before_step:{index}", $"after_step:{index}" })
            .Append("before_commit:-1").Append("after_commit:-1").ToArray();
        foreach (var boundary in boundaries)
        {
            work.Reset();
            Seed();
            var before = TestSupport.ContentHash(work.Root);
            // The broker opens the flagged folder itself; this process lets go of it first.
            work.Registry.Dispose();
            await KillBrokerAt(boundary);
            Assert.Single(work.JournalFiles());

            var bootstrap = TestSupport.DefaultBootstrap([work.Root], work.JournalFolder);
            var (broker, ready) = await TestSupport.StartReady(bootstrap);
            try
            {
                var journal = ready.RootElement.GetProperty("journal");
                Assert.True(journal.GetProperty("available").GetBoolean());
                Assert.True(journal.GetProperty("recovered").GetInt32() == 1 && journal.GetProperty("pending").GetArrayLength() == 0,
                    $"Killed at {boundary}: start line {ready.RootElement.GetRawText()}");
            }
            finally
            {
                ready.Dispose();
                await TestSupport.Kill(broker);
            }
            if (boundary == "after_commit:-1") AssertAfterState();
            else Assert.True(before == TestSupport.ContentHash(work.Root), $"Killed at {boundary}: the workspace is not as it was.");
            Assert.Empty(work.BrokerNames());
            Assert.Empty(work.JournalFiles());
        }
    }

    // A replace stopped after f.txt was set aside: then another program swaps a different file in
    // under the aside name. Recovery leaves that file alone, reports the step and keeps the journal.
    [Fact]
    public void A_journalled_name_another_object_now_holds_is_left_and_reported()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        BatchRunner.BeforeStepForTests = (_, kind) => { if (kind == "place") throw new BatchCrashForTests(); };
        Assert.Throws<BatchCrashForTests>(() => work.Runner.Apply([work.Write("f.txt", "new", Workspace.Sha256("original"))]));
        BatchRunner.BeforeStepForTests = null;
        var aside = Assert.Single(work.BrokerNames(), name => Path.GetFileName(name).StartsWith(".vool-broker-aside-", StringComparison.Ordinal));
        File.Move(aside, Path.Combine(work.Outside, "moved-away.txt"));
        File.WriteAllText(aside, "someone else's");

        work.Restart();
        var outcome = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.False(outcome.Complete);
        Assert.Contains(outcome.Left, line => line.StartsWith("aside f.txt", StringComparison.Ordinal) && line.EndsWith("changed_since_check", StringComparison.Ordinal));
        Assert.Equal("someone else's", File.ReadAllText(aside));
        Assert.False(File.Exists(work.In("f.txt")));
        // The batch's own temp file, which the journal does know by id, is removed.
        Assert.Equal(aside, Assert.Single(work.BrokerNames()));
        Assert.Single(work.JournalFiles());
    }

    // Recovery by a broker whose flagged folders do not include the batch's folder changes nothing
    // there; once that folder is flagged again, the same journal finishes the job.
    [Fact]
    public void A_folder_outside_every_flagged_folder_is_left_alone()
    {
        var batch = Seed();
        var before = TestSupport.ContentHash(work.Root);
        CrashAt(6);
        Assert.Throws<BatchCrashForTests>(() => work.Runner.Apply(batch));
        BatchRunner.BeforeStepForTests = null;
        work.Restart();
        var crashed = TestSupport.TreeHash(work.Root);

        var other = TestSupport.NewTempDirectory();
        try
        {
            using var registry = new RootRegistry();
            registry.AddFlaggedRoot(other);
            var outcome = Assert.Single(Journal.Open(work.JournalFolder).RecoverAll(registry));
            Assert.False(outcome.Complete);
            Assert.All(outcome.Left, line => Assert.EndsWith("path_escapes_root", line, StringComparison.Ordinal));
            Assert.Equal(crashed, TestSupport.TreeHash(work.Root));
            Assert.Single(work.JournalFiles());
        }
        finally { TestSupport.DeleteTree(other); }

        var finished = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.True(finished.Complete, string.Join("; ", finished.Left));
        Assert.Equal(before, TestSupport.ContentHash(work.Root));
        Assert.Empty(work.JournalFiles());
    }

    // A damaged journal record (cut off part way with records after it, or not a record, with or
    // without a torn last record after it, or a file holding nothing) is never
    // guessed at: recovery stops and changes nothing, every new change is refused, reads still
    // work, and the next start reports the batch as pending. The same holds for records that
    // parse but do not fit the journal (review R4): a missing or mistyped field, an unknown
    // record, a step numbered twice, an undone for no step, another batch's begin, a temp step
    // naming a file the batch never made (other.txt, which must survive), and a string that
    // cannot be read (review F6).
    [Theory]
    [InlineData("cut_off_before_the_end")]
    [InlineData("cut_off_and_torn")]
    [InlineData("unknown_record_and_torn")]
    [InlineData("empty")]
    [InlineData("not_a_record")]
    [InlineData("step_without_index")]
    [InlineData("index_not_a_number")]
    [InlineData("unknown_record")]
    [InlineData("step_numbered_twice")]
    [InlineData("undone_of_no_step")]
    [InlineData("other_batch")]
    [InlineData("foreign_temp_name")]
    [InlineData("lone_surrogate")]
    public async Task A_damaged_journal_is_never_guessed_at(string damage)
    {
        File.WriteAllText(work.In("other.txt"), "readable");
        File.WriteAllText(work.In("f.txt"), "original");
        BatchRunner.BeforeStepForTests = (_, kind) => { if (kind == "place") throw new BatchCrashForTests(); };
        Assert.Throws<BatchCrashForTests>(() => work.Runner.Apply([work.Write("f.txt", "new", Workspace.Sha256("original"))]));
        BatchRunner.BeforeStepForTests = null;
        var journalFile = Assert.Single(work.JournalFiles());
        var batchId = Path.GetFileNameWithoutExtension(journalFile)["batch-".Length..];
        var bytes = File.ReadAllBytes(journalFile);
        var lines = Encoding.UTF8.GetString(bytes).TrimEnd('\n').Split('\n');
        var stepLines = lines.Where(line => line.Contains("\"t\":\"step\"", StringComparison.Ordinal)).ToArray();
        byte[] Appended(string line) => [.. bytes, .. Encoding.UTF8.GetBytes(line + "\n")];
        string ForeignTemp()
        {
            var record = JsonNode.Parse(stepLines[0])!.AsObject();
            Assert.Equal("temp", (string?)record["k"]);
            record["i"] = stepLines.Length;
            record["name"] = "other.txt";
            return record.ToJsonString();
        }
        // A string that parses but cannot be read (review F6): the aside name starts with an
        // escaped lone surrogate.
        string LoneSurrogate()
        {
            var text = Encoding.UTF8.GetString(bytes);
            const string asideField = "\"aside\":\".vool-broker-aside-";
            Assert.Equal(1, text.Split(asideField).Length - 1);
            return text.Replace(asideField, "\"aside\":\"\\uD800vool-broker-aside-", StringComparison.Ordinal);
        }
        // A record cut part way with complete records after it: damage, not a write that never finished.
        byte[] CutOffBeforeTheEnd()
        {
            var cut = lines.Select((text, index) => index == 1 ? text[..^5] : text);
            return Encoding.UTF8.GetBytes(string.Join("\n", cut) + "\n");
        }
        bytes = damage switch
        {
            "cut_off_before_the_end" => CutOffBeforeTheEnd(),
            // Damage before the last line end is never excused by a torn tail after it.
            "cut_off_and_torn" => [.. CutOffBeforeTheEnd(), .. Encoding.UTF8.GetBytes("{\"t\":\"comm")],
            "unknown_record_and_torn" => [.. Appended("{\"t\":\"frobnicate\"}"), .. Encoding.UTF8.GetBytes("{\"t\":\"comm")],
            // A file with no record at all: not a begin cut short, since it holds no part of one.
            "empty" => [],
            "not_a_record" => [.. Encoding.UTF8.GetBytes("not a record\n"), .. bytes],
            "step_without_index" => Appended("{\"t\":\"step\",\"k\":\"temp\"}"),
            "index_not_a_number" => Appended("{\"t\":\"undone\",\"i\":\"x\"}"),
            "unknown_record" => Appended("{\"t\":\"frobnicate\"}"),
            "step_numbered_twice" => Appended(stepLines[0]),
            "undone_of_no_step" => Appended("{\"t\":\"undone\",\"i\":99}"),
            "lone_surrogate" => Encoding.UTF8.GetBytes(LoneSurrogate()),
            "other_batch" => Encoding.UTF8.GetBytes(string.Join("\n", lines.Skip(1).Prepend(lines[0].Replace(batchId, new string('0', 32), StringComparison.Ordinal))) + "\n"),
            _ => Appended(ForeignTemp()),
        };
        File.WriteAllBytes(journalFile, bytes);
        work.Restart();
        var crashed = TestSupport.TreeHash(work.Root);

        var outcome = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.False(outcome.Complete);
        Assert.StartsWith("journal: journal_unreadable", Assert.Single(outcome.Left), StringComparison.Ordinal);
        Assert.Equal("journal_unreadable", Assert.Throws<BrokerRefusal>(() => work.Runner.Restore(batchId)).Code);
        Assert.Equal("journal_pending", work.Refusal(work.Write("g.txt", "g", "absent")));
        Assert.Equal("readable", Encoding.UTF8.GetString(FileOps.Read(work.Opened, "other.txt", 100).Data));
        Assert.Equal(crashed, TestSupport.TreeHash(work.Root));
        Assert.Equal(bytes, File.ReadAllBytes(journalFile));
        Assert.Equal(journalFile, Assert.Single(work.JournalFiles()));

        work.Registry.Dispose();
        var bootstrap = TestSupport.DefaultBootstrap([work.Root], work.JournalFolder);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var journal = ready.RootElement.GetProperty("journal");
            Assert.Equal(0, journal.GetProperty("recovered").GetInt32());
            var pending = Assert.Single(journal.GetProperty("pending").EnumerateArray());
            Assert.Equal(batchId, pending.GetProperty("batch_id").GetString());
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            using var written = await TestSupport.Request(client, new { id = "w", op = "write_file", root_id = rootId, rel = "g.txt", data = Base64("g"), expect = "absent" });
            Assert.Equal("journal_pending", TestSupport.ErrorCode(written));
            using var read = await TestSupport.Request(client, new { id = "r", op = "read", root_id = rootId, rel = "other.txt", max_bytes = 100 });
            Assert.Equal(Base64("readable"), read.RootElement.GetProperty("data").GetString());
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
        Assert.Equal(crashed, TestSupport.TreeHash(work.Root));
        Assert.Equal(bytes, File.ReadAllBytes(journalFile));
    }

    // A power loss in the middle of a record's write (review F1) leaves bytes after the last line
    // end. Each record is written whole and flushed before its action begins, so that action never
    // started: at each place a write can tear (a begin, each kind of step, a done, the commit), cut
    // part way or missing only its line end, the torn bytes are kept in batch-ID.torn, cut off the
    // journal, and the batch is put back. A begin alone ends as no batch at all.
    [Theory]
    [InlineData("begin", 3)]
    [InlineData("begin", 1)]
    [InlineData("temp", 5)]
    [InlineData("temp", 1)]
    [InlineData("aside", 5)]
    [InlineData("aside", 1)]
    [InlineData("place", 5)]
    [InlineData("place", 1)]
    [InlineData("done", 5)]
    [InlineData("done", 1)]
    [InlineData("commit", 5)]
    [InlineData("commit", 1)]
    public void A_record_whose_write_never_finished_is_kept_aside_and_the_batch_put_back(string record, int cut)
    {
        File.WriteAllText(work.In("f.txt"), "original");
        var before = TestSupport.ContentHash(work.Root);
        var (batchId, tail) = TearAt(record, cut);
        work.Restart();

        var outcome = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.True(outcome.Complete, string.Join("; ", outcome.Left));
        Assert.False(outcome.Committed);
        Assert.Equal(new TornRecord(tail.Length, $"batch-{batchId}.torn"), outcome.Torn);
        Assert.Equal(before, TestSupport.ContentHash(work.Root));
        Assert.Equal("original", File.ReadAllText(work.In("f.txt")));
        Assert.Empty(work.BrokerNames());
        var kept = Assert.Single(work.JournalFiles());
        Assert.Equal($"batch-{batchId}.torn", Path.GetFileName(kept));
        Assert.Equal(tail, File.ReadAllBytes(kept));
        Assert.Empty(work.Journal.Pending());
    }

    // The start line names each torn record it cut off, with its byte count and the file keeping it.
    [Fact]
    public async Task The_start_line_reports_a_torn_record()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        var (batchId, tail) = TearAt("commit", 5);
        work.Registry.Dispose();
        var bootstrap = TestSupport.DefaultBootstrap([work.Root], work.JournalFolder);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var journal = ready.RootElement.GetProperty("journal");
            Assert.Equal(1, journal.GetProperty("recovered").GetInt32());
            Assert.Empty(journal.GetProperty("pending").EnumerateArray());
            var torn = Assert.Single(journal.GetProperty("torn").EnumerateArray());
            Assert.Equal(batchId, torn.GetProperty("batch_id").GetString());
            Assert.Equal(tail.Length, torn.GetProperty("bytes").GetInt32());
            Assert.Equal($"batch-{batchId}.torn", torn.GetProperty("kept_in").GetString());
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
        Assert.Equal("original", File.ReadAllText(work.In("f.txt")));
        Assert.Equal(tail, File.ReadAllBytes(Assert.Single(work.JournalFiles())));
    }

    // A torn record kept before (a crash during an earlier recovery, after keeping it but before the
    // cut) is never replaced: the next one is kept beside it.
    [Fact]
    public void A_torn_record_never_replaces_one_kept_before()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        var (batchId, tail) = TearAt("place", 5);
        var earlier = Path.Combine(work.JournalFolder, $"batch-{batchId}.torn");
        File.WriteAllText(earlier, "kept before");
        work.Restart();

        var outcome = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.True(outcome.Complete, string.Join("; ", outcome.Left));
        Assert.Equal($"batch-{batchId}.2.torn", outcome.Torn!.KeptIn);
        Assert.Equal("kept before", File.ReadAllText(earlier));
        Assert.Equal(tail, File.ReadAllBytes(Path.Combine(work.JournalFolder, $"batch-{batchId}.2.torn")));
        Assert.Equal("original", File.ReadAllText(work.In("f.txt")));
    }

    // The torn bytes are cut off before recovery adds its own records, so a batch it cannot finish
    // at once (the file's name taken by another program) reads cleanly at the next attempt.
    [Fact]
    public void A_batch_left_pending_after_a_torn_record_finishes_later()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        var (batchId, tail) = TearAt("place", 5);
        File.WriteAllText(work.In("f.txt"), "someone else's");
        work.Restart();

        var first = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.False(first.Complete);
        Assert.Equal(tail.Length, first.Torn!.Bytes);
        Assert.Equal("someone else's", File.ReadAllText(work.In("f.txt")));

        File.Delete(work.In("f.txt"));
        var second = work.Runner.Restore(batchId);
        Assert.True(second.Complete, string.Join("; ", second.Left));
        Assert.Null(second.Torn);
        Assert.Equal("original", File.ReadAllText(work.In("f.txt")));
        Assert.Empty(work.BrokerNames());
        Assert.Equal($"batch-{batchId}.torn", Path.GetFileName(Assert.Single(work.JournalFiles())));
    }

    // When the torn bytes cannot be kept, nothing is cut or changed: the batch stays pending.
    [Fact]
    public void A_torn_record_that_cannot_be_kept_cuts_nothing()
    {
        File.WriteAllText(work.In("f.txt"), "original");
        var (batchId, _) = TearAt("place", 5);
        // A folder under the name the torn bytes would take: the file cannot be made.
        Directory.CreateDirectory(Path.Combine(work.JournalFolder, $"batch-{batchId}.torn"));
        var journalFile = Path.Combine(work.JournalFolder, $"batch-{batchId}.jsonl");
        var bytes = File.ReadAllBytes(journalFile);
        work.Restart();
        var crashed = TestSupport.TreeHash(work.Root);

        var outcome = Assert.Single(work.Journal.RecoverAll(work.Registry));
        Assert.False(outcome.Complete);
        Assert.StartsWith("journal: journal_unavailable", Assert.Single(outcome.Left), StringComparison.Ordinal);
        Assert.Null(outcome.Torn);
        Assert.Equal(bytes, File.ReadAllBytes(journalFile));
        Assert.Equal(crashed, TestSupport.TreeHash(work.Root));
        Assert.Equal(batchId, Assert.Single(work.Journal.Pending()));
    }

    /// <summary>
    /// Replaces f.txt in one batch and stops the broker in the write of the chosen record (begin;
    /// the temp, aside or place step; the temp's done; the commit), then cuts that record's last cut
    /// bytes off the journal: what a power loss part way through the write leaves. Returns the
    /// batch id and the bytes left after the last line end.
    /// </summary>
    private (string BatchId, byte[] Tail) TearAt(string record, int cut)
    {
        if (record == "begin")
        {
            // Stopped before the first step's action; then only part of the begin is left.
            CrashAt(0);
        }
        else
        {
            BatchLog.FaultForTests = point =>
            {
                if (point != "after_write") return;
                var last = LastRecord();
                var kind = last.GetProperty("t").GetString();
                var matches = record switch
                {
                    "done" => kind == "done" && last.GetProperty("i").GetInt32() == 0,
                    "commit" => kind == "commit",
                    _ => kind == "step" && last.GetProperty("k").GetString() == record,
                };
                if (matches) throw new BatchCrashForTests();
            };
        }
        try { Assert.Throws<BatchCrashForTests>(() => work.Runner.Apply([work.Write("f.txt", "new", Workspace.Sha256("original"))])); }
        finally
        {
            BatchLog.FaultForTests = null;
            BatchRunner.BeforeStepForTests = null;
        }
        var journalFile = Assert.Single(work.JournalFiles());
        var batchId = Path.GetFileNameWithoutExtension(journalFile)["batch-".Length..];
        var bytes = File.ReadAllBytes(journalFile);
        // The torn record is the begin (the first line), or the last line.
        var (lastLineStart, lineEnd) = record == "begin"
            ? (0, Array.IndexOf(bytes, (byte)'\n'))
            : (Array.LastIndexOf(bytes, (byte)'\n', bytes.Length - 2) + 1, bytes.Length - 1);
        Assert.Equal((byte)'\n', bytes[lineEnd]);
        var keep = lineEnd + 1 - cut;
        // The cut never reaches back past the record before it.
        Assert.True(keep > lastLineStart);
        bytes = bytes[..keep];
        File.WriteAllBytes(journalFile, bytes);
        return (batchId, bytes[lastLineStart..]);
    }

    private JsonElement LastRecord()
    {
        using var stream = new FileStream(Assert.Single(work.JournalFiles()), FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
        using var reader = new StreamReader(stream);
        var last = reader.ReadToEnd().TrimEnd('\n').Split('\n')[^1];
        using var document = JsonDocument.Parse(last);
        return document.RootElement.Clone();
    }

    // A journal folder the broker cannot create: it still starts, says so, and refuses every change.
    [Fact]
    public async Task A_journal_folder_that_cannot_be_written_is_reported_at_start()
    {
        var notAFolder = Path.Combine(work.Outside, "not-a-folder");
        File.WriteAllText(notAFolder, "a file");
        var bootstrap = TestSupport.DefaultBootstrap([work.Root], Path.Combine(notAFolder, "journal"));
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var journal = ready.RootElement.GetProperty("journal");
            Assert.False(journal.GetProperty("available").GetBoolean());
            Assert.Equal(0, journal.GetProperty("recovered").GetInt32());
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            using var written = await TestSupport.Request(client, new { id = "w", op = "write_file", root_id = rootId, rel = "f.txt", data = "eA==", expect = "absent" });
            Assert.Equal("journal_unavailable", TestSupport.ErrorCode(written));
            Assert.False(File.Exists(work.In("f.txt")));
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
    }
}
