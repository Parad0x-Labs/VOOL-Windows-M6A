using System.Text.Json;
using System.Text.Json.Nodes;

namespace VoolConfine.Core;

/// <summary>
/// Writes the status file (docs/WINDOWS_CONFINEMENT.md section 2, "Exit codes"). It sits outside every
/// write root, so the confined child can't write it; the caller trusts the exit code only when this
/// file says child_started is true. Each write replaces the whole file through a rename, so a reader
/// never sees half a record.
/// </summary>
internal sealed class LauncherStatus(string path)
{
    internal string Path { get; } = path;

    internal void NotStarted(string error) => Write(false, null, null, null, error, null);

    internal void Started() => Write(true, null, null, null, null, null);

    internal void Finished(int exitCode, bool timedOut, string? limit, IReadOnlyList<string> newRepositories) =>
        Write(true, exitCode, timedOut, limit, null, newRepositories);

    /// <summary>
    /// The scan form's rewrite: the existing record with new_repositories and repositories_scanned
    /// replaced. A missing or foreign record is not rewritten (SetupException), so the scan never
    /// invents a run record.
    /// </summary>
    internal void Scanned(IReadOnlyList<string> repositories)
    {
        JsonObject record;
        try
        {
            record = JsonNode.Parse(File.ReadAllText(Path)) as JsonObject
                ?? throw new SetupException("the status file is not a JSON object");
        }
        catch (JsonException e)
        {
            throw new SetupException($"the status file is not valid JSON: {e.Message}");
        }
        catch (FileNotFoundException)
        {
            throw new SetupException("there is no status file to rewrite");
        }
        if (record["schema"]?.GetValueKind() != JsonValueKind.String || (string?)record["schema"] != ExitCodes.StatusSchema)
            throw new SetupException("the status file has another schema");
        var list = new JsonArray();
        foreach (var repository in repositories) list.Add(repository);
        record["new_repositories"] = list;
        record["repositories_scanned"] = true;
        Replace(stream =>
        {
            using var json = new Utf8JsonWriter(stream);
            record.WriteTo(json);
        });
    }

    void Write(bool started, int? exitCode, bool? timedOut, string? limit, string? error, IReadOnlyList<string>? newRepositories)
    {
        // Set only by Finished, which runs after the post-job scan completed.
        var scanned = newRepositories is not null;
        Replace(stream =>
        {
            using var json = new Utf8JsonWriter(stream);
            json.WriteStartObject();
            json.WriteString("schema", ExitCodes.StatusSchema);
            json.WriteBoolean("child_started", started);
            if (exitCode is { } code) json.WriteNumber("exit_code", code);
            if (timedOut is { } timed) json.WriteBoolean("timed_out", timed);
            if (limit is null) json.WriteNull("limit"); else json.WriteString("limit", limit);
            if (error is null) json.WriteNull("error"); else json.WriteString("error", error);
            json.WriteStartArray("new_repositories");
            foreach (var repository in newRepositories ?? Array.Empty<string>()) json.WriteStringValue(repository);
            json.WriteEndArray();
            json.WriteBoolean("repositories_scanned", scanned);
            json.WriteEndObject();
        });
    }

    /// <summary>Writes the whole file through a rename, so a reader never sees half a record.</summary>
    void Replace(Action<Stream> write)
    {
        var temporary = Path + ".writing";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        {
            write(stream);
            stream.Flush(flushToDisk: true);
        }
        File.Move(temporary, Path, overwrite: true);
    }
}
