using System.Text;
using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// stat, read and each single change (design section 6) in process, on real NTFS folders, plus one
/// pass through the real broker and its pipe. Every refusal is shown to change nothing.
/// </summary>
public sealed class FileOpsTests : IDisposable
{
    private readonly Workspace work = new();

    public void Dispose() => work.Dispose();

    private string Text(params string[] parts) => File.ReadAllText(work.In(parts));

    [Fact]
    public void Stat_and_read_go_through_the_walk()
    {
        Directory.CreateDirectory(work.In("a"));
        File.WriteAllText(work.In("a", "f.txt"), "hello");
        var stat = FileOps.Stat(work.Opened, "a\\f.txt");
        Assert.Equal("file", stat.Kind);
        Assert.Equal(5, stat.Size);
        Assert.False(stat.ReadOnly);
        Assert.Equal("directory", FileOps.Stat(work.Opened, "a").Kind);
        Assert.Equal("directory", FileOps.Stat(work.Opened, "").Kind);
        var (data, sha256) = FileOps.Read(work.Opened, "a\\f.txt", 5);
        Assert.Equal("hello", Encoding.UTF8.GetString(data));
        Assert.Equal(Workspace.Sha256("hello"), "sha256:" + sha256);
        Assert.Equal("too_large", Assert.Throws<BrokerRefusal>(() => FileOps.Read(work.Opened, "a\\f.txt", 4)).Code);
        Assert.Equal("is_a_directory", Assert.Throws<BrokerRefusal>(() => FileOps.Read(work.Opened, "a", 100)).Code);
        TestSupport.CreateJunction(work.In("link"), work.Outside);
        File.WriteAllText(Path.Combine(work.Outside, "secret.txt"), "outside");
        Assert.Equal("reparse_point", Assert.Throws<BrokerRefusal>(() => FileOps.Read(work.Opened, "link\\secret.txt", 100)).Code);
    }

