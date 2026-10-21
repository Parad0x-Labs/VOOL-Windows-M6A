using Microsoft.Win32.SafeHandles;
using System.Security.Cryptography;
using System.Text.Json;

namespace VoolBroker.Core;

/// <summary>A batch that made changes and then stopped: which operations ran, and whether the broker's own restore finished.</summary>
public sealed class BatchStopped(string code, string message, string batchId, int ran, bool restored, IReadOnlyList<string> left) : Exception(message)
{
    public string Code { get; } = code;
    public string BatchId { get; } = batchId;
    public int Ran { get; } = ran;
    public bool Restored { get; } = restored;
    public IReadOnlyList<string> Left { get; } = left;
}

/// <summary>A finished batch: one result per operation, and any aside object its cleanup had to leave.</summary>
public sealed record BatchResult(string BatchId, IReadOnlyList<Dictionary<string, object?>> Results, IReadOnlyList<string> CleanupLeft);

/// <summary>Test hook: thrown from a step hook to stop a batch as a crash would, with no restore.</summary>
internal sealed class BatchCrashForTests : Exception;

/// <summary>
/// Runs batches of file changes (design section 6, apply_batch and restore_batch). Every write,
/// delete, rename and new folder, alone or in a batch, runs here.
/// 1. Plan: every path is walked and every rule and expect checked, holding each object, before
///    anything changes. A refusal here changes nothing.
/// 2. Execute: each step is journalled and flushed before it is made. New objects are made under
///    a temp name and renamed into place; an object that is replaced or deleted is renamed aside,
///    never overwritten, so the batch can always be put back.
/// 3. An error part way: every handle is released and the journal undoes the steps begun, newest
///    first, by file id. A finished batch commits, then its aside objects are deleted.
/// </summary>
public sealed class BatchRunner(RootRegistry roots, Journal journal)
{
    public const int MaxOperations = 1024;

    // Changes run one at a time (design section 5), and restore_batch with them: a batch never
    // starts while another is running, recovering or being restored, so the pending check below
    // sees every batch the journal holds.
    private readonly object changing = new();

    // The test hooks are per async flow, so a test's hook never runs in another test's batch.
    private static readonly AsyncLocal<Action<int, string>?> beforeStep = new();
    private static readonly AsyncLocal<Action?> afterPlan = new();
    private static readonly AsyncLocal<Action?> afterCommitRecord = new();

    /// <summary>Test hook: runs before each step (its index and kind), after its journal record is flushed.</summary>
    internal static Action<int, string>? BeforeStepForTests { get => beforeStep.Value; set => beforeStep.Value = value; }
    /// <summary>Test hook: runs after the plan holds everything, before the journal is begun.</summary>
    internal static Action? AfterPlanForTests { get => afterPlan.Value; set => afterPlan.Value = value; }
    /// <summary>Test hook: runs right after the commit record is written, where a failing flush would throw.</summary>
    internal static Action? AfterCommitRecordForTests { get => afterCommitRecord.Value; set => afterCommitRecord.Value = value; }
    /// <summary>
    /// Test hook for the broker process (set only from VOOL_BROKER_TEST_PAUSE_AT, see Program): runs
    /// at each journal boundary with its name and step index (-1 for the commit), so a test can stop
    /// the broker there and kill it. Boundaries: before_record, before_step, after_step (each step);
    /// before_commit, after_commit.
    /// </summary>
    internal static Action<string, int>? BoundaryForTests;

    /// <summary>
    /// Tests only: VOOL_BROKER_TEST_PAUSE_AT="boundary:index" makes the broker write
    /// {"paused":"boundary:index"} on stdout when a batch reaches that boundary, and stop there
    /// until it is killed, so a test can kill a real broker at an exact point. Stopping a broker
    /// part way is what a kill does anyway; the journal is what makes either safe.
    /// </summary>
    public static void PauseAtFromEnvironment()
    {
        var text = Environment.GetEnvironmentVariable("VOOL_BROKER_TEST_PAUSE_AT");
        if (string.IsNullOrEmpty(text)) return;
        BoundaryForTests = (boundary, index) =>
        {
            if (text != string.Create(System.Globalization.CultureInfo.InvariantCulture, $"{boundary}:{index}")) return;
            Console.Out.WriteLine(JsonSerializer.Serialize(new { paused = text }));
            Console.Out.Flush();
            Thread.Sleep(Timeout.Infinite);
        };
    }

