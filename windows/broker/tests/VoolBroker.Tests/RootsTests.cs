using System.Diagnostics;
using System.Text.Json;
using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// Flagged folders and roots through the real broker process and its pipe (RULES-3b F1-F10, P15,
/// open_root and close_root).
/// </summary>
public sealed class RootsTests : IDisposable
{
    private readonly string temp = TestSupport.NewTempDirectory();
    private readonly string journal;

    public RootsTests()
    {
        journal = Path.Combine(temp, "journal");
        Directory.CreateDirectory(journal);
    }

    public void Dispose() => TestSupport.DeleteTree(temp);

    private string NewFolder(string name)
    {
        var path = Path.Combine(temp, name);
        Directory.CreateDirectory(path);
        return path;
    }

    private static string Code(JsonDocument refusal) => refusal.RootElement.GetProperty("error").GetProperty("code").GetString()!;

    // F1, F2, F3, F5, F6, case-sensitive: each refused entry stops the start, names the entry, and creates no pipe.
    [Theory]
    [InlineData("relative", "invalid_flagged")]
    [InlineData("missing", "invalid_flagged")]
    [InlineData("file", "invalid_flagged")]
    [InlineData("junction", "reparse_point")]
    [InlineData("symlink", "reparse_point")]
    [InlineData("volume_root", "invalid_flagged")]
    [InlineData("unc", "unsupported_filesystem")]
    [InlineData("long_unc", "unsupported_filesystem")]
    [InlineData("device", "unsupported_filesystem")]
    [InlineData("case_sensitive", "case_sensitive_dir")]
    [InlineData("volume_guid", "invalid_flagged")]
    [InlineData("mapped_drive", "unsupported_filesystem")]
    [InlineData("embedded_nul", "invalid_flagged")]
    public async Task Refused_flagged_entry_stops_the_start_without_a_pipe(string kind, string code)
    {
        // F5: windows-broker.yml maps a local share as a drive letter and names a folder on it.
        if (kind == "mapped_drive" && TestSupport.AdminFolder("VOOL_BROKER_TEST_MAPPED_ROOT") is null)
            TestSupport.SkipOrFailAdminRow("A mapped network drive needs a share, and making one needs admin.");
        var target = NewFolder("target");
        var entry = kind switch
        {
            "relative" => "relative\\folder",
            "missing" => Path.Combine(temp, "absent"),
            "file" => Path.Combine(temp, "file.txt"),
            "junction" or "symlink" => Path.Combine(temp, "link"),
            "volume_root" => Path.GetPathRoot(temp)!,
            "unc" => "\\\\localhost\\" + temp[0] + "$" + temp[2..],
            "long_unc" => "\\\\?\\UNC\\localhost\\" + temp[0] + "$" + temp[2..],
            "device" => "\\\\.\\" + temp,
            "case_sensitive" => NewFolder("cs"),
            // F1: fully qualified and it opens the folder, but has no drive letter.
            "volume_guid" => TestSupport.VolumeGuidSpelling(target),
            "mapped_drive" => TestSupport.AdminFolder("VOOL_BROKER_TEST_MAPPED_ROOT")!,
            // Windows reads a path only up to a NUL, so this would open target itself (review F2).
            "embedded_nul" => target + "\0\\inner",
            _ => throw new ArgumentOutOfRangeException(nameof(kind))
        };
        if (kind == "file") File.WriteAllText(entry, "x");
        if (kind == "junction") TestSupport.CreateJunction(entry, target);
        if (kind == "symlink") TestSupport.CreateDirectorySymlinkOrSkip(entry, target);
        if (kind == "case_sensitive") TestSupport.SetCaseSensitiveOrSkip(entry, enable: true);
        try
        {
            var before = TestSupport.TreeHash(temp);
            var pipes = TestSupport.BrokerPipes();
            using var refusal = await TestSupport.StartRefused(TestSupport.DefaultBootstrap([entry], journal));
            Assert.Equal(code, Code(refusal));
            Assert.Equal(entry, refusal.RootElement.GetProperty("error").GetProperty("entry").GetString());
            Assert.Empty(TestSupport.BrokerPipes().Except(pipes));
            Assert.Equal(before, TestSupport.TreeHash(temp));
        }
        finally
        {
            if (kind == "case_sensitive") TestSupport.SetCaseSensitiveOrSkip(entry, enable: false);
        }
    }

