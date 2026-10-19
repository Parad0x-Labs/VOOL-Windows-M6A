using Microsoft.Win32.SafeHandles;
using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// The walk in process, on real NTFS folders under the test temp root (RULES-3b P1-P16). Each
/// test makes its own flagged folder and deletes it afterwards.
/// </summary>
public sealed class HandleWalkTests : IDisposable
{
    private readonly string root = TestSupport.NewTempDirectory();
    private readonly string outside = TestSupport.NewTempDirectory();
    private readonly RootRegistry registry = new();
    private readonly RootEntry entry;

    public HandleWalkTests() { entry = registry.GetEntry(registry.AddFlaggedRoot(root)); }

    public void Dispose()
    {
        HandleWalk.AfterComponentOpenForTests = null;
        HandleWalk.BeforeOpenForTests = null;
        registry.Dispose();
        TestSupport.DeleteTree(root);
        TestSupport.DeleteTree(outside);
    }

    private string Inside(params string[] parts) => Path.Combine([root, .. parts]);

    private static byte[] FileIdOf(string path)
    {
        using var handle = Native.CreateFileW(path, Native.FileReadAttributes, Native.ShareRead | Native.ShareWrite | Native.ShareDelete, 0,
            Native.OpenExisting, Native.FileFlagBackupSemantics | Native.FileFlagOpenReparsePoint, 0);
        Assert.False(handle.IsInvalid, $"Cannot open {path}.");
        return Native.GetFileIdentity(handle).FileId;
    }

    private string Refusal(string relative, WalkTarget target = WalkTarget.Any, WalkAccess access = WalkAccess.Read) =>
        Assert.Throws<BrokerRefusal>(() => HandleWalk.Open(entry, relative, target, access).Dispose()).Code;

    // P1, P2, P6, P10, P11, P12: refused by text, before anything is opened.
    [Theory]
    [InlineData("..", "path_escapes_root")]
    [InlineData(".", "path_escapes_root")]
    [InlineData("a\\..\\b", "path_escapes_root")]
    [InlineData("a\\.\\b", "path_escapes_root")]
    [InlineData("a//b", "path_escapes_root")]
    [InlineData("a\\", "path_escapes_root")]
    [InlineData("\\a", "path_escapes_root")]
    [InlineData("/a", "path_escapes_root")]
    [InlineData("\\\\?\\C:\\a", "path_escapes_root")]
    [InlineData("\\\\.\\C:\\a", "path_escapes_root")]
    [InlineData("\\??\\C:\\a", "path_escapes_root")]
    [InlineData("\\\\localhost\\C$\\a", "path_escapes_root")]
    [InlineData("C:\\a", "invalid_name")]
    [InlineData("C:a", "invalid_name")]
    [InlineData("a:b", "invalid_name")]
    [InlineData("a::$DATA", "invalid_name")]
    [InlineData("a\\b:stream", "invalid_name")]
    [InlineData("a.", "invalid_name")]
    [InlineData("a ", "invalid_name")]
    [InlineData("a\\b.", "invalid_name")]
    [InlineData("a*b", "invalid_name")]
    [InlineData("a?b", "invalid_name")]
    [InlineData("a<b", "invalid_name")]
    [InlineData("a>b", "invalid_name")]
    [InlineData("a|b", "invalid_name")]
    [InlineData("a\"b", "invalid_name")]
    [InlineData("CON", "device_name")]
    [InlineData("con.txt", "device_name")]
    [InlineData("Aux", "device_name")]
    [InlineData("nul.tar.gz", "device_name")]
    [InlineData("PRN", "device_name")]
    [InlineData("COM0", "device_name")]
    [InlineData("COM1.foo", "device_name")]
    [InlineData("com9", "device_name")]
    [InlineData("LPT¹", "device_name")]
    [InlineData("lpt³.log", "device_name")]
    [InlineData("COM² .txt", "device_name")]
    [InlineData("CONIN$", "device_name")]
    [InlineData("conout$.x", "device_name")]
    [InlineData("a\\CON", "device_name")]
    public void Refuses_lexically_invalid_paths_before_any_open(string path, string code)
    {
        var opens = 0;
        HandleWalk.BeforeOpenForTests = () => opens++;
        var before = TestSupport.TreeHash(root);
        Assert.Equal(code, Refusal(path));
        Assert.Equal(0, opens);
        Assert.Equal(before, TestSupport.TreeHash(root));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(31)]
    public void Control_character_in_a_name_is_refused_before_any_open(int code)
    {
        var opens = 0;
        HandleWalk.BeforeOpenForTests = () => opens++;
        Assert.Equal("invalid_name", Refusal("a" + (char)code + "b"));
        Assert.Equal(0, opens);
    }

