using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using VoolBroker.Core;
using Xunit;

namespace VoolBroker.Tests;

/// <summary>
/// The journal folder is held (Phase C pick 19): opened once at start, without delete sharing, and
/// every journal file is reached through that handle, never by the path again. A same-user process
/// can then neither rename, delete or replace the folder, nor swap a junction in above it, to hand
/// Pending, Holds or restore_batch a crafted journal. The layout is VOOL_HOME\broker\journal, as
/// the API passes it.
/// </summary>
public sealed class JournalHandleTests : IDisposable
{
    private const int ErrorAccessDenied = 5;
    private const int ErrorSharingViolation = 32;

    private readonly string home = TestSupport.NewTempDirectory();
    private readonly List<Journal> opened = [];

    private string BrokerFolder => Path.Combine(home, "broker");
    private string JournalFolder => Path.Combine(BrokerFolder, "journal");

    public void Dispose()
    {
        foreach (var journal in opened) TestSupport.Release(journal);
        TestSupport.DeleteTree(home);
    }

    private Journal Open(string folder)
    {
        var journal = Journal.Open(folder);
        opened.Add(journal);
        return journal;
    }

    /// <summary>A batch file of one begin record: what recovery reads and, with no step, finishes by deleting.</summary>
    private static byte[] BeginOnly(string batchId) =>
        Encoding.UTF8.GetBytes(JsonSerializer.Serialize(new { t = "begin", batch = batchId, ops = 1 }) + "\n");

    /// <summary>A crafted folder holding a crafted pending batch, ready to be swapped in; returns the batch id.</summary>
    private string Crafted(out string craftedJournal)
    {
        craftedJournal = Path.Combine(home, "crafted", "journal");
        Directory.CreateDirectory(craftedJournal);
        var batchId = Journal.NewId();
        File.WriteAllBytes(Path.Combine(craftedJournal, $"batch-{batchId}.jsonl"), BeginOnly(batchId));
        return batchId;
    }

    /// <summary>
    /// Renames VOOL_HOME\broker aside and puts a junction to the crafted folder in its place. Returns
    /// false when the rename itself is refused (NTFS may refuse renaming a folder with an open child),
    /// and checks it was refused for that reason.
    /// </summary>
    private bool SwapBrokerFolderForJunction(string craftedJournal)
    {
        if (!TestSupport.TryRename(BrokerFolder, Path.Combine(home, "broker-aside")))
        {
            var error = Marshal.GetLastWin32Error();
            Assert.True(error is ErrorAccessDenied or ErrorSharingViolation, $"Renaming {BrokerFolder} failed with Win32 error {error}, not a refusal for the held folder.");
            return false;
        }
        TestSupport.CreateJunction(BrokerFolder, Path.GetDirectoryName(craftedJournal)!);
        // By path, the journal folder is now the crafted one.
        Assert.Single(Directory.GetFiles(JournalFolder));
        return true;
    }

    // (1) While the journal is open, its folder can be neither renamed nor deleted (a sharing
    // violation, or access denied), and the journal works as before: begin, append, read,
    // pending, holds and the delete at the end of recovery.
    [Fact]
    public void The_held_journal_folder_cannot_be_renamed_or_deleted_and_still_works()
    {
        var journal = Open(JournalFolder);
        Assert.True(journal.Available);

        var removed = TestSupport.TryRemoveDirectory(JournalFolder);
        var removeError = Marshal.GetLastWin32Error();
        Assert.False(removed, "The empty journal folder was deleted while the journal held it.");
        Assert.True(removeError is ErrorAccessDenied or ErrorSharingViolation, $"Deleting the journal folder failed with Win32 error {removeError}.");

        var renamed = TestSupport.TryRename(JournalFolder, Path.Combine(BrokerFolder, "journal-aside"));
        var renameError = Marshal.GetLastWin32Error();
        Assert.False(renamed, "The journal folder was renamed while the journal held it.");
        Assert.True(renameError is ErrorAccessDenied or ErrorSharingViolation, $"Renaming the journal folder failed with Win32 error {renameError}.");
        Assert.True(Directory.Exists(JournalFolder));

        var batchId = Journal.NewId();
        using (var log = journal.Begin(batchId, 1)) { }
        Assert.Equal(batchId, Assert.Single(journal.Pending()));
        Assert.True(journal.Holds(batchId));
        Assert.Equal(BeginOnly(batchId), File.ReadAllBytes(Path.Combine(JournalFolder, $"batch-{batchId}.jsonl")));

        using var registry = new RootRegistry();
        var outcome = journal.Recover(batchId, registry);
        Assert.True(outcome.Complete, string.Join("; ", outcome.Left));
        Assert.False(outcome.Committed);
        Assert.Empty(journal.Pending());
        Assert.False(journal.Holds(batchId));
        Assert.Empty(Directory.GetFileSystemEntries(JournalFolder));
    }

