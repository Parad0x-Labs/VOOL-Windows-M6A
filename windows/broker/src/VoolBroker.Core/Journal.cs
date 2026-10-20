using Microsoft.Win32.SafeHandles;
using System.Text.Json;

namespace VoolBroker.Core;

/// <summary>What recovering one journalled batch did: complete, or the objects it had to leave.</summary>
public sealed record RecoveryOutcome(string BatchId, bool Committed, bool Complete, IReadOnlyList<string> Left);

/// <summary>
/// The journal for file changes (design 7.4 as it applies to section 6). Each batch has one
/// append-only file of JSON lines in the journal folder; a record is flushed to disk before the
/// step it names is made. A batch is finished, cleaned up or undone from these records alone, by
/// folder file id and exact name, never by a name pattern, and only inside a flagged folder.
/// </summary>
public sealed class Journal
{
    private const string Prefix = "batch-";
    private const string Suffix = ".jsonl";

    private Journal(string folder, bool available) { Folder = folder; Available = available; }

    public string Folder { get; }

    /// <summary>False when the journal folder cannot be created or written: every change is then refused.</summary>
    public bool Available { get; }

    /// <summary>Creates the journal folder if needed and proves a file can be written and removed in it.</summary>
    public static Journal Open(string folder)
    {
        try
        {
            Directory.CreateDirectory(folder);
            var probe = Path.Combine(folder, "probe-" + NewId() + ".tmp");
            using (var stream = new FileStream(probe, FileMode.CreateNew, FileAccess.Write, FileShare.None, 1, FileOptions.WriteThrough))
            {
                stream.WriteByte(0);
                stream.Flush(flushToDisk: true);
            }
            File.Delete(probe);
            return new Journal(folder, true);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException)
        {
            return new Journal(folder, false);
        }
    }

    /// <summary>A journal that refuses every change, for a channel started without one.</summary>
    public static Journal Unavailable(string folder) => new(folder, false);

    public static string NewId() => Convert.ToHexString(System.Security.Cryptography.RandomNumberGenerator.GetBytes(16)).ToLowerInvariant();

    public static bool IsBatchId(string value) => value.Length == 32 && value.All(c => c is >= '0' and <= '9' or >= 'a' and <= 'f');

    /// <summary>The batches the journal still holds, oldest file first.</summary>
    public IReadOnlyList<string> Pending()
    {
        if (!Available) return [];
        return new DirectoryInfo(Folder).EnumerateFiles(Prefix + "*" + Suffix)
            .OrderBy(file => file.CreationTimeUtc)
            .Select(file => file.Name[Prefix.Length..^Suffix.Length])
            .Where(IsBatchId)
            .ToArray();
    }

    public bool Holds(string batchId) => IsBatchId(batchId) && File.Exists(PathOf(batchId));

    /// <summary>Starts a batch's file. It fails, and nothing is changed, if the journal cannot be written.</summary>
    public BatchLog Begin(string batchId, int operations)
    {
        if (!Available) throw new BrokerRefusal("journal_unavailable", "The journal folder cannot be written; no file is changed.");
        FileStream? stream = null;
        try
        {
            stream = new FileStream(PathOf(batchId), FileMode.CreateNew, FileAccess.Write, FileShare.Read, 1, FileOptions.WriteThrough);
            var log = new BatchLog(stream);
            log.Append(new Dictionary<string, object?> { ["t"] = "begin", ["batch"] = batchId, ["ops"] = operations });
            return log;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or BrokerRefusal)
        {
            // Nothing has changed yet: the file this call just created (CreateNew) is removed, so a
            // failed begin never leaves a batch that blocks every later change.
            if (stream is not null)
            {
                stream.Dispose();
                try { File.Delete(PathOf(batchId)); }
                catch (Exception deleteError) when (deleteError is IOException or UnauthorizedAccessException) { }
            }
            if (error is BrokerRefusal) throw;
            throw new BrokerRefusal("journal_unavailable", error.Message);
        }
    }