    public Journal Journal { get; } = journal;

    public BatchResult Apply(IReadOnlyList<FileChange> changes)
    {
        if (changes.Count is 0 or > MaxOperations) throw new BrokerRefusal("invalid_request", $"A batch holds 1 to {MaxOperations} operations.");
        if (!Journal.Available) throw new BrokerRefusal("journal_unavailable", "The journal folder cannot be written; no file is changed.");
        lock (changing) return ApplyAlone(changes);
    }

    private BatchResult ApplyAlone(IReadOnlyList<FileChange> changes)
    {
        // A batch the journal still holds (not finished, not fully restored, or unreadable) leaves
        // objects the broker may still have to put back: no new change starts until it is cleared.
        var unfinished = Journal.Pending();
        if (unfinished.Count > 0)
            throw new BrokerRefusal("journal_pending", $"Batch {unfinished[0]} is not finished; restore_batch it first. No file is changed.");
        using var plan = new Plan();
        var planned = changes.Select(plan.Add).ToArray();
        AfterPlanForTests?.Invoke();
        var batchId = Journal.NewId();
        var results = new List<Dictionary<string, object?>>();
        var ran = 0;
        var log = Journal.Begin(batchId, changes.Count);
        var steps = new Steps(log, batchId);
        try
        {
            foreach (var change in planned)
            {
                results.Add(change.Execute(steps));
                ran++;
            }
            BoundaryForTests?.Invoke("before_commit", -1);
            log.Append(new Dictionary<string, object?> { ["t"] = "commit" });
            AfterCommitRecordForTests?.Invoke();
            BoundaryForTests?.Invoke("after_commit", -1);
        }
        catch (BatchCrashForTests)
        {
            log.Dispose();
            plan.Dispose();
            throw;
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            log.Dispose();
            plan.Dispose();
            var (code, message) = error is BrokerRefusal refusal ? (refusal.Code, refusal.Message)
                : error is IOException ? ("io_error", error.Message) : ("authority_check_failed", error.GetType().Name);
            RecoveryOutcome outcome;
            try { outcome = Journal.Recover(batchId, roots); }
            catch (Exception recoverError) when (recoverError is not OutOfMemoryException)
            {
                outcome = new RecoveryOutcome(batchId, false, false, [$"journal: {recoverError.GetType().Name}: {recoverError.Message}"]);
            }
            // The commit record reached the journal before the error (its flush failed, say):
            // the batch is committed and recovery finished it, so it is reported as done, with
            // anything its cleanup left, never as restored.
            if (outcome.Committed) return new BatchResult(batchId, results, outcome.Left);
            throw new BatchStopped(code, message, batchId, ran, outcome.Complete, outcome.Left);
        }
        // Committed: the objects set aside are deleted through the handles the plan still holds,
        // never looked up again by name. Any it cannot delete, the journal cleans up below.
        steps.RemoveAsides();
        log.Dispose();
        plan.Dispose();
        IReadOnlyList<string> left;
        try { left = Journal.Recover(batchId, roots).Left; }
        catch (Exception error) when (error is not OutOfMemoryException) { left = [$"journal: {error.GetType().Name}: {error.Message}"]; }
        return new BatchResult(batchId, results, left);
    }

    /// <summary>restore_batch: finishes (committed) or undoes (not committed) a batch the journal still holds.</summary>
    public RecoveryOutcome Restore(string batchId)
    {
        if (!Journal.IsBatchId(batchId)) throw new BrokerRefusal("invalid_request", "batch_id must be 32 lowercase hex digits.");
        if (!Journal.Available) throw new BrokerRefusal("journal_unavailable", "The journal folder cannot be read.");
        lock (changing) return Journal.Recover(batchId, roots);
    }