    // A journal folder holding a NUL is refused, never truncated to the part before it (review F2).
    [Fact]
    public async Task A_journal_folder_with_a_NUL_is_refused()
    {
        var good = NewFolder("good");
        var journalDirectory = journal + "\0x";
        var pipes = TestSupport.BrokerPipes();
        using var refusal = await TestSupport.StartRefused(TestSupport.DefaultBootstrap([good], journalDirectory));
        Assert.Equal("invalid_bootstrap", Code(refusal));
        Assert.Equal(journalDirectory, refusal.RootElement.GetProperty("error").GetProperty("entry").GetString());
        Assert.Empty(TestSupport.BrokerPipes().Except(pipes));
    }

    // F10: one refused entry among good ones refuses the whole start, naming that entry.
    [Fact]
    public async Task One_refused_entry_refuses_the_whole_start()
    {
        var good = NewFolder("good");
        var bad = Path.Combine(temp, "absent");
        var pipes = TestSupport.BrokerPipes();
        using var refusal = await TestSupport.StartRefused(TestSupport.DefaultBootstrap([good, bad, NewFolder("good2")], journal));
        Assert.Equal("invalid_flagged", Code(refusal));
        Assert.Equal(bad, refusal.RootElement.GetProperty("error").GetProperty("entry").GetString());
        Assert.Empty(TestSupport.BrokerPipes().Except(pipes));
    }

    // F9: the journal folder inside a flagged folder, by its plain path, through a junction, or not created yet.
    [Theory]
    [InlineData("plain")]
    [InlineData("through_junction")]
    [InlineData("not_created_yet")]
    [InlineData("flagged_itself")]
    public async Task Journal_inside_a_flagged_folder_is_refused(string kind)
    {
        var flagged = NewFolder("flagged");
        Directory.CreateDirectory(Path.Combine(flagged, "journal"));
        string journalDirectory;
        switch (kind)
        {
            case "plain": journalDirectory = Path.Combine(flagged, "journal"); break;
            case "through_junction":
                TestSupport.CreateJunction(Path.Combine(temp, "alias"), flagged);
                journalDirectory = Path.Combine(temp, "alias", "journal");
                break;
            case "not_created_yet": journalDirectory = Path.Combine(flagged, "later", "journal"); break;
            default: journalDirectory = flagged; break;
        }
        var pipes = TestSupport.BrokerPipes();
        using var refusal = await TestSupport.StartRefused(TestSupport.DefaultBootstrap([flagged], journalDirectory));
        Assert.Equal("journal_inside_flagged", Code(refusal));
        Assert.Empty(TestSupport.BrokerPipes().Except(pipes));
        Assert.False(Directory.Exists(Path.Combine(flagged, "later")));
    }