    /// <summary>Recovers every batch the journal holds (at start). A batch whose file cannot be read is kept and reported.</summary>
    public IReadOnlyList<RecoveryOutcome> RecoverAll(RootRegistry roots) => Pending().Select(batchId =>
    {
        try { return Recover(batchId, roots); }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            return new RecoveryOutcome(batchId, false, false, [error is BrokerRefusal refusal ? $"journal: {refusal.Code}: {refusal.Message}" : $"journal: {error.GetType().Name}: {error.Message}"]);
        }
    }).ToArray();

    /// <summary>
    /// A committed batch has its aside objects deleted; any other batch has every step it began
    /// undone, newest first. Each object is found by its folder's file id and its exact journalled
    /// name, and changed only when its own file id matches the journal. Each step finished here is
    /// recorded as "undone" before the next, so a later attempt (restore_batch, or the next start)
    /// skips it. What cannot be finished is left in place, named in the outcome, and the batch's
    /// file is kept for a later attempt.
    /// </summary>
    public RecoveryOutcome Recover(string batchId, RootRegistry roots)
    {
        if (!Holds(batchId)) throw new BrokerRefusal("unknown_batch", "The journal holds no such batch.");
        var records = Read(batchId);
        var committed = records.Any(record => Kind(record) == "commit");
        var steps = new SortedDictionary<int, JsonElement>();
        var finished = new HashSet<int>();
        foreach (var record in records)
        {
            if (Kind(record) == "step") steps[record.GetProperty("i").GetInt32()] = record;
            else if (Kind(record) == "undone") finished.Add(record.GetProperty("i").GetInt32());
        }
        var left = new List<string>();
        var ordered = committed ? steps.Values.ToList() : steps.Values.Reverse().ToList();
        using (var log = Reopen(batchId))
        {
            foreach (var step in ordered)
            {
                var index = step.GetProperty("i").GetInt32();
                if (finished.Contains(index)) continue;
                try
                {
                    if (committed) Cleanup(step, roots);
                    else Undo(step, records, finished, roots);
                }
                catch (BrokerRefusal refusal) { left.Add($"{Describe(step)}: {refusal.Code}"); continue; }
                catch (Exception error) when (error is not OutOfMemoryException) { left.Add($"{Describe(step)}: {error.GetType().Name}: {error.Message}"); continue; }
                log.Append(new Dictionary<string, object?> { ["t"] = "undone", ["i"] = index });
                finished.Add(index);
            }
        }
        if (left.Count == 0)
        {
            File.Delete(PathOf(batchId));
            return new RecoveryOutcome(batchId, committed, true, []);
        }
        return new RecoveryOutcome(batchId, committed, false, left);
    }

    private string PathOf(string batchId) => Path.Combine(Folder, Prefix + batchId + Suffix);

    /// <summary>
    /// Every record. Each one is written whole, with its line end, in one write that is flushed to
    /// disk, so a record that does not parse or has no line end means the file was damaged: it is
    /// never guessed at. The batch is then left as it is and reported, and changes stay refused.
    /// </summary>
    private List<JsonElement> Read(string batchId)
    {
        var records = new List<JsonElement>();
        byte[] bytes;
        using (var stream = new FileStream(PathOf(batchId), FileMode.Open, FileAccess.Read, FileShare.ReadWrite))
        {
            bytes = new byte[stream.Length];
            stream.ReadExactly(bytes);
        }
        if (bytes.Length == 0 || bytes[^1] != (byte)'\n')
            throw new BrokerRefusal("journal_unreadable", $"The journal of batch {batchId} ends part way through a record.");
        var line = 0;
        for (var start = 0; start < bytes.Length; line++)
        {
            var end = Array.IndexOf(bytes, (byte)'\n', start);
            try
            {
                using var document = JsonDocument.Parse(bytes.AsMemory(start, end - start));
                if (document.RootElement.ValueKind != JsonValueKind.Object || !document.RootElement.TryGetProperty("t", out _))
                    throw new JsonException("not a journal record");
                records.Add(document.RootElement.Clone());
            }
            catch (JsonException) { throw new BrokerRefusal("journal_unreadable", $"Record {line + 1} of batch {batchId} is unreadable."); }
            start = end + 1;
        }
        Validate(records, batchId);
        return records;
    }

    /// <summary>
    /// The whole journal is checked before recovery acts on any of it: begin first, with this
    /// batch's id; steps numbered 0, 1, 2 in order, each of a known kind with exactly its fields
    /// and their types; temp and aside names of this batch's own form; done and undone only for a
    /// step already recorded, each at most once; nothing but undone after the commit. A record
    /// that parses but does not fit is damage like any other: never guessed at.
    /// </summary>
    private static void Validate(List<JsonElement> records, string batchId)
    {
        var steps = 0;
        var committed = false;
        var done = new HashSet<int>();
        var undone = new HashSet<int>();
        for (var line = 0; line < records.Count; line++)
        {
            var record = records[line];
            void Bad(string why) => throw new BrokerRefusal("journal_unreadable", $"Record {line + 1} of batch {batchId} is not a journal record ({why}).");
            void Fields(params string[] names)
            {
                foreach (var property in record.EnumerateObject())
                    if (!names.Contains(property.Name, StringComparer.Ordinal)) Bad($"unexpected field '{property.Name}'");
                foreach (var name in names)
                    if (!record.TryGetProperty(name, out _) && !(name == "object" && Text(record, "t") == "done")) Bad($"missing field '{name}'");
            }
            int Index()
            {
                if (!Int(record, "i", out var index) || index < 0 || index >= steps) Bad("step index");
                return index;
            }
            void Hex(string name)
            {
                var value = Text(record, name);
                if (value is null || value.Length != 32 || !value.All(char.IsAsciiHexDigitLower)) Bad($"field '{name}'");
            }
            void Name(string name, string? prefix = null)
            {
                var value = Text(record, name);
                if (value is null || value.Length is 0 or > 255 || value is "." or ".." || value.IndexOfAny(['\\', '/', ':', '\0']) >= 0) Bad($"field '{name}'");
                if (prefix is not null && !IsOwnName(value!, prefix)) Bad($"field '{name}' is not a name this batch uses");
            }
            var kind = Text(record, "t");
            if ((line == 0) != (kind == "begin")) Bad("begin must come first, once");
            switch (kind)
            {
                case "begin":
                    Fields("t", "batch", "ops");
                    if (Text(record, "batch") != batchId) Bad("batch id");
                    if (!Int(record, "ops", out var operations) || operations is < 1 or > BatchRunner.MaxOperations) Bad("ops");
                    break;
                case "step":
                {
                    if (committed) Bad("a step after the commit");
                    if (!Int(record, "i", out var index) || index != steps) Bad("step index");
                    if (!record.TryGetProperty("vol", out var vol) || vol.ValueKind != JsonValueKind.Number || !vol.TryGetUInt64(out _)) Bad("field 'vol'");
                    switch (Text(record, "k"))
                    {
                        case "temp":
                            Fields("t", "i", "k", "vol", "parent", "name");
                            Hex("parent");
                            Name("name", TempPrefix(batchId));
                            break;
                        case "aside":
                            Fields("t", "i", "k", "vol", "parent", "name", "aside", "object");
                            Hex("parent"); Hex("object");
                            Name("name");
                            Name("aside", AsidePrefix(batchId));
                            break;
                        case "place":
                            Fields("t", "i", "k", "vol", "from_parent", "from_name", "parent", "name", "object");
                            Hex("from_parent"); Hex("parent"); Hex("object");
                            Name("from_name"); Name("name");
                            break;
                        default:
                            Bad("step kind");
                            break;
                    }
                    steps++;
                    break;
                }
                case "done":
                    if (committed) Bad("done after the commit");
                    Fields("t", "i", "object");
                    if (!done.Add(Index())) Bad("a step done twice");
                    if (record.TryGetProperty("object", out _)) Hex("object");
                    break;
                case "undone":
                    Fields("t", "i");
                    if (!undone.Add(Index())) Bad("a step undone twice");
                    break;
                case "commit":
                    if (committed) Bad("a second commit");
                    Fields("t");
                    committed = true;
                    break;
                default:
                    Bad("record kind");
                    break;
            }
        }
    }

    private static bool Int(JsonElement record, string name, out int value)
    {
        value = 0;
        return record.TryGetProperty(name, out var element) && element.ValueKind == JsonValueKind.Number && element.TryGetInt32(out value);
    }

    private static string? Text(JsonElement record, string name) =>
        record.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String ? value.GetString() : null;

    /// <summary>The temp and aside names a batch makes (BatchRunner.Steps): its prefix, then a number.</summary>
    internal static string TempPrefix(string batchId) => $".vool-broker-tmp-{batchId[..12]}-";
    internal static string AsidePrefix(string batchId) => $".vool-broker-aside-{batchId[..12]}-";

    private static bool IsOwnName(string name, string prefix) =>
        name.StartsWith(prefix, StringComparison.Ordinal) && name.Length > prefix.Length && name.Length <= prefix.Length + 10 &&
        name[prefix.Length..].All(char.IsAsciiDigit);

    /// <summary>Opens a batch's file to add the records recovery writes.</summary>
    private BatchLog Reopen(string batchId)
    {
        try { return new BatchLog(new FileStream(PathOf(batchId), FileMode.Append, FileAccess.Write, FileShare.Read, 1, FileOptions.WriteThrough)); }
        catch (IOException error) { throw new BrokerRefusal("journal_unavailable", error.Message); }
        catch (UnauthorizedAccessException error) { throw new BrokerRefusal("journal_unavailable", error.Message); }
    }

    private static string Kind(JsonElement record) => record.GetProperty("t").GetString() ?? "";

    private static string Describe(JsonElement step) => $"{step.GetProperty("k").GetString()} {step.GetProperty("name").GetString()}" +
        (step.TryGetProperty("aside", out var aside) ? $" (aside {aside.GetString()})" : "");

    private static byte[] Id(JsonElement step, string name) => Convert.FromHexString(step.GetProperty(name).GetString()!);

    private static (SafeFileHandle Handle, RootEntry Flagged) OpenFolder(JsonElement step, string name, RootRegistry roots) =>
        roots.OpenFlaggedFolderById(step.GetProperty("vol").GetUInt64(), Id(step, name));

    /// <summary>Opens name in folder for change, or returns null when nothing has that name.</summary>
    private static SafeFileHandle? TryOpen(SafeFileHandle folder, string name)
    {
        try { return Native.OpenRelative(folder, name, Native.Delete, 0, Native.ShareRead, Native.FileOpen); }
        catch (BrokerRefusal refusal) when (refusal.Code == "not_found") { return null; }
    }

    private static bool Is(SafeFileHandle handle, byte[] fileId) => Native.GetFileIdentity(handle).FileId.AsSpan().SequenceEqual(fileId);

    /// <summary>The object a step made or moved, when the journal knows its file id.</summary>
    private static byte[]? ObjectOf(JsonElement step, IReadOnlyList<JsonElement> records)
    {
        if (step.TryGetProperty("object", out var known)) return Convert.FromHexString(known.GetString()!);
        var index = step.GetProperty("i").GetInt32();
        foreach (var record in records)
            if (Kind(record) == "done" && record.GetProperty("i").GetInt32() == index && record.TryGetProperty("object", out var made))
                return Convert.FromHexString(made.GetString()!);
        return null;
    }

    private static void Undo(JsonElement step, IReadOnlyList<JsonElement> records, IReadOnlySet<int> finished, RootRegistry roots)
    {
        var kind = step.GetProperty("k").GetString();
        var name = step.GetProperty("name").GetString()!;
        var (folder, _) = OpenFolder(step, "parent", roots);
        using (folder)
        {
            switch (kind)
            {
                case "temp":
                {
                    // The name has this batch's own temp form (Validate). When the journal knows the
                    // temp's id, the object must match it. When it does not (the create failed, or a
                    // crash came before the done record), only what the create itself leaves is
                    // removed: an empty file with one link, or an empty folder.
                    using var temp = TryOpen(folder, name);
                    var made = ObjectOf(step, records);
                    if (temp is null)
                    {
                        // The temp was moved on by a place step that is not undone yet: it is not gone.
                        if (made is not null && PlacedAndNotUndone(made, records, finished))
                            throw new BrokerRefusal("changed_since_check", "The temp object is still in the place a later step moved it to.");
                        return;
                    }
                    var isDirectory = (Native.GetAttributeTag(temp).FileAttributes & Native.FileAttributeDirectory) != 0;
                    if (made is not null)
                    {
                        if (!Is(temp, made)) throw new BrokerRefusal("changed_since_check", "Another object holds the temp name.");
                    }
                    else if (!IsAsCreated(temp, isDirectory))
                        throw new BrokerRefusal("changed_since_check", "The object under the temp name is not the empty one the batch made.");
                    if (isDirectory && !Native.IsDirectoryEmpty(temp)) throw new BrokerRefusal("not_empty", "A temp folder is not empty.");
                    Native.DeleteNow(temp, name);
                    return;
                }
                case "aside":
                {
                    var target = Id(step, "object");
                    var asideName = step.GetProperty("aside").GetString()!;
                    // Already in its place (the aside rename never happened, or was undone): done.
                    using (var original = TryOpen(folder, name))
                    {
                        if (original is not null && Is(original, target)) return;
                    }
                    using var aside = TryOpen(folder, asideName);
                    if (aside is null) throw new BrokerRefusal("not_found", "The object set aside is neither aside nor in its place.");
                    if (!Is(aside, target)) throw new BrokerRefusal("changed_since_check", "Another object holds the aside name.");
                    Native.RenameInto(aside, folder, name);
                    return;
                }
                case "place":
                {
                    var target = Id(step, "object");
                    var (from, _) = OpenFolder(step, "from_parent", roots);
                    using (from)
                    {
                        var fromName = step.GetProperty("from_name").GetString()!;
                        using (var placed = TryOpen(folder, name))
                        {
                            if (placed is not null && Is(placed, target))
                            {
                                Native.RenameInto(placed, from, fromName);
                                return;
                            }
                        }
                        using var unplaced = TryOpen(from, fromName);
                        if (unplaced is not null && Is(unplaced, target)) return;
                        throw new BrokerRefusal("not_found", "The moved object is neither in its new place nor its old one.");
                    }
                }
                default:
                    throw new BrokerRefusal("journal_unavailable", $"Unknown journal step '{kind}'.");
            }
        }
    }

    /// <summary>What a create leaves before anything is written: an empty folder, or an empty file with one link.</summary>
    private static bool IsAsCreated(SafeFileHandle handle, bool isDirectory)
    {
        if (isDirectory) return Native.IsDirectoryEmpty(handle);
        var info = Native.GetStandardInfo(handle);
        return info.EndOfFile == 0 && info.NumberOfLinks == 1;
    }

    /// <summary>True when a place step not yet undone moved the object with this id.</summary>
    private static bool PlacedAndNotUndone(byte[] objectId, IReadOnlyList<JsonElement> records, IReadOnlySet<int> finished)
    {
        var id = Convert.ToHexString(objectId).ToLowerInvariant();
        return records.Any(record => Kind(record) == "step" && record.GetProperty("k").GetString() == "place" &&
            record.GetProperty("object").GetString() == id && !finished.Contains(record.GetProperty("i").GetInt32()));
    }

    /// <summary>After a commit only aside objects remain to remove; every other step stays as made.</summary>
    private static void Cleanup(JsonElement step, RootRegistry roots)
    {
        if (step.GetProperty("k").GetString() != "aside") return;
        var (folder, _) = OpenFolder(step, "parent", roots);
        using (folder)
        {
            var asideName = step.GetProperty("aside").GetString()!;
            using var aside = TryOpen(folder, asideName);
            if (aside is null) return;
            if (!Is(aside, Id(step, "object"))) throw new BrokerRefusal("changed_since_check", "Another object holds the aside name.");
            if ((Native.GetAttributeTag(aside).FileAttributes & Native.FileAttributeDirectory) != 0 && !Native.IsDirectoryEmpty(aside))
                throw new BrokerRefusal("not_empty", "A folder set aside is no longer empty.");
            Native.DeleteNow(aside, asideName);
        }
    }
}

/// <summary>One batch's journal file. Each record is written and flushed to disk before Append returns.</summary>
public sealed class BatchLog : IDisposable
{
    private readonly FileStream stream;

    internal BatchLog(FileStream stream) { this.stream = stream; }

    public void Append(Dictionary<string, object?> record)
    {
        try
        {
            // One write of the whole record and its line end, then a flush to disk.
            var bytes = JsonSerializer.SerializeToUtf8Bytes(record);
            var line = new byte[bytes.Length + 1];
            bytes.CopyTo(line, 0);
            line[^1] = (byte)'\n';
            stream.Write(line);
            stream.Flush(flushToDisk: true);
        }
        catch (IOException error) { throw new BrokerRefusal("journal_unavailable", error.Message); }
    }

    public void Dispose() => stream.Dispose();
}