    [Fact]
    public void Empty_path_is_refused_before_any_open()
    {
        var opens = 0;
        HandleWalk.BeforeOpenForTests = () => opens++;
        Assert.Equal("path_escapes_root", Refusal(""));
        Assert.Equal(0, opens);
    }

    // P13.
    [Fact]
    public void Path_over_32767_units_is_refused_before_any_open()
    {
        var opens = 0;
        HandleWalk.BeforeOpenForTests = () => opens++;
        var path = string.Join('\\', Enumerable.Repeat(new string('a', 200), 164));
        Assert.True(path.Length > HandleWalk.MaxPathUnits);
        Assert.Equal("path_too_long", Refusal(path));
        Assert.Equal(0, opens);
    }

    [Fact]
    public void Walk_opens_the_named_file_and_holds_every_folder_on_the_way()
    {
        Directory.CreateDirectory(Inside("a", "b"));
        File.WriteAllText(Inside("a", "b", "c.txt"), "inside");
        var expected = new FileInfo(Inside("a", "b", "c.txt"));
        using (var walk = HandleWalk.Open(entry, "a\\b\\c.txt", WalkTarget.File))
        {
            Assert.Equal(3, walk.HeldHandles);
            Assert.Equal(FileIdOf(expected.FullName), Native.GetFileIdentity(walk.FinalHandle).FileId);
            // P15: nothing on the way can be renamed or deleted while the walk holds it.
            Assert.False(TestSupport.TryRename(Inside("a"), Inside("a-moved")));
            Assert.False(TestSupport.TryRename(Inside("a", "b"), Inside("a", "b-moved")));
            Assert.False(TestSupport.TryRename(Inside("a", "b", "c.txt"), Inside("a", "b", "c-moved.txt")));
            Assert.False(File.Exists(Inside("a", "b", "c-moved.txt")));
        }
        TestSupport.Rename(Inside("a"), Inside("a-moved"));
        TestSupport.Rename(Inside("a-moved"), Inside("a"));
    }

    // P8: a different case is the same name.
    [Fact]
    public void Different_case_names_the_same_object()
    {
        Directory.CreateDirectory(Inside("Alpha", "beta"));
        using var walk = HandleWalk.Open(entry, "ALPHA\\Beta", WalkTarget.Directory);
        Assert.Equal(2, walk.HeldHandles);
    }

    // P5, for a read walk and a write walk, with the second name inside or outside the root.
    [Theory]
    [InlineData(false, WalkAccess.Read)]
    [InlineData(false, WalkAccess.Write)]
    [InlineData(true, WalkAccess.Read)]
    [InlineData(true, WalkAccess.Write)]
    public void File_with_a_second_name_is_refused(bool secondNameOutside, WalkAccess access)
    {
        var target = secondNameOutside ? Path.Combine(outside, "outside.txt") : Inside("first.txt");
        File.WriteAllText(target, "content");
        TestSupport.HardLink(Inside("linked.txt"), target);
        var before = TestSupport.TreeHash(root);
        var outsideBefore = TestSupport.TreeHash(outside);
        Assert.Equal("blocked_by_hard_link", Refusal("linked.txt", WalkTarget.File, access));
        Assert.Equal(before, TestSupport.TreeHash(root));
        Assert.Equal(outsideBefore, TestSupport.TreeHash(outside));
    }

