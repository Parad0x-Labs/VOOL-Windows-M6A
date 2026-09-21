using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Vool.WindowsHost;

internal sealed record CompanionPayload(
    string State,
    string Character,
    string Pack,
    string Caption,
    [property: JsonPropertyName("desktop_position")] PetPosition? DesktopPosition)
{
    private static readonly HashSet<string> States = [
        "idle", "starting", "thinking", "tool", "waiting", "approval", "retry",
        "success", "failure", "cancelled", "unknown",
    ];
    private static readonly HashSet<string> Characters = ["spark", "rascal", "prime", "prism", "veil", "ember"];
    private static readonly HashSet<string> Packs = ["default", "ninja", "moss", "cyber"];
    private static readonly JsonSerializerOptions RendererJson = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    internal static CompanionPayload Normalize(JsonElement value)
    {
        var state = Choice(value, "state", "idle", "unknown", States);
        var character = Choice(value, "character", "spark", "spark", Characters);
        var pack = Choice(value, "pack", "default", "default", Packs);
        var caption = StringField(value, "caption");
        caption = string.Concat((caption ?? state.ToUpperInvariant()).Trim().EnumerateRunes()
            .Take(64).Select(rune => rune.ToString()));
        if (caption.Length == 0)
            caption = state.ToUpperInvariant();
        PetPosition? position = null;
        if (value.ValueKind == JsonValueKind.Object
            && value.TryGetProperty("desktop_position", out var raw)
            && raw.ValueKind == JsonValueKind.Object
            && raw.TryGetProperty("x", out var xValue)
            && raw.TryGetProperty("y", out var yValue)
            && Coordinate(xValue, out var x) && Coordinate(yValue, out var y))
            position = new PetPosition(x, y);
        return new CompanionPayload(state, character, pack, caption, position);
    }

    // JsonSerializer's default encoder escapes '<', '>' and '&' for a script element.
    internal string ToRendererJson() => JsonSerializer.Serialize(this, RendererJson);

    private static string Choice(JsonElement value, string name, string missing,
        string unknown, HashSet<string> allowed)
    {
        var raw = StringField(value, name);
        if (string.IsNullOrEmpty(raw))
            return missing;
        var clean = raw.ToLowerInvariant();
        return allowed.Contains(clean) ? clean : unknown;
    }

    private static string? StringField(JsonElement value, string name) =>
        value.ValueKind == JsonValueKind.Object
        && value.TryGetProperty(name, out var field)
        && field.ValueKind == JsonValueKind.String
            ? field.GetString() : null;

    private static bool Coordinate(JsonElement value, out double number)
    {
        number = 0;
        var parsed = value.ValueKind switch
        {
            JsonValueKind.Number => value.TryGetDouble(out number),
            JsonValueKind.String => double.TryParse(value.GetString(), NumberStyles.Float,
                CultureInfo.InvariantCulture, out number),
            _ => false,
        };
        return parsed && double.IsFinite(number) && Math.Abs(number) <= 60000;
    }
}

internal sealed record PetPosition(double X, double Y);