    [Fact]
    public void Write_creates_a_new_file_and_leaves_nothing_else()
    {
        var result = work.Runner.Apply([work.Write("new.txt", "content", expect: "absent")]);
        Assert.Equal("content", Text("new.txt"));
        Assert.Equal(Workspace.Sha256("content"), "sha256:" + result.Results[0]["sha256"]);
        Assert.Equal(FileOps.Stat(work.Opened, "new.txt").FileId, (string?)result.Results[0]["file_id"]);
        Assert.Empty(result.CleanupLeft);
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    [Fact]
    public void Write_replaces_a_file_whose_hash_matches()
    {
        File.WriteAllText(work.In("f.txt"), "old");
        work.Runner.Apply([work.Write("f.txt", "new", Workspace.Sha256("old"))]);
        Assert.Equal("new", Text("f.txt"));
        work.Runner.Apply([work.Write("f.txt", "newer")]);
        Assert.Equal("newer", Text("f.txt"));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    // Every refusal before the first change: the tree, times included, is exactly as it was.
    [Theory]
    [InlineData("stale_hash", "changed_since_check")]
    [InlineData("exists_expected_absent", "changed_since_check")]
    [InlineData("absent_expected_hash", "changed_since_check")]
    [InlineData("read_only", "read_only")]
    [InlineData("held", "in_use")]
    [InlineData("hard_link", "blocked_by_hard_link")]
    [InlineData("junction_parent", "reparse_point")]
    [InlineData("folder", "is_a_directory")]
    [InlineData("missing_parent", "not_found")]
    [InlineData("bad_expect", "invalid_request")]
    public void Write_refusals_change_nothing(string kind, string code)
    {
        File.WriteAllText(work.In("f.txt"), "old");
        Directory.CreateDirectory(work.In("dir"));
        var change = work.Write("f.txt", "new", Workspace.Sha256("old"));
        switch (kind)
        {
            case "stale_hash": change = work.Write("f.txt", "new", Workspace.Sha256("other")); break;
            case "exists_expected_absent": change = work.Write("f.txt", "new", "absent"); break;
            case "absent_expected_hash": change = work.Write("g.txt", "new", Workspace.Sha256("old")); break;
            case "read_only": File.SetAttributes(work.In("f.txt"), FileAttributes.ReadOnly); break;
            case "hard_link": TestSupport.HardLink(Path.Combine(work.Outside, "other-name.txt"), work.In("f.txt")); break;
            case "junction_parent":
                TestSupport.CreateJunction(work.In("link"), work.Outside);
                change = work.Write("link\\f.txt", "new", "any");
                break;
            case "folder": change = work.Write("dir", "new", "any"); break;
            case "missing_parent": change = work.Write("absent\\f.txt", "new", "any"); break;
            case "bad_expect": change = work.Write("f.txt", "new", "sha256:ABC"); break;
        }
        var before = TestSupport.TreeHash(work.Root);
        var outside = TestSupport.TreeHash(work.Outside);
        // Held only after hashing: the hash reads the file, and the hold shares nothing.
        using (kind == "held" ? TestSupport.HoldExclusively(work.In("f.txt")) : null)
            Assert.Equal(code, work.Refusal(change));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
        Assert.Equal(outside, TestSupport.TreeHash(work.Outside));
        Assert.Empty(work.JournalFiles());
        if (kind == "read_only") File.SetAttributes(work.In("f.txt"), FileAttributes.Normal);
    }

    // The destination is held from its check until it is set aside: another program cannot write it meanwhile.
    [Fact]
    public void Destination_is_held_against_other_writers_until_it_is_replaced()
    {
        File.WriteAllText(work.In("f.txt"), "old");
        var blocked = 0;
        BatchRunner.BeforeStepForTests = (_, kind) =>
        {
            if (kind != "aside") return;
            // Full sharing, delete included, so only the broker's own share mode can refuse this writer.
            try
            {
                using var racer = new FileStream(work.In("f.txt"), FileMode.Open, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete);
                racer.Write("racer"u8);
            }
            catch (IOException) { blocked++; }
        };
        work.Runner.Apply([work.Write("f.txt", "new", Workspace.Sha256("old"))]);
        Assert.Equal(1, blocked);
        Assert.Equal("new", Text("f.txt"));
    }

    // expect=absent against a file another program creates after the check: the broker never overwrites it.
    [Fact]
    public void A_file_created_after_an_absent_check_is_not_overwritten()
    {
        BatchRunner.BeforeStepForTests = (_, kind) => { if (kind == "place") File.WriteAllText(work.In("new.txt"), "theirs"); };
        var stopped = work.Stopped(work.Write("new.txt", "mine", "absent"));
        Assert.Equal("changed_since_check", stopped.Code);
        Assert.True(stopped.Restored);
        Assert.Empty(stopped.Left);
        Assert.Equal("theirs", Text("new.txt"));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    [Fact]
    public void Ensure_directory_creates_each_missing_folder()
    {
        Directory.CreateDirectory(work.In("a"));
        var result = work.Runner.Apply([work.Ensure("a\\b\\c")]);
        Assert.Equal(new[] { "a\\b", "a\\b\\c" }, (string[])result.Results[0]["created"]!);
        Assert.True(Directory.Exists(work.In("a", "b", "c")));
        var again = work.Runner.Apply([work.Ensure("a\\b\\c")]);
        Assert.Empty((string[])again.Results[0]["created"]!);
        File.WriteAllText(work.In("file"), "x");
        Assert.Equal("not_a_directory", work.Refusal(work.Ensure("file\\sub")));
        Assert.Empty(work.BrokerNames());
    }

    [Fact]
    public void Rename_moves_and_refuses_or_replaces_an_existing_destination()
    {
        File.WriteAllText(work.In("a.txt"), "a");
        File.WriteAllText(work.In("b.txt"), "b");
        Directory.CreateDirectory(work.In("dir"));
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal("destination_exists", work.Refusal(work.Rename("a.txt", "b.txt")));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
        work.Runner.Apply([work.Rename("a.txt", "dir\\moved.txt")]);
        Assert.False(File.Exists(work.In("a.txt")));
        Assert.Equal("a", Text("dir", "moved.txt"));
        work.Runner.Apply([work.Rename("dir\\moved.txt", "b.txt", replace: true)]);
        Assert.Equal("a", Text("b.txt"));
        Assert.Empty(Directory.GetFileSystemEntries(work.In("dir")));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    [Fact]
    public void Rename_moves_a_folder_between_two_roots_of_one_flagged_folder()
    {
        Directory.CreateDirectory(work.In("src", "tree"));
        File.WriteAllText(work.In("src", "tree", "f.txt"), "f");
        Directory.CreateDirectory(work.In("dst"));
        var registry = work.Registry;
        var flaggedId = registry.Flagged.Single().Id;
        var source = registry.GetOpenedRoot(registry.OpenRoot(flaggedId, "src"));
        var target = registry.GetOpenedRoot(registry.OpenRoot(flaggedId, "dst"));
        work.Runner.Apply([new FileChange("rename", source, "tree", ToRoot: target, ToPath: "tree")]);
        Assert.Equal("f", Text("dst", "tree", "f.txt"));
        Assert.False(Directory.Exists(work.In("src", "tree")));
    }

    [Fact]
    public void Delete_removes_a_file_or_an_empty_folder_and_refuses_a_full_one()
    {
        File.WriteAllText(work.In("f.txt"), "x");
        Directory.CreateDirectory(work.In("empty"));
        Directory.CreateDirectory(work.In("full"));
        File.WriteAllText(work.In("full", "inner.txt"), "x");
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal("not_empty", work.Refusal(work.Delete("full")));
        Assert.Equal("not_found", work.Refusal(work.Delete("absent.txt")));
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
        work.Runner.Apply([work.Delete("f.txt")]);
        work.Runner.Apply([work.Delete("empty")]);
        Assert.False(File.Exists(work.In("f.txt")));
        Assert.False(Directory.Exists(work.In("empty")));
        Assert.Empty(work.BrokerNames());
        Assert.Empty(work.JournalFiles());
    }

    [Fact]
    public void Changes_are_refused_without_a_journal_while_reads_still_work()
    {
        File.WriteAllText(work.In("f.txt"), "x");
        // A journal folder whose parent is a file can never be created.
        File.WriteAllText(Path.Combine(work.Outside, "not-a-folder"), "x");
        var runner = new BatchRunner(work.Registry, Journal.Open(Path.Combine(work.Outside, "not-a-folder", "journal")));
        Assert.False(runner.Journal.Available);
        var before = TestSupport.TreeHash(work.Root);
        Assert.Equal("journal_unavailable", Assert.Throws<BrokerRefusal>(() => runner.Apply([work.Write("f.txt", "y")])).Code);
        Assert.Equal("journal_unavailable", Assert.Throws<BrokerRefusal>(() => runner.Apply([work.Delete("f.txt")])).Code);
        Assert.Equal(before, TestSupport.TreeHash(work.Root));
        Assert.Equal("file", FileOps.Stat(work.Opened, "f.txt").Kind);
    }

    // A name is used exactly as sent or refused, never repaired: an escaped lone surrogate (which
    // Python's json writes for such a Windows name) and bytes that are not UTF-8 are refused
    // invalid_name with nothing changed; a valid non-ASCII name means the same object whether it
    // comes escaped or as raw UTF-8.
    [Fact]
    public async Task Names_that_are_not_valid_unicode_are_refused_never_repaired()
    {
        var flagged = TestSupport.NewTempDirectory();
        var journal = Path.Combine(TestSupport.NewTempDirectory(), "journal");
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            File.WriteAllText(Path.Combine(flagged, "café.txt"), "accent");
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            static string Escape(int unit) => "\\" + "u" + unit.ToString("x4", System.Globalization.CultureInfo.InvariantCulture);
            byte[] Body(string id, string op, params object[] rel) =>
            [
                .. Encoding.UTF8.GetBytes($"{{\"id\":\"{id}\",\"op\":\"{op}\",\"root_id\":\"{rootId}\",\"rel\":\""),
                .. rel.SelectMany(part => part as byte[] ?? Encoding.UTF8.GetBytes((string)part)),
                .. Encoding.UTF8.GetBytes(op == "stat" ? "\"}" : "\",\"data\":\"eA==\",\"expect\":\"absent\"}"),
            ];
            var before = TestSupport.TreeHash(flagged);
            foreach (var (id, rel) in new (string, object[])[]
            {
                ("lone_low", new object[] { "a", Escape(0xDC80), ".txt" }),
                ("lone_high", new object[] { "a", Escape(0xD83D), ".txt" }),
                ("reversed_pair", new object[] { "a", Escape(0xDE00), Escape(0xD83D), ".txt" }),
                ("not_utf8", new object[] { "a", new byte[] { 0xFF, 0xFE }, ".txt" }),
                ("overlong", new object[] { "a", new byte[] { 0xC0, 0xAF }, ".txt" }),
            })
            {
                using var refused = await TestSupport.RawRequest(client, Body(id, "write_file", rel));
                Assert.True(TestSupport.ErrorCode(refused) == "invalid_name", $"{id}: {refused.RootElement.GetRawText()}");
            }
            Assert.Equal(before, TestSupport.TreeHash(flagged));
            using var pair = await TestSupport.RawRequest(client, Body("pair", "write_file", "a", Escape(0xD83D), Escape(0xDE00), ".txt"));
            Assert.True(pair.RootElement.GetProperty("ok").GetBoolean(), pair.RootElement.GetRawText());
            Assert.True(File.Exists(Path.Combine(flagged, "a😀.txt")));

            using var escaped = await TestSupport.RawRequest(client, Body("escaped", "stat", "caf", Escape(0xE9), ".txt"));
            using var utf8 = await TestSupport.RawRequest(client, Body("utf8", "stat", "caf", new byte[] { 0xC3, 0xA9 }, ".txt"));
            Assert.True(escaped.RootElement.GetProperty("ok").GetBoolean(), escaped.RootElement.GetRawText());
            using (var handle = File.OpenHandle(Path.Combine(flagged, "café.txt")))
                Assert.Equal(Convert.ToHexString(Native.GetFileIdentity(handle).FileId).ToLowerInvariant(), escaped.RootElement.GetProperty("file_id").GetString());
            Assert.Equal(escaped.RootElement.GetProperty("file_id").GetString(), utf8.RootElement.GetProperty("file_id").GetString());
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
            TestSupport.DeleteTree(flagged);
            TestSupport.DeleteTree(Path.GetDirectoryName(journal)!);
        }
    }

    // The same operations through the real broker process and its pipe.
    [Fact]
    public async Task Operations_over_the_pipe()
    {
        var flagged = TestSupport.NewTempDirectory();
        var journal = Path.Combine(TestSupport.NewTempDirectory(), "journal");
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            Assert.True(ready.RootElement.GetProperty("journal").GetProperty("available").GetBoolean());
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            await using var other = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            var data = Convert.ToBase64String(Encoding.UTF8.GetBytes("over the pipe"));
            using var written = await TestSupport.Request(client, new { id = "w", op = "write_file", root_id = rootId, rel = "f.txt", data, expect = "absent" });
            Assert.True(written.RootElement.GetProperty("ok").GetBoolean(), written.RootElement.GetRawText());
            Assert.Equal("over the pipe", File.ReadAllText(Path.Combine(flagged, "f.txt")));
            using var read = await TestSupport.Request(client, new { id = "r", op = "read", root_id = rootId, rel = "f.txt", max_bytes = 100 });
            Assert.Equal(data, read.RootElement.GetProperty("data").GetString());
            using var stat = await TestSupport.Request(client, new { id = "s", op = "stat", root_id = rootId, rel = "f.txt" });
            Assert.Equal("file", stat.RootElement.GetProperty("kind").GetString());
            using var batch = await TestSupport.Request(client, new
            {
                id = "b", op = "apply_batch", root_id = rootId,
                ops = new object[]
                {
                    new { op = "ensure_directory", rel = "d" },
                    new { op = "rename", rel = "f.txt", to = "d\\f.txt", replace = false }
                }
            });
            Assert.True(batch.RootElement.GetProperty("ok").GetBoolean(), batch.RootElement.GetRawText());
            Assert.True(File.Exists(Path.Combine(flagged, "d", "f.txt")));
            using var stale = await TestSupport.Request(client, new { id = "x", op = "write_file", root_id = rootId, rel = "d\\f.txt", data, expect = "absent" });
            Assert.Equal("changed_since_check", TestSupport.ErrorCode(stale));
            Assert.False(stale.RootElement.TryGetProperty("batch", out _));
            using var extra = await TestSupport.Request(client, new { id = "e", op = "delete", root_id = rootId, rel = "d\\f.txt", force = true });
            Assert.Equal("invalid_request", TestSupport.ErrorCode(extra));
            using var badData = await TestSupport.Request(client, new { id = "d", op = "write_file", root_id = rootId, rel = "g.txt", data = "not base64!", expect = "any" });
            Assert.Equal("invalid_request", TestSupport.ErrorCode(badData));
            using var foreign = await TestSupport.Request(other, new { id = "f", op = "delete", root_id = rootId, rel = "d\\f.txt" });
            Assert.Equal("unknown_root", TestSupport.ErrorCode(foreign));
            using var unknownBatch = await TestSupport.Request(client, new { id = "u", op = "restore_batch", batch_id = new string('0', 32) });
            Assert.Equal("unknown_batch", TestSupport.ErrorCode(unknownBatch));
            Assert.True(File.Exists(Path.Combine(flagged, "d", "f.txt")));
            Assert.Empty(Directory.GetFiles(journal));
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
            TestSupport.DeleteTree(flagged);
            TestSupport.DeleteTree(Path.GetDirectoryName(journal)!);
        }
    }
}
