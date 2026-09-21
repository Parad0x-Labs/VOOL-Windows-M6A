using System.Text.Json;

namespace Vool.WindowsHost;

internal sealed record BridgeRequest(string Id, string Method, JsonElement Arguments);

internal static class NativeBridge
{
    internal const string Channel = "vool-native-v1";
    internal const int MaxMessageBytes = 16 * 1024;
    private static readonly Uri ApiOrigin = new("http://127.0.0.1:11435/");

    internal static bool TryParse(string source, string current, string expectedPath, string json,
        out BridgeRequest? request)
    {
        request = null;
        if (!TrustedPage(source, expectedPath) || !TrustedPage(current, expectedPath)
            || System.Text.Encoding.UTF8.GetByteCount(json) > MaxMessageBytes)
            return false;
        try
        {
            using var document = JsonDocument.Parse(json);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || HasDuplicateProperties(root))
                return false;
            if (!root.TryGetProperty("channel", out var channel) || channel.ValueKind != JsonValueKind.String
                || channel.GetString() != Channel
                || !root.TryGetProperty("id", out var id) || id.ValueKind != JsonValueKind.String
                || !root.TryGetProperty("method", out var method) || method.ValueKind != JsonValueKind.String
                || !root.TryGetProperty("args", out var args) || args.ValueKind != JsonValueKind.Array)
                return false;
            var idText = id.GetString() ?? "";
            var methodText = method.GetString() ?? "";
            if (idText.Length is < 1 or > 64 || !idText.All(char.IsAsciiLetterOrDigit)
                || methodText is not ("open_settings" or "close_settings" or "open_setup" or "close_setup"
                    or "pick_folder" or "save_answer_pdf" or "save_chat_export" or "open_companion_appearance"
                    or "detach_companion" or "sync_companion" or "pet_yield_rect"
                    or "hide_companion" or "reset_companion_position" or "attach_companion"
                    or "begin_companion_drag")
                || (methodText is "open_settings" or "open_setup"
                    && (args.GetArrayLength() != 1 || args[0].ValueKind != JsonValueKind.String))
                || (methodText is "close_settings" or "close_setup" or "pick_folder" or "open_companion_appearance"
                    && args.GetArrayLength() != 0)
                || (methodText is "hide_companion" or "reset_companion_position" or "attach_companion"
                    or "begin_companion_drag"
                    && args.GetArrayLength() != 0)
                || (methodText is "detach_companion" or "sync_companion"
                    && (args.GetArrayLength() > 1
                        || args.GetArrayLength() == 1 && args[0].ValueKind is not (JsonValueKind.Object or JsonValueKind.Null)))
                || (methodText == "pet_yield_rect"
                    && (args.GetArrayLength() != 1
                        || args[0].ValueKind is not (JsonValueKind.Object or JsonValueKind.Null)))
                || (methodText == "save_answer_pdf" && args.GetArrayLength() != 2)
                || (methodText == "save_chat_export" && args.GetArrayLength() != 4))
                return false;
            request = new BridgeRequest(idText, methodText, args.Clone());
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    internal static bool TrustedPage(string raw, string expectedPath)
    {
        return Uri.TryCreate(raw, UriKind.Absolute, out var uri)
            && uri.Scheme == ApiOrigin.Scheme
            && uri.Host == ApiOrigin.Host
            && uri.Port == ApiOrigin.Port
            && uri.AbsolutePath == expectedPath;
    }

    internal static string CleanSection(JsonElement arguments, bool setup)
    {
        if (arguments.GetArrayLength() == 0)
            return "";
        var item = arguments[0];
        if (item.ValueKind != JsonValueKind.String)
            return "";
        var text = (item.GetString() ?? "").Trim().TrimStart('#');
        if (setup && text.StartsWith("step=", StringComparison.Ordinal))
            text = text[5..];
        return text.Length <= 64 && text.All(c => char.IsAsciiLetterOrDigit(c) || c is '_' or '-')
            ? text : "";
    }

    private static bool HasDuplicateProperties(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Array)
            return element.EnumerateArray().Any(HasDuplicateProperties);
        if (element.ValueKind != JsonValueKind.Object)
            return false;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
            if (!seen.Add(property.Name) || HasDuplicateProperties(property.Value))
                return true;
        return false;
    }
}