    /// <summary>Journals and makes the steps of one batch, numbering them in order.</summary>
    private sealed class Steps(BatchLog log, string batchId)
    {
        private int next;
        private int names;
        private readonly List<(int Index, Node Node, string Name)> asides = [];

        public string TempName() => $".vool-broker-tmp-{batchId[..12]}-{names++}";
        public string AsideName() => $".vool-broker-aside-{batchId[..12]}-{names++}";

        /// <summary>Makes a new empty folder, or a file holding data, under a name only this batch uses.</summary>
        public SafeFileHandle Temp(Node parent, string name, bool directory, byte[]? data, Plan plan)
        {
            var index = Begin("temp", new() { ["vol"] = parent.Volume, ["parent"] = parent.Id(), ["name"] = name });
            var access = Native.Delete | (directory ? 0 : Native.FileWriteData | Native.FileWriteAttributes);
            SafeFileHandle handle;
            try
            {
                handle = Native.OpenRelative(parent.Handle!, name, access,
                    directory ? Native.FileDirectoryFile : Native.FileNonDirectoryFile,
                    directory ? Native.ShareRead | Native.ShareWrite : Native.ShareRead, Native.FileCreate);
            }
            catch (BrokerRefusal)
            {
                // The create made nothing (NtCreateFile creates or fails as a whole), so the step is
                // recorded as never made: an object someone else has under that name is not the
                // batch's to remove.
                log.Append(new() { ["t"] = "undone", ["i"] = index });
                throw;
            }
            plan.Own(handle);
            Done(index, FileOps.FileIdOf(handle));
            if (data is not null)
            {
                RandomAccess.Write(handle, data, 0);
                Native.Flush(handle);
            }
            return handle;
        }

        /// <summary>Renames node out of the way to an aside name in its folder; the journal can put it back.</summary>
        public void Aside(Node node, string asideName)
        {
            var parent = node.Parent!;
            var index = Begin("aside", new()
            {
                ["vol"] = node.Volume, ["parent"] = parent.Id(), ["name"] = node.ActualName(),
                ["aside"] = asideName, ["object"] = node.Id()
            });
            Native.RenameInto(node.Handle!, parent.Handle!, asideName);
            Done(index);
            asides.Add((index, node, asideName));
        }

        /// <summary>Renames the object handle holds from fromParent\fromName to toParent\toName, never replacing anything.</summary>
        public void Place(SafeFileHandle handle, Node fromParent, string fromName, Node toParent, string toName)
        {
            var index = Begin("place", new()
            {
                ["vol"] = toParent.Volume, ["from_parent"] = fromParent.Id(), ["from_name"] = fromName,
                ["parent"] = toParent.Id(), ["name"] = toName, ["object"] = FileOps.FileIdOf(handle)
            });
            Native.RenameInto(handle, toParent.Handle!, toName);
            Done(index);
        }

        /// <summary>
        /// After the commit: deletes each object set aside through its held handle, recording each
        /// one as done. A failure is left for the journal's cleanup, which reports what it cannot do;
        /// a folder someone filled since its check is put back under its name there, never left hidden.
        /// </summary>
        public void RemoveAsides()
        {
            foreach (var (index, node, name) in asides)
            {
                try
                {
                    if (node.IsDirectory && !Native.IsDirectoryEmpty(node.Handle!)) continue;
                    Native.DeleteNow(node.Handle!, name);
                    log.Append(new() { ["t"] = "undone", ["i"] = index });
                }
                catch (Exception error) when (error is BrokerRefusal or IOException or UnauthorizedAccessException) { }
            }
        }