    [Fact]
    public void File_with_one_name_is_opened_for_write()
    {
        File.WriteAllText(Inside("single.txt"), "content");
        using var walk = HandleWalk.Open(entry, "single.txt", WalkTarget.File, WalkAccess.Write);
        Assert.False(walk.FinalHandle.IsInvalid);
    }

    // P3: a junction or a symlink, on the way or at the end. The outside folder is held with no
    // sharing, so any open that reached it would fail with in_use instead of reparse_point.
    [Theory]
    [InlineData("junction", "link")]
    [InlineData("junction", "link\\secret.txt")]
    [InlineData("symlink", "link")]
    [InlineData("symlink", "link\\secret.txt")]
    public void Link_inside_the_root_is_refused_without_reaching_its_target(string kind, string relative)
    {
        File.WriteAllText(Path.Combine(outside, "secret.txt"), "outside");
        if (kind == "junction") TestSupport.CreateJunction(Inside("link"), outside);
        else TestSupport.CreateDirectorySymlinkOrSkip(Inside("link"), outside);
        var before = TestSupport.TreeHash(root);
        using (TestSupport.HoldExclusively(outside))
            Assert.Equal("reparse_point", Refusal(relative));
        Assert.Equal(before, TestSupport.TreeHash(root));
    }

    [Fact]
    public void File_symlink_is_refused()
    {
        File.WriteAllText(Path.Combine(outside, "secret.txt"), "outside");
        try { File.CreateSymbolicLink(Inside("file-link.txt"), Path.Combine(outside, "secret.txt")); }
        catch (Exception error) when (error is UnauthorizedAccessException or IOException)
        {
            Assert.Skip($"Creating a file symlink was refused on this host ({error.Message}).");
        }
        Assert.Equal("reparse_point", Refusal("file-link.txt", WalkTarget.File));
    }

    // P4: a reparse point that is not a link (a third-party tag, standing in for OneDrive, dedup
    // or AppExecLink tags, which need their drivers to create).
    [Fact]
    public void Reparse_point_of_any_tag_is_refused()
    {
        File.WriteAllBytes(Inside("placeholder.txt"), []);
        TestSupport.SetCustomReparsePointOrSkip(Inside("placeholder.txt"));
        var before = TestSupport.TreeHash(root);
        Assert.Equal("reparse_point", Refusal("placeholder.txt", WalkTarget.File));
        Assert.Equal(before, TestSupport.TreeHash(root));
    }

    // P3 under a race: after "a" is opened and checked, the test swaps a junction to the outside
    // folder in for "b". The walk must refuse it, and the outside folder must never be opened.
    [Theory]
    [InlineData("junction")]
    [InlineData("symlink")]
    public void Link_swapped_in_after_the_parent_is_checked_is_refused(string kind)
    {
        Directory.CreateDirectory(Inside("a", "b"));
        File.WriteAllText(Inside("a", "b", "secret.txt"), "inside");
        File.WriteAllText(Path.Combine(outside, "secret.txt"), "outside");
        if (kind == "symlink")
        {
            // Probe first: a skip thrown inside the walk's hook would surface as a wrong exception.
            TestSupport.CreateDirectorySymlinkOrSkip(Path.Combine(outside, "probe-link"), outside);
            Directory.Delete(Path.Combine(outside, "probe-link"));
        }
        var swapped = false;
        using var hold = TestSupport.HoldExclusively(outside);
        HandleWalk.AfterComponentOpenForTests = (index, component) =>
        {
            if (index != 0 || component != "a") return;
            TestSupport.Rename(Inside("a", "b"), Inside("a", "b-real"));
            if (kind == "junction") TestSupport.CreateJunction(Inside("a", "b"), outside);
            else TestSupport.CreateDirectorySymlinkOrSkip(Inside("a", "b"), outside);
            swapped = true;
        };
        Assert.Equal("reparse_point", Refusal("a\\b\\secret.txt", WalkTarget.File));
        Assert.True(swapped);
        Assert.Equal("outside", File.ReadAllText(Path.Combine(outside, "secret.txt")));
    }

