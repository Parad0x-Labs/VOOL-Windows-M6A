using System.IO;
using System.Text.Json;

namespace Vool.WindowsHost;

internal sealed record ExportRequest(string Session, string Format, string RequestId,
    bool Timestamps, bool Attachments)
{
    internal int ByteLimit => (Format == "pdf" ? 4 : 16) * 1024 * 1024;
    internal string ContentType => Format switch
    {
        "pdf" => "application/pdf",
        "md" => "text/markdown",
        _ => "text/plain",
    };
    internal string FileName => (RequestId.Length > 0 ? "vool-answer." : "vool-chat.") + Format;
    internal Uri Url => new("http://127.0.0.1:11435/api/chat/export?session="
        + Uri.EscapeDataString(Session) + "&format=" + Format
        + (RequestId.Length > 0
            ? "&request_id=" + Uri.EscapeDataString(RequestId)
            : "&timestamps=" + (Timestamps ? "1" : "0") + "&no_attachments=" + (Attachments ? "0" : "1")));
}

internal static class NativeExport
{
    internal static ExportRequest? Parse(string method, JsonElement args)
    {
        if (args.ValueKind != JsonValueKind.Array)
            return null;
        if (method == "save_answer_pdf" && args.GetArrayLength() == 2
            && Text(args[0], out var answerSession) && Text(args[1], out var requestId))
            return new ExportRequest(answerSession, "pdf", requestId, false, true);
        if (method == "save_chat_export" && args.GetArrayLength() == 4
            && Text(args[0], out var session)
            && args[1].ValueKind == JsonValueKind.String
            && args[1].GetString() is "md" or "txt" or "pdf"
            && args[2].ValueKind is JsonValueKind.True or JsonValueKind.False
            && args[3].ValueKind is JsonValueKind.True or JsonValueKind.False)
            return new ExportRequest(session, args[1].GetString()!, "", args[2].GetBoolean(), args[3].GetBoolean());
        return null;
    }

    private static bool Text(JsonElement value, out string result)
    {
        result = value.ValueKind == JsonValueKind.String ? value.GetString() ?? "" : "";
        return result.Length is >= 1 and <= 512;
    }

    internal static bool ValidBody(ExportRequest request, string? contentType, byte[] body)
    {
        return contentType == request.ContentType && body.Length <= request.ByteLimit
            && (request.Format != "pdf" || body.AsSpan().StartsWith("%PDF-"u8));
    }

    internal static string RefusalMessage(ReadOnlySpan<byte> body)
    {
        try
        {
            using var document = JsonDocument.Parse(body.ToArray());
            if (document.RootElement.ValueKind == JsonValueKind.Object
                && document.RootElement.TryGetProperty("message", out var message)
                && message.ValueKind == JsonValueKind.String
                && message.GetString() is { Length: > 0 and <= 512 } text)
                return text;
        }
        catch (JsonException) { }
        return "Export refused by VOOL.";
    }

    internal static void SaveNoClobber(byte[] body, string destination)
    {
        var fullDestination = Path.GetFullPath(destination);
        var parent = Path.GetDirectoryName(fullDestination)
            ?? throw new IOException("Export destination has no parent directory.");
        var temporary = Path.Combine(parent, ".vool-export-" + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write,
                FileShare.None, 4096, FileOptions.WriteThrough))
            {
                stream.Write(body);
                stream.Flush(flushToDisk: true);
            }
            File.Move(temporary, fullDestination, overwrite: false);
        }
        finally
        {
            if (File.Exists(temporary))
                File.Delete(temporary);
        }
    }
}