        private int Begin(string kind, Dictionary<string, object?> fields)
        {
            var index = next++;
            fields["t"] = "step";
            fields["i"] = index;
            fields["k"] = kind;
            BoundaryForTests?.Invoke("before_record", index);
            log.Append(fields);
            BoundaryForTests?.Invoke("before_step", index);
            BeforeStepForTests?.Invoke(index, kind);
            return index;
        }

        private void Done(int index, string? objectId = null)
        {
            BoundaryForTests?.Invoke("after_step", index);
            var record = new Dictionary<string, object?> { ["t"] = "done", ["i"] = index };
            if (objectId is not null) record["object"] = objectId;
            log.Append(record);
        }
    }

    /// <summary>
    /// One object in the plan. An existing one is held by Handle; a missing one may be a folder an
    /// earlier ensure_directory in the batch creates (Planned). Each node is either a folder other
    /// operations pass through, or the object of exactly one operation, never both.
    /// </summary>
    private sealed class Node(Tree tree, Node? parent, string name)
    {
        public Tree Tree { get; } = tree;
        public Node? Parent { get; } = parent;
        public string Name { get; } = name;
        public SafeFileHandle? Handle { get; set; }
        public bool Exists { get; set; }
        public bool IsDirectory { get; set; }
        public bool Planned { get; set; }
        public bool IsObject { get; set; }
        public Dictionary<string, Node> Children { get; } = new(StringComparer.OrdinalIgnoreCase);
        public ulong Volume => Tree.Root.Flagged.VolumeSerial;
        public string Rel => Parent is null ? "" : Parent.Rel.Length == 0 ? Name : Parent.Rel + "\\" + Name;
        public string Id() => FileOps.FileIdOf(Handle!);

        /// <summary>The name the object has on disk (its case may differ from the request's).</summary>
        public string ActualName()
        {
            var full = Native.GetObjectName(Handle!);
            return full[(full.LastIndexOf('\\') + 1)..];
        }
    }

    private sealed class Tree(OpenedRoot root)
    {
        public OpenedRoot Root { get; } = root;
        public string RootPath { get; } = HandleWalk.RootPath(root.Flagged);
        public Node Top { get; set; } = null!;
    }

    private sealed record PlannedChange(Func<Steps, Dictionary<string, object?>> Run)
    {
        public Dictionary<string, object?> Execute(Steps steps) => Run(steps);
    }

    /// <summary>The plan: every node checked, and every handle it holds until the batch ends.</summary>
    private sealed class Plan : IDisposable
    {
        private readonly List<SafeFileHandle> handles = [];
        private readonly Dictionary<SafeFileHandle, Tree> trees = new();

        public void Own(SafeFileHandle handle) => handles.Add(handle);

        public void Dispose()
        {
            foreach (var handle in handles) handle.Dispose();
            handles.Clear();
        }

        private Tree TreeOf(OpenedRoot root)
        {
            if (!trees.TryGetValue(root.Handle, out var tree))
            {
                tree = new Tree(root);
                tree.Top = new Node(tree, null, "") { Handle = root.Handle, Exists = true, IsDirectory = true };
                trees.Add(root.Handle, tree);
            }
            return tree;
        }

        public PlannedChange Add(FileChange change) => change.Op switch
        {
            "ensure_directory" => EnsureDirectory(change),
            "write_file" => WriteFile(change),
            "delete" => Delete(change),
            "rename" => Rename(change),
            _ => throw new BrokerRefusal("invalid_request", $"Unknown batch operation '{change.Op}'.")
        };