    // P3 after the check: mklink /J makes an empty folder first and sets its reparse data second, so
    // a walk can open and check the plain folder before it becomes a junction. The open under it must
    // then refuse, not follow it (the race test below met this as NTSTATUS 0xC0000280).
    [Fact]
    public void Folder_turned_into_a_junction_after_its_check_is_refused()
    {
        Directory.CreateDirectory(Inside("empty"));
        File.WriteAllText(Path.Combine(outside, "secret.txt"), "outside");
        var turned = false;
        HandleWalk.AfterComponentOpenForTests = (index, component) =>
        {
            if (index != 0 || component != "empty") return;
            TestSupport.TurnIntoJunction(Inside("empty"), outside);
            turned = true;
        };
        Assert.Equal("reparse_point", Refusal("empty\\secret.txt", WalkTarget.File));
        Assert.True(turned);
        Assert.True(File.GetAttributes(Inside("empty")).HasFlag(FileAttributes.ReparsePoint));
    }

    // P3 under a racing thread: it keeps swapping "b" between the real folder and a junction to
    // the outside folder while walks run. Each walk refuses or returns the file inside the root.
    [Fact]
    public async Task Walks_racing_a_junction_swap_never_return_the_outside_file()
    {
        Directory.CreateDirectory(Inside("a", "b"));
        File.WriteAllText(Inside("a", "b", "secret.txt"), "inside");
        File.WriteAllText(Path.Combine(outside, "secret.txt"), "outside");
        var insideId = FileIdOf(Inside("a", "b", "secret.txt"));
        using var stop = new CancellationTokenSource();
        var swaps = 0;
        var racer = Task.Run(() =>
        {
            while (!stop.IsCancellationRequested)
            {
                // A walk may hold "b" (or the junction) for a moment; every step retries until it lands.
                if (!TestSupport.TryRename(Inside("a", "b"), Inside("a", "b-real"))) continue;
                TestSupport.CreateJunction(Inside("a", "b"), outside);
                Thread.Sleep(20);
                while (!TestSupport.TryRemoveDirectory(Inside("a", "b"))) Thread.Sleep(1);
                while (!TestSupport.TryRename(Inside("a", "b-real"), Inside("a", "b"))) Thread.Sleep(1);
                Interlocked.Increment(ref swaps);
            }
        }, TestContext.Current.CancellationToken);
        var accepted = 0;
        var refused = new Dictionary<string, int>();
        var failures = new List<string>();
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(60);
        try
        {
            while (Volatile.Read(ref swaps) < 20 && DateTime.UtcNow < deadline && !racer.IsCompleted)
            {
                try
                {
                    using var walk = HandleWalk.Open(entry, "a\\b\\secret.txt", WalkTarget.File);
                    Assert.Equal(insideId, Native.GetFileIdentity(walk.FinalHandle).FileId);
                    accepted++;
                }
                catch (BrokerRefusal refusal)
                {
                    refused[refusal.Code] = refused.GetValueOrDefault(refusal.Code) + 1;
                    // Which call failed, and how, for any refusal that is not one of the expected race outcomes.
                    if (refusal.Code == "authority_check_failed" && failures.Count < 20)
                        failures.Add(refusal.Message + " | " + string.Join(" / ", (refusal.StackTrace ?? "").Split(Environment.NewLine).Take(3).Select(line => line.Trim())));
                }
            }
        }
        finally
        {
            stop.Cancel();
            await racer.WaitAsync(TestContext.Current.CancellationToken);
        }
        foreach (var failure in failures) Console.WriteLine("race authority_check_failed: " + failure);
        Console.WriteLine($"race: {accepted} accepted, refused {string.Join(", ", refused.Select(pair => $"{pair.Key}={pair.Value}"))}, {swaps} swaps");
        Assert.True(swaps >= 20, $"The racing thread made only {swaps} swaps.");
        Assert.True(accepted > 0, "No walk ran while the real folder was in place.");
        Assert.True(refused.ContainsKey("reparse_point"), "No walk met the junction; the race did not run.");
        Assert.All(refused.Keys, code => Assert.Contains(code, new[] { "reparse_point", "not_found", "in_use" }));
        Assert.False(File.GetAttributes(Inside("a", "b")).HasFlag(FileAttributes.ReparsePoint));
    }

