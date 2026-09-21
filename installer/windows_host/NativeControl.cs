using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace Vool.WindowsHost;

internal static class NativeControl
{
    internal const int SecretBytes = 32;
    internal const int MaxBodyBytes = 64 * 1024;
    private static readonly Uri ApiOrigin = new("http://127.0.0.1:11435/");

    internal static bool IsProtected(Uri uri, string method)
    {
        return method.Equals("POST", StringComparison.OrdinalIgnoreCase)
            && uri.Scheme == ApiOrigin.Scheme
            && uri.Host == ApiOrigin.Host
            && uri.Port == ApiOrigin.Port
            && (uri.AbsolutePath == "/api/mode" || uri.AbsolutePath == "/api/commands/dispatch");
    }

    internal static IReadOnlyDictionary<string, string> Proof(
        byte[] secret, string method, string pathAndQuery, byte[] body,
        string? nonce = null, long? issuedAt = null)
    {
        if (secret.Length != SecretBytes)
            throw new ArgumentException("Native control secret must be 32 bytes.", nameof(secret));
        nonce ??= Convert.ToBase64String(RandomNumberGenerator.GetBytes(24))
            .TrimEnd('=').Replace('+', '-').Replace('/', '_');
        var stamp = (issuedAt ?? DateTimeOffset.UtcNow.ToUnixTimeSeconds()).ToString(CultureInfo.InvariantCulture);
        var bodyHash = Convert.ToHexStringLower(SHA256.HashData(body));
        var message = JsonSerializer.Serialize(new[] { method.ToUpperInvariant(), pathAndQuery, nonce, stamp, bodyHash });
        var signature = Convert.ToHexStringLower(HMACSHA256.HashData(secret, Encoding.ASCII.GetBytes(message)));
        return new Dictionary<string, string>
        {
            ["X-VOOL-Native-Nonce"] = nonce,
            ["X-VOOL-Native-Time"] = stamp,
            ["X-VOOL-Native-Proof"] = signature,
        };
    }

    internal static ReviewRequest? Review(Uri uri, string method, byte[] body)
    {
        if (!IsProtected(uri, method) || body.Length is 0 or > MaxBodyBytes)
            return null;
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
                return null;
            // The server uses the last value of a duplicate JSON key. Refuse an ambiguous
            // approval instead of showing a reviewer two competing operations or targets.
            if (HasDuplicateProperties(root))
                return null;
            var display = JsonSerializer.Serialize(root, new JsonSerializerOptions { WriteIndented = true });
            if (uri.AbsolutePath == "/api/commands/dispatch")
            {
                if (!root.TryGetProperty("command_id", out var command) || command.ValueKind != JsonValueKind.String)
                    return null;
                var id = command.GetString();
                if (string.IsNullOrWhiteSpace(id))
                    return null;
                return new ReviewRequest($"Run command: {id}", display, true);
            }
            if (!root.TryGetProperty("op", out var operation) || operation.ValueKind != JsonValueKind.String)
                return null;
            var op = operation.GetString();
            return op switch
            {
                "set" when root.TryGetProperty("mode", out var mode) && mode.ValueKind == JsonValueKind.String
                    && (mode.GetString() is "manual" or "review_edits" or "plan" or "auto" or "bypass_permissions")
                    => new ReviewRequest($"Set permissions mode: {mode.GetString()}", display,
                        mode.GetString() != "manual"),
                "activate_bypass" => new ReviewRequest("Activate bypass permissions", display, true),
                "grant_chat_workspace" => new ReviewRequest("Grant chat workspace access", display, true),
                "resolve_approval" when root.TryGetProperty("decision", out var decision)
                    && decision.ValueKind == JsonValueKind.String
                    && (decision.GetString() is "allow" or "deny")
                    => new ReviewRequest($"Resolve action: {decision.GetString()}", display,
                        decision.GetString() != "deny"),
                "revoke_bypass" => new ReviewRequest("Revoke bypass permissions", display, false),
                "revoke_chat_workspace" => new ReviewRequest("Revoke chat workspace access", display, false),
                _ => null,
            };
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static bool HasDuplicateProperties(JsonElement element)
    {
        if (element.ValueKind == JsonValueKind.Array)
            return element.EnumerateArray().Any(HasDuplicateProperties);
        if (element.ValueKind != JsonValueKind.Object)
            return false;

        var keys = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in element.EnumerateObject())
            if (!keys.Add(property.Name) || HasDuplicateProperties(property.Value))
                return true;
        return false;
    }
}

internal sealed record ReviewRequest(string Title, string Details, bool NeedsConfirmation);