        /// <summary>Looks up or opens name under parent: as a folder to pass through, or as the object of an operation.</summary>
        private Node Child(Node parent, string name, bool asObject)
        {
            if (parent.IsObject) throw new BrokerRefusal("invalid_request", $"'{parent.Rel}' is changed by another operation in this batch.");
            if (parent.Children.TryGetValue(name, out var known))
            {
                if (asObject || known.IsObject) throw new BrokerRefusal("invalid_request", $"'{known.Rel}' is named by two operations in this batch.");
                return known;
            }
            var node = new Node(parent.Tree, parent, name) { IsObject = asObject };
            if (parent.Exists)
            {
                try
                {
                    // An object is held with DELETE access and read sharing only, so no one else can
                    // write, rename or delete it until the batch ends; a folder passed through is held
                    // as the walk holds it, so it cannot be renamed or deleted.
                    var handle = asObject
                        ? Native.OpenRelative(parent.Handle!, name, Native.Delete, 0, Native.ShareRead, Native.FileOpen)
                        : Native.OpenRelative(parent.Handle!, name, 0, Native.FileDirectoryFile);
                    Own(handle);
                    HandleWalk.CheckComponent(parent.Tree.Root.Flagged, parent.Tree.RootPath, handle, name, last: true);
                    node.Handle = handle;
                    node.Exists = true;
                    node.IsDirectory = (Native.GetAttributeTag(handle).FileAttributes & Native.FileAttributeDirectory) != 0;
                }
                catch (BrokerRefusal refusal) when (refusal.Code == "not_found") { }
            }
            parent.Children.Add(name, node);
            return node;
        }

        /// <summary>The folder the last component of parts goes in: it must exist, or be planned by an earlier ensure_directory.</summary>
        private Node ParentOf(Tree tree, string[] parts)
        {
            var node = tree.Top;
            foreach (var part in parts[..^1])
            {
                node = Child(node, part, asObject: false);
                if (!node.Exists && !node.Planned) throw new BrokerRefusal("not_found", $"Folder '{node.Rel}' does not exist.");
            }
            if (node.IsObject) throw new BrokerRefusal("invalid_request", $"'{node.Rel}' is changed by another operation in this batch.");
            return node;
        }

        private PlannedChange EnsureDirectory(FileChange change)
        {
            var parts = HandleWalk.ValidateRelativePath(change.Path);
            var node = TreeOf(change.Root).Top;
            var created = new List<Node>();
            foreach (var part in parts)
            {
                node = Child(node, part, asObject: false);
                if (!node.Exists && !node.Planned)
                {
                    node.Planned = true;
                    created.Add(node);
                }
            }
            return new PlannedChange(steps =>
            {
                foreach (var folder in created)
                {
                    var temp = steps.TempName();
                    var handle = steps.Temp(folder.Parent!, temp, directory: true, null, this);
                    steps.Place(handle, folder.Parent!, temp, folder.Parent!, folder.Name);
                    folder.Handle = handle;
                    folder.Exists = true;
                    folder.IsDirectory = true;
                }
                return new() { ["op"] = "ensure_directory", ["created"] = created.Select(folder => folder.Rel).ToArray() };
            });
        }

        private PlannedChange WriteFile(FileChange change)
        {
            var parts = HandleWalk.ValidateRelativePath(change.Path);
            var data = change.Data ?? throw new BrokerRefusal("invalid_request", "write_file needs data.");
            var expect = change.Expect ?? throw new BrokerRefusal("invalid_request", "write_file needs expect.");
            if (expect is not ("any" or "absent") && !(expect.StartsWith("sha256:", StringComparison.Ordinal) && expect.Length == 71 && expect[7..].All(char.IsAsciiHexDigitLower)))
                throw new BrokerRefusal("invalid_request", "expect must be any, absent or sha256:<64 lowercase hex digits>.");
            var parent = ParentOf(TreeOf(change.Root), parts);
            var node = Child(parent, parts[^1], asObject: true);
            if (node.Exists)
            {
                if (node.IsDirectory) throw new BrokerRefusal("is_a_directory", $"'{node.Rel}' is a folder.");
                if ((Native.GetAttributeTag(node.Handle!).FileAttributes & Native.FileAttributeReadOnly) != 0) throw new BrokerRefusal("read_only", $"'{node.Rel}' is read-only.");
                if (expect == "absent") throw new BrokerRefusal("changed_since_check", $"'{node.Rel}' exists.");
                if (expect != "any" && FileOps.HashOf(node.Handle!) != expect[7..]) throw new BrokerRefusal("changed_since_check", $"'{node.Rel}' does not have the expected hash.");
            }
            else if (expect != "any" && expect != "absent") throw new BrokerRefusal("changed_since_check", $"'{node.Rel}' does not exist.");
            var sha = Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
            return new PlannedChange(steps =>
            {
                var temp = steps.TempName();
                var handle = steps.Temp(parent, temp, directory: false, data, this);
                if (node.Exists) steps.Aside(node, steps.AsideName());
                try { steps.Place(handle, parent, temp, parent, parts[^1]); }
                catch (BrokerRefusal refusal) when (refusal.Code == "destination_exists")
                {
                    throw new BrokerRefusal("changed_since_check", $"Another program created '{node.Rel}' during the write.");
                }
                return new() { ["op"] = "write_file", ["path"] = node.Rel, ["file_id"] = FileOps.FileIdOf(handle), ["sha256"] = sha };
            });
        }