    // F4: other spellings of one folder get its id, and every reported path is the long final path.
    [Fact]
    public async Task Spellings_of_one_folder_share_its_id_and_report_its_final_path()
    {
        var flagged = NewFolder("Flagged Folder [1]");
        var spellings = new[] { flagged, flagged.ToUpperInvariant(), "\\\\?\\" + flagged, flagged + "\\" };
        var bootstrap = TestSupport.DefaultBootstrap(spellings, journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var reported = ready.RootElement.GetProperty("flagged").EnumerateArray().ToArray();
            Assert.Equal(spellings.Length, reported.Length);
            var id = reported[0].GetProperty("id").GetString()!;
            Assert.Equal(48, id.Length);
            Assert.All(id, ch => Assert.True(char.IsAsciiHexDigitLower(ch)));
            Assert.All(reported, item => Assert.Equal(id, item.GetProperty("id").GetString()));
            var path = reported[0].GetProperty("path").GetString()!;
            Assert.All(reported, item => Assert.Equal(path, item.GetProperty("path").GetString()));
            Assert.False(path.StartsWith("\\\\?\\", StringComparison.Ordinal));
            Assert.Equal(new DirectoryInfo(flagged).FullName.TrimEnd('\\'), path, ignoreCase: true);
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task Subst_drive_spelling_shares_the_folder_id()
    {
        var flagged = NewFolder("subst-target");
        var letter = Enumerable.Range('D', 'Z' - 'D' + 1).Select(c => (char)c).Reverse()
            .FirstOrDefault(c => !Directory.Exists(c + ":\\") && !DriveInfo.GetDrives().Any(d => d.Name[0] == c));
        if (letter == default) Assert.Skip("No free drive letter for a subst drive.");
        var drive = letter + ":";
        var made = Subst(drive, flagged);
        if (made != 0) Assert.Skip($"subst {drive} was refused on this host (exit {made}).");
        try
        {
            var (broker, ready) = await TestSupport.StartReady(TestSupport.DefaultBootstrap([flagged, drive + "\\"], journal));
            try
            {
                var reported = ready.RootElement.GetProperty("flagged").EnumerateArray().ToArray();
                Assert.Equal(reported[0].GetProperty("id").GetString(), reported[1].GetProperty("id").GetString());
                Assert.Equal(reported[0].GetProperty("path").GetString(), reported[1].GetProperty("path").GetString());
            }
            finally { ready.Dispose(); await TestSupport.Kill(broker); }
        }
        finally { Assert.Equal(0, Subst(drive, null)); }
    }

    [Fact]
    public async Task Short_name_spelling_shares_the_folder_id()
    {
        var parent = TestSupport.NewShortNameDirectory();
        try
        {
            var flagged = Path.Combine(parent, "A long flagged folder name");
            Directory.CreateDirectory(flagged);
            var shortPath = TestSupport.ShortPath(flagged);
            if (shortPath is null)
                TestSupport.SkipOrFailAdminRow("This volume created no 8.3 name for a new folder (8.3 name creation is disabled; enabling it needs admin).");
            var (broker, ready) = await TestSupport.StartReady(TestSupport.DefaultBootstrap([flagged, shortPath!], journal));
            try
            {
                var reported = ready.RootElement.GetProperty("flagged").EnumerateArray().ToArray();
                Assert.Equal(reported[0].GetProperty("id").GetString(), reported[1].GetProperty("id").GetString());
                Assert.Equal(reported[0].GetProperty("path").GetString(), reported[1].GetProperty("path").GetString());
            }
            finally { ready.Dispose(); await TestSupport.Kill(broker); }
        }
        finally { TestSupport.DeleteTree(parent); }
    }

    // F8: nested flagged folders are both accepted, each with its own id.
    [Fact]
    public async Task Nested_flagged_folders_keep_their_own_ids()
    {
        var outer = NewFolder("outer");
        var inner = Path.Combine(outer, "inner");
        Directory.CreateDirectory(inner);
        var (broker, ready) = await TestSupport.StartReady(TestSupport.DefaultBootstrap([outer, inner], journal));
        try
        {
            var reported = ready.RootElement.GetProperty("flagged").EnumerateArray().ToArray();
            Assert.NotEqual(reported[0].GetProperty("id").GetString(), reported[1].GetProperty("id").GetString());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    // F7: a non-NTFS volume. windows-broker.yml makes a FAT32, an exFAT and a ReFS volume, each
    // with a folder.
    [Theory]
    [InlineData("VOOL_BROKER_TEST_FAT32_ROOT")]
    [InlineData("VOOL_BROKER_TEST_EXFAT_ROOT")]
    [InlineData("VOOL_BROKER_TEST_REFS_ROOT")]
    public async Task Flagged_folder_on_a_volume_that_is_not_NTFS_is_refused(string variable)
    {
        var flagged = TestSupport.AdminFolder(variable);
        if (flagged is null)
        {
            const string reason = "No FAT, exFAT or ReFS volume is reachable inside the VOOL Windows folder, and making one needs admin. The file-system check is in RootRegistry.AddFlaggedRoot.";
            TestSupport.SkipOrFailAdminRow(reason);
            return;
        }
        var pipes = TestSupport.BrokerPipes();
        using var refusal = await TestSupport.StartRefused(TestSupport.DefaultBootstrap([flagged], journal));
        Console.WriteLine($"{variable}: {refusal.RootElement.GetRawText()}");
        Assert.Equal("unsupported_filesystem", Code(refusal));
        Assert.Equal(flagged, refusal.RootElement.GetProperty("error").GetProperty("entry").GetString());
        Assert.Empty(TestSupport.BrokerPipes().Except(pipes));
    }

    // The held flagged folder by itself, with no walk under it: it cannot be renamed or deleted
    // while the broker runs, and can be once the broker is gone.
    [Fact]
    public async Task Flagged_folder_cannot_be_renamed_or_deleted_while_the_broker_runs()
    {
        var flagged = NewFolder("held-flagged");
        var (broker, ready) = await TestSupport.StartReady(TestSupport.DefaultBootstrap([flagged], journal));
        bool renamed, deleted;
        try
        {
            renamed = TestSupport.TryRename(flagged, flagged + "-moved");
            if (renamed) TestSupport.Rename(flagged + "-moved", flagged);
            deleted = TestSupport.TryRemoveDirectory(flagged);
            if (deleted) Directory.CreateDirectory(flagged);
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
        Assert.False(renamed, "The empty flagged folder was renamed while the broker held it.");
        Assert.False(deleted, "The empty flagged folder was deleted while the broker held it.");
        TestSupport.Rename(flagged, flagged + "-moved");
        TestSupport.Rename(flagged + "-moved", flagged);
    }

    // P15: open_root holds every folder from the flagged folder down to the root it returns.
    [Fact]
    public async Task Open_root_holds_every_folder_on_the_way_until_close_root()
    {
        var parent = NewFolder("parent");
        var flagged = Path.Combine(parent, "flagged");
        var b = Path.Combine(flagged, "a", "b");
        Directory.CreateDirectory(b);
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "a\\b" });
            Assert.True(opened.RootElement.GetProperty("ok").GetBoolean());
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            Assert.Equal(64, rootId.Length);
            Assert.DoesNotContain("flagged", rootId, StringComparison.OrdinalIgnoreCase);

            var observations = new Dictionary<string, bool>
            {
                ["rename b"] = TryRenameAndRestore(b, b + "-moved"),
                ["delete empty b"] = TryDeleteAndRestore(b),
                ["rename a"] = TryRenameAndRestore(Path.Combine(flagged, "a"), Path.Combine(flagged, "a-moved")),
                ["rename flagged"] = TryRenameAndRestore(flagged, flagged + "-moved"),
                ["rename flagged's parent"] = TryRenameAndRestore(parent, parent + "-moved"),
            };
            Console.WriteLine("While open_root held a\\b: " + string.Join(", ", observations.Select(pair => $"{pair.Key} {(pair.Value ? "succeeded" : "refused")}")));
            Assert.False(observations["rename b"], "The root open_root returned was renamed while held.");
            Assert.False(observations["delete empty b"], "The root open_root returned was deleted while held.");
            Assert.False(observations["rename a"], "A folder on the way was renamed while held.");
            Assert.False(observations["rename flagged"], "The flagged folder was renamed while held.");

            using var closed = await TestSupport.Request(client, new { id = "close", op = "close_root", root_id = rootId });
            Assert.True(closed.RootElement.GetProperty("ok").GetBoolean());
            TestSupport.Rename(b, b + "-moved");
            TestSupport.Rename(b + "-moved", b);
            TestSupport.Rename(Path.Combine(flagged, "a"), Path.Combine(flagged, "a-moved"));
            TestSupport.Rename(Path.Combine(flagged, "a-moved"), Path.Combine(flagged, "a"));
            using var again = await TestSupport.Request(client, new { id = "close-again", op = "close_root", root_id = rootId });
            Assert.Equal("unknown_root", TestSupport.ErrorCode(again));
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task Roots_are_released_when_their_connection_closes()
    {
        var flagged = NewFolder("flagged");
        var a = Path.Combine(flagged, "a");
        Directory.CreateDirectory(Path.Combine(a, "b"));
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using (var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap)))
            {
                using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "a\\b" });
                Assert.True(opened.RootElement.GetProperty("ok").GetBoolean());
                Assert.False(TestSupport.TryRename(a, a + "-moved"));
            }
            var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
            while (!TestSupport.TryRename(a, a + "-moved"))
            {
                Assert.True(DateTime.UtcNow < deadline, "The root was still held 10 s after its connection closed.");
                await Task.Delay(50, TestContext.Current.CancellationToken);
            }
            TestSupport.Rename(a + "-moved", a);
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task Open_root_with_an_empty_path_is_the_flagged_folder_itself()
    {
        var flagged = NewFolder("flagged");
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var opened = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = opened.RootElement.GetProperty("root_id").GetString()!;
            using var closed = await TestSupport.Request(client, new { id = "close", op = "close_root", root_id = rootId });
            Assert.True(closed.RootElement.GetProperty("ok").GetBoolean());
            // Closing that root must not close the flagged folder's own handle.
            Assert.False(TestSupport.TryRename(flagged, flagged + "-moved"));
            using var reopened = await TestSupport.Request(client, new { id = "reopen", op = "open_root", flagged_id = flaggedId, rel = "" });
            Assert.True(reopened.RootElement.GetProperty("ok").GetBoolean());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    // Refusals over the channel: each answers with its code, keeps the connection, and changes nothing.
    [Fact]
    public async Task Open_root_and_close_root_refusals_change_nothing()
    {
        var flagged = NewFolder("flagged");
        Directory.CreateDirectory(Path.Combine(flagged, "a", "b"));
        File.WriteAllText(Path.Combine(flagged, "file.txt"), "x");
        TestSupport.CreateJunction(Path.Combine(flagged, "link"), NewFolder("elsewhere"));
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            await using var other = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var othersRoot = await TestSupport.Request(other, new { id = "o", op = "open_root", flagged_id = flaggedId, rel = "a" });
            var othersRootId = othersRoot.RootElement.GetProperty("root_id").GetString()!;
            var before = TestSupport.TreeHash(flagged);
            var cases = new (object Request, string Code)[]
            {
                (new { id = "1", op = "open_root", flagged_id = "0000000000000000" + new string('0', 32), rel = "a" }, "unknown_root"),
                (new { id = "2", op = "open_root", flagged_id = flaggedId, rel = "..\\outside" }, "path_escapes_root"),
                (new { id = "3", op = "open_root", flagged_id = flaggedId, rel = "a\\..\\..\\x" }, "path_escapes_root"),
                (new { id = "4", op = "open_root", flagged_id = flaggedId, rel = "file.txt" }, "not_a_directory"),
                (new { id = "5", op = "open_root", flagged_id = flaggedId, rel = "absent" }, "not_found"),
                (new { id = "6", op = "open_root", flagged_id = flaggedId, rel = "link" }, "reparse_point"),
                (new { id = "7", op = "open_root", flagged_id = flaggedId, rel = "CON" }, "device_name"),
                (new { id = "8", op = "open_root", flagged_id = flaggedId, rel = 5 }, "invalid_request"),
                (new { id = "9", op = "open_root", flagged_id = flaggedId }, "invalid_request"),
                (new { id = "10", op = "open_root", flagged_id = flaggedId, rel = "a", extra = true }, "invalid_request"),
                (new { id = "11", op = "close_root", root_id = "not-a-root" }, "unknown_root"),
                (new { id = "12", op = "close_root", root_id = othersRootId }, "unknown_root"),
                (new { id = "13", op = "ping", extra = true }, "invalid_request"),
            };
            foreach (var (request, code) in cases)
            {
                using var response = await TestSupport.Request(client, request);
                Assert.False(response.RootElement.GetProperty("ok").GetBoolean());
                Assert.Equal(code, TestSupport.ErrorCode(response));
            }
            using var ping = await TestSupport.Request(client, new { id = "ping", op = "ping" });
            Assert.True(ping.RootElement.GetProperty("ok").GetBoolean());
            Assert.Equal(before, TestSupport.TreeHash(flagged));
            // The other connection's root was not closed by the refused close_root.
            Assert.False(TestSupport.TryRename(Path.Combine(flagged, "a"), Path.Combine(flagged, "a-moved")));
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public async Task Folder_held_by_another_program_is_refused_in_use()
    {
        var flagged = NewFolder("flagged");
        Directory.CreateDirectory(Path.Combine(flagged, "a", "b"));
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using (TestSupport.HoldExclusively(Path.Combine(flagged, "a", "b")))
            {
                using var refused = await TestSupport.Request(client, new { id = "busy", op = "open_root", flagged_id = flaggedId, rel = "a\\b" });
                Assert.Equal("in_use", TestSupport.ErrorCode(refused));
            }
            using var opened = await TestSupport.Request(client, new { id = "free", op = "open_root", flagged_id = flaggedId, rel = "a\\b" });
            Assert.True(opened.RootElement.GetProperty("ok").GetBoolean());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    // A connection holds at most 64 roots: the next open_root is refused, the roots it holds stay
    // open and usable, and closing one frees a place.
    [Fact]
    public async Task A_connection_holds_at_most_64_roots()
    {
        var flagged = NewFolder("flagged");
        var a = Path.Combine(flagged, "a");
        Directory.CreateDirectory(a);
        var bootstrap = TestSupport.DefaultBootstrap([flagged], journal);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        try
        {
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            var roots = new List<string>();
            for (var index = 0; index < Channel.MaxRootsPerConnection; index++)
            {
                using var opened = await TestSupport.Request(client, new { id = "open" + index, op = "open_root", flagged_id = flaggedId, rel = "a" });
                Assert.True(opened.RootElement.GetProperty("ok").GetBoolean(), opened.RootElement.GetRawText());
                roots.Add(opened.RootElement.GetProperty("root_id").GetString()!);
            }
            using var refused = await TestSupport.Request(client, new { id = "over", op = "open_root", flagged_id = flaggedId, rel = "a" });
            Assert.Equal("too_many_roots", TestSupport.ErrorCode(refused));
            Assert.False(TestSupport.TryRename(a, a + "-moved"));
            using var stat = await TestSupport.Request(client, new { id = "stat", op = "stat", root_id = roots[0], rel = "" });
            Assert.True(stat.RootElement.GetProperty("ok").GetBoolean(), stat.RootElement.GetRawText());
            using var closed = await TestSupport.Request(client, new { id = "close", op = "close_root", root_id = roots[0] });
            Assert.True(closed.RootElement.GetProperty("ok").GetBoolean());
            using var again = await TestSupport.Request(client, new { id = "again", op = "open_root", flagged_id = flaggedId, rel = "a" });
            Assert.True(again.RootElement.GetProperty("ok").GetBoolean(), again.RootElement.GetRawText());
        }
        finally { ready.Dispose(); await TestSupport.Kill(broker); }
    }

    [Fact]
    public void Platform_below_Windows_10_1809_is_refused()
    {
        if (OperatingSystem.IsWindowsVersionAtLeast(10, 0, 17763))
            Assert.Skip($"This host is Windows build {Environment.OSVersion.Version.Build}, at or above 17763; the refusal (unsupported_windows) cannot be reached here.");
        Assert.Equal("unsupported_windows", Assert.Throws<BrokerRefusal>(RootRegistry.ValidatePlatform).Code);
    }

    // A flagged entry whose real path holds an unpaired surrogate (here reached through a junction
    // above the entry, so the entry text itself is valid) cannot go on the start line unchanged:
    // the start is refused invalid_name, never repaired and never a crash (coordinator, 20 Oct).
    [Fact]
    public async Task A_flagged_folder_whose_real_path_is_not_valid_unicode_stops_the_start()
    {
        var odd = Path.Combine(temp, "odd\uDC80name");
        Directory.CreateDirectory(Path.Combine(odd, "flag"));
        Assert.Contains(Directory.GetDirectories(temp), path => path == odd);
        var link = NewFolder("link");
        TestSupport.TurnIntoJunction(link, odd);
        using var refusal = await TestSupport.StartRefused(TestSupport.DefaultBootstrap([Path.Combine(link, "flag")], journal));
        Assert.Equal("invalid_name", Code(refusal));
        Assert.Equal(Path.Combine(link, "flag"), refusal.RootElement.GetProperty("error").GetProperty("entry").GetString());
    }

    private static bool TryRenameAndRestore(string source, string destination)
    {
        if (!TestSupport.TryRename(source, destination)) return false;
        TestSupport.Rename(destination, source);
        return true;
    }

    private static bool TryDeleteAndRestore(string directory)
    {
        if (!TestSupport.TryRemoveDirectory(directory)) return false;
        Directory.CreateDirectory(directory);
        return true;
    }

    private static int Subst(string drive, string? target)
    {
        var start = new ProcessStartInfo("subst.exe") { UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
        start.ArgumentList.Add(drive);
        if (target is null) start.ArgumentList.Add("/d"); else start.ArgumentList.Add(target);
        using var subst = Process.Start(start)!;
        subst.StandardOutput.ReadToEnd();
        subst.StandardError.ReadToEnd();
        subst.WaitForExit();
        return subst.ExitCode;
    }
}
