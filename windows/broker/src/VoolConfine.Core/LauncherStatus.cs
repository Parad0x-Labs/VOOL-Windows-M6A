using System.Text.Json;

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

    void Write(bool started, int? exitCode, bool? timedOut, string? limit, string? error, IReadOnlyList<string>? newRepositories)
    {
        var temporary = Path + ".writing";
        using (var stream = new FileStream(temporary, FileMode.Create, FileAccess.Write, FileShare.None))
        using (var json = new Utf8JsonWriter(stream))
        {
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
            json.WriteEndObject();
            json.Flush();
            stream.Flush(flushToDisk: true);
        }
        File.Move(temporary, Path, overwrite: true);
    }
}