        private PlannedChange Delete(FileChange change)
        {
            var parts = HandleWalk.ValidateRelativePath(change.Path);
            var node = Child(ParentOf(TreeOf(change.Root), parts), parts[^1], asObject: true);
            if (!node.Exists) throw new BrokerRefusal("not_found", $"'{node.Rel}' does not exist.");
            if (node.IsDirectory)
            {
                if (!Native.IsDirectoryEmpty(node.Handle!)) throw new BrokerRefusal("not_empty", $"Folder '{node.Rel}' is not empty.");
            }
            else if ((Native.GetAttributeTag(node.Handle!).FileAttributes & Native.FileAttributeReadOnly) != 0) throw new BrokerRefusal("read_only", $"'{node.Rel}' is read-only.");
            return new PlannedChange(steps =>
            {
                steps.Aside(node, steps.AsideName());
                // Holding a folder does not stop another program creating something in it. Checked
                // again once it is aside: an entry made since the plan stops the batch, which puts
                // the folder back under its name, entry and all.
                if (node.IsDirectory && !Native.IsDirectoryEmpty(node.Handle!))
                    throw new BrokerRefusal("not_empty", $"Folder '{node.Rel}' is no longer empty.");
                return new() { ["op"] = "delete", ["path"] = node.Rel };
            });
        }

        private PlannedChange Rename(FileChange change)
        {
            var fromParts = HandleWalk.ValidateRelativePath(change.Path);
            var toParts = HandleWalk.ValidateRelativePath(change.ToPath ?? throw new BrokerRefusal("invalid_request", "rename needs to."));
            var toRoot = change.ToRoot ?? change.Root;
            if (toRoot.Flagged.VolumeSerial != change.Root.Flagged.VolumeSerial) throw new BrokerRefusal("cross_volume", "A rename cannot move between volumes.");
            var fromParent = ParentOf(TreeOf(change.Root), fromParts);
            var from = Child(fromParent, fromParts[^1], asObject: true);
            if (!from.Exists) throw new BrokerRefusal("not_found", $"'{from.Rel}' does not exist.");
            var toParent = ParentOf(TreeOf(toRoot), toParts);
            var to = Child(toParent, toParts[^1], asObject: true);
            if (to.Exists)
            {
                if (!change.Replace) throw new BrokerRefusal("destination_exists", $"'{to.Rel}' exists.");
                if (to.IsDirectory) throw new BrokerRefusal("is_a_directory", $"'{to.Rel}' is a folder.");
                if ((Native.GetAttributeTag(to.Handle!).FileAttributes & Native.FileAttributeReadOnly) != 0) throw new BrokerRefusal("read_only", $"'{to.Rel}' is read-only.");
            }
            return new PlannedChange(steps =>
            {
                var fromName = from.ActualName();
                if (to.Exists) steps.Aside(to, steps.AsideName());
                steps.Place(from.Handle!, fromParent, fromName, toParent, toParts[^1]);
                return new() { ["op"] = "rename", ["path"] = to.Rel, ["file_id"] = from.Id() };
            });
        }
    }
}