    // (2) A junction swapped in for a folder above the journal (VOOL_HOME\broker renamed aside, a
    // junction to a crafted folder with a crafted pending batch in its place) is never used:
    // Pending, Holds and restore never see the crafted batch, which is left as it was, and a new
    // batch goes to the original folder. Or the rename itself is refused.
    [Fact]
    public void A_junction_swapped_in_above_the_journal_folder_is_never_used()
    {
        var journal = Open(JournalFolder);
        Assert.True(journal.Available);
        var craftedId = Crafted(out var craftedJournal);
        var craftedFile = Path.Combine(craftedJournal, $"batch-{craftedId}.jsonl");
        var swapped = SwapBrokerFolderForJunction(craftedJournal);
        TestContext.Current.TestOutputHelper?.WriteLine(swapped ? "The rename was allowed; the junction is in place." : "The rename was refused.");

        Assert.Empty(journal.Pending());
        Assert.False(journal.Holds(craftedId));
        using var registry = new RootRegistry();
        var runner = new BatchRunner(registry, journal);
        Assert.Equal("unknown_batch", Assert.Throws<BrokerRefusal>(() => runner.Restore(craftedId)).Code);
        Assert.Equal(BeginOnly(craftedId), File.ReadAllBytes(craftedFile));

        // The journal still works, in the folder it holds.
        var original = swapped ? Path.Combine(home, "broker-aside", "journal") : JournalFolder;
        var batchId = Journal.NewId();
        using (var log = journal.Begin(batchId, 1)) { }
        Assert.Equal(batchId, Assert.Single(journal.Pending()));
        Assert.True(File.Exists(Path.Combine(original, $"batch-{batchId}.jsonl")));
        Assert.Equal(craftedFile, Assert.Single(Directory.GetFiles(craftedJournal)));
        Assert.True(runner.Restore(batchId).Complete);
        Assert.Empty(Directory.GetFileSystemEntries(original));
        Assert.Equal(BeginOnly(craftedId), File.ReadAllBytes(craftedFile));
    }

    // (2) The same swap against the real broker process, as the API uses it: restore_batch of the
    // crafted batch is refused unknown_batch, a change is not held up by it (journal_pending), and
    // the crafted journal is left as it was.
    [Fact]
    public async Task Restore_batch_over_the_pipe_never_reads_a_journal_swapped_in_above_the_folder()
    {
        var flagged = Path.Combine(home, "flagged");
        Directory.CreateDirectory(flagged);
        var craftedId = Crafted(out var craftedJournal);
        var craftedFile = Path.Combine(craftedJournal, $"batch-{craftedId}.jsonl");
        var bootstrap = TestSupport.DefaultBootstrap([flagged], JournalFolder);
        var (broker, ready) = await TestSupport.StartReady(bootstrap);
        bool swapped;
        try
        {
            Assert.True(ready.RootElement.GetProperty("journal").GetProperty("available").GetBoolean());
            swapped = SwapBrokerFolderForJunction(craftedJournal);
            TestContext.Current.TestOutputHelper?.WriteLine(swapped ? "The rename was allowed; the junction is in place." : "The rename was refused.");
            var flaggedId = ready.RootElement.GetProperty("flagged")[0].GetProperty("id").GetString()!;
            await using var client = await TestSupport.ConnectAuthenticated(ready, TestSupport.SecretOf(bootstrap));
            using var restored = await TestSupport.Request(client, new { id = "r", op = "restore_batch", batch_id = craftedId });
            Assert.True(TestSupport.ErrorCode(restored) == "unknown_batch", restored.RootElement.GetRawText());
            using var root = await TestSupport.Request(client, new { id = "open", op = "open_root", flagged_id = flaggedId, rel = "" });
            var rootId = root.RootElement.GetProperty("root_id").GetString()!;
            using var written = await TestSupport.Request(client, new { id = "w", op = "write_file", root_id = rootId, rel = "f.txt", data = "eA==", expect = "absent" });
            Assert.True(written.RootElement.GetProperty("ok").GetBoolean(), written.RootElement.GetRawText());
        }
        finally
        {
            ready.Dispose();
            await TestSupport.Kill(broker);
        }
        Assert.Equal("x", File.ReadAllText(Path.Combine(flagged, "f.txt")));
        Assert.Equal(BeginOnly(craftedId), File.ReadAllBytes(craftedFile));
        Assert.Equal(craftedFile, Assert.Single(Directory.GetFiles(craftedJournal)));
        var original = swapped ? Path.Combine(home, "broker-aside", "journal") : JournalFolder;
        Assert.Empty(Directory.GetFileSystemEntries(original));
    }