    // P7: an 8.3 alias opens the same object under another name.
    [Fact]
    public void Short_name_is_refused()
    {
        Directory.CreateDirectory(Inside("A long folder name for 8dot3"));
        var shortPath = TestSupport.ShortPath(Inside("A long folder name for 8dot3"));
        if (shortPath is null)
            Assert.Skip("This volume created no 8.3 name for a new folder (8.3 name creation is disabled; enabling it needs admin).");
        var before = TestSupport.TreeHash(root);
        Assert.Equal("short_name", Refusal(Path.GetFileName(shortPath!)));
        Assert.Equal(before, TestSupport.TreeHash(root));
    }

    // P9, on the way and at the end.
    [Theory]
    [InlineData("cs\\inner")]
    [InlineData("cs")]
    public void Case_sensitive_folder_is_refused(string relative)
    {
        Directory.CreateDirectory(Inside("cs"));
        TestSupport.SetCaseSensitiveOrSkip(Inside("cs"), enable: true);
        try
        {
            Directory.CreateDirectory(Inside("cs", "inner"));
            var before = TestSupport.TreeHash(root);
            Assert.Equal("case_sensitive_dir", Refusal(relative, WalkTarget.Directory));
            Assert.Equal(before, TestSupport.TreeHash(root));
        }
        finally
        {
            TestSupport.DeleteTree(Inside("cs", "inner"));
            TestSupport.SetCaseSensitiveOrSkip(Inside("cs"), enable: false);
        }
    }

    // P14: crossing to another volume needs a volume mount point, which is a reparse point (P3
    // refuses it first) and needs admin to create.
    [Fact]
    public void Object_on_another_volume_is_refused()
    {
        Assert.Skip("Reaching another volume from a relative open needs a mounted-volume folder: creating one needs admin, and it is a reparse point, which the walk refuses first (P3). The volume check stays as defence in depth.");
    }

    [Fact]
    public void Object_held_by_another_program_is_refused_in_use()
    {
        Directory.CreateDirectory(Inside("a"));
        File.WriteAllText(Inside("a", "busy.txt"), "busy");
        using (TestSupport.HoldExclusively(Inside("a", "busy.txt")))
            Assert.Equal("in_use", Refusal("a\\busy.txt", WalkTarget.File));
        using (TestSupport.HoldExclusively(Inside("a")))
            Assert.Equal("in_use", Refusal("a\\busy.txt", WalkTarget.File));
    }

    [Fact]
    public void Missing_wrong_kind_and_file_on_the_way_are_refused()
    {
        Directory.CreateDirectory(Inside("dir"));
        File.WriteAllText(Inside("file.txt"), "x");
        Assert.Equal("not_found", Refusal("absent"));
        Assert.Equal("not_found", Refusal("absent\\x"));
        Assert.Equal("not_a_directory", Refusal("file.txt\\x"));
        Assert.Equal("not_a_directory", Refusal("file.txt", WalkTarget.Directory));
        Assert.Equal("is_a_directory", Refusal("dir", WalkTarget.File));
    }

    [Fact]
    public void Failed_walk_releases_every_handle_it_opened()
    {
        Directory.CreateDirectory(Inside("a", "b"));
        Assert.Equal("not_found", Refusal("a\\b\\absent"));
        TestSupport.Rename(Inside("a"), Inside("a-moved"));
        TestSupport.Rename(Inside("a-moved"), Inside("a"));
    }

    [Fact]
    public void Walk_handles_are_not_inheritable()
    {
        Directory.CreateDirectory(Inside("a"));
        using var walk = HandleWalk.Open(entry, "a", WalkTarget.Directory);
        var table = TestSupport.ReadHandleTable(Environment.ProcessId).ToDictionary(item => item.Value);
        var value = (ulong)walk.FinalHandle.DangerousGetHandle();
        Assert.True(table.TryGetValue(value, out var held), "The walk's handle is not in the handle table.");
        Assert.False(held.Inheritable);
        Assert.False(table[(ulong)entry.Handle.DangerousGetHandle()].Inheritable);
    }
}