    // (3) The start probe works through the held folder: Open creates the missing folders, proves
    // a file can be written and removed, and leaves nothing behind; a batch file is then deleted
    // through the folder handle when recovery finishes it.
    [Fact]
    public void The_start_probe_and_the_delete_work_through_the_held_folder()
    {
        var journal = Open(JournalFolder);
        Assert.True(journal.Available);
        Assert.True(Directory.Exists(JournalFolder));
        Assert.Empty(Directory.GetFileSystemEntries(JournalFolder));

        var batchId = Journal.NewId();
        using (var log = journal.Begin(batchId, 1)) { }
        Assert.Single(Directory.GetFiles(JournalFolder));
        using var registry = new RootRegistry();
        Assert.True(new BatchRunner(registry, journal).Restore(batchId).Complete);
        Assert.Empty(Directory.GetFileSystemEntries(JournalFolder));
        Assert.Empty(journal.Pending());

        // A second journal on the same folder (a broker started while another still runs, as the
        // in-process tests do beside a real broker) shares it: holding it never blocks the other.
        var second = Open(JournalFolder);
        Assert.True(second.Available);
        Assert.Empty(Directory.GetFileSystemEntries(JournalFolder));
    }

    // (3) A journal folder that is itself a junction is never followed: the journal is unavailable,
    // the start probe writes nothing in the junction's target, and every change is refused.
    [Fact]
    public void A_journal_folder_that_is_a_junction_is_refused()
    {
        var target = Path.Combine(home, "elsewhere");
        Directory.CreateDirectory(target);
        Directory.CreateDirectory(BrokerFolder);
        TestSupport.CreateJunction(JournalFolder, target);

        var journal = Open(JournalFolder);
        Assert.False(journal.Available);
        Assert.Empty(Directory.GetFileSystemEntries(target));
        Assert.Empty(journal.Pending());
        Assert.Equal("journal_unavailable", Assert.Throws<BrokerRefusal>(() => journal.Begin(Journal.NewId(), 1)).Code);
        Assert.Empty(Directory.GetFileSystemEntries(target));
    }

    // A test the hold alone cannot pass (c0b security review, minor 2: NTFS refuses to rename a
    // folder above an open one, so the swap tests above never got their junction in). The journal
    // is opened through a junction above it, and that junction is then re-pointed at a crafted
    // journal. The held folder never moved, so only opens relative to its handle stay out of the
    // crafted one.
    [Fact]
    public void A_junction_above_the_journal_repointed_after_open_is_never_used()
    {
        var real = Path.Combine(home, "real-broker");
        var realJournal = Path.Combine(real, "journal");
        Directory.CreateDirectory(realJournal);
        TestSupport.CreateJunction(BrokerFolder, real);
        var journal = Open(JournalFolder);
        Assert.True(journal.Available);
        var craftedId = Crafted(out var craftedJournal);
        var craftedFile = Path.Combine(craftedJournal, $"batch-{craftedId}.jsonl");
        // Removes the junction only, never what it points at.
        Directory.Delete(BrokerFolder);
        TestSupport.CreateJunction(BrokerFolder, Path.GetDirectoryName(craftedJournal)!);
        Assert.Equal(craftedFile, Path.Combine(craftedJournal, Path.GetFileName(Assert.Single(Directory.GetFiles(JournalFolder)))));

        Assert.Empty(journal.Pending());
        Assert.False(journal.Holds(craftedId));
        using var registry = new RootRegistry();
        var runner = new BatchRunner(registry, journal);
        Assert.Equal("unknown_batch", Assert.Throws<BrokerRefusal>(() => runner.Restore(craftedId)).Code);

        var batchId = Journal.NewId();
        using (var log = journal.Begin(batchId, 1)) { }
        Assert.Equal(batchId, Assert.Single(journal.Pending()));
        Assert.True(File.Exists(Path.Combine(realJournal, $"batch-{batchId}.jsonl")));
        Assert.True(runner.Restore(batchId).Complete);
        Assert.Empty(Directory.GetFileSystemEntries(realJournal));
        Assert.Equal(BeginOnly(craftedId), File.ReadAllBytes(craftedFile));
        Assert.Equal(craftedFile, Assert.Single(Directory.GetFiles(craftedJournal)));
    }

    // The start check that the journal lies outside every flagged folder is made on the folder the
    // journal holds, not on the path again (c0b security review, minor 1): a junction above the
    // journal that pointed into a flagged folder at Open and was re-pointed before the check would
    // otherwise pass it.
    [Fact]
    public void The_journal_outside_flagged_check_judges_the_held_folder()
    {
        var flagged = Path.Combine(home, "flagged");
        Directory.CreateDirectory(Path.Combine(flagged, "journal"));
        using var registry = new RootRegistry();
        registry.AddFlaggedRoot(flagged);
        TestSupport.CreateJunction(BrokerFolder, flagged);
        var journal = Open(JournalFolder);
        Assert.True(journal.Available);
        Directory.Delete(BrokerFolder);
        Directory.CreateDirectory(JournalFolder);

        // By path the journal is now outside; the folder it holds is inside the flagged one.
        registry.ValidateJournalOutsideFlagged(JournalFolder);
        Assert.Equal("journal_inside_flagged", Assert.Throws<BrokerRefusal>(() => registry.ValidateJournalOutsideFlagged(journal)).Code);
    }
}
