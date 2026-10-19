namespace VoolBroker.Core;

public static class HandleWalk
{
    private static readonly HashSet<string> devices = BuildDevices();

    public static string[] ValidateRelativePath(string relativePath)
    {
        if (relativePath.Length > 32767) throw new BrokerRefusal("path_too_long", "Path exceeds 32,767 UTF-16 units.");
        var parts = relativePath.Split(['\\', '/']);
        foreach (var part in parts)
        {
            if (part.Length == 0 || part is "." or "..") throw new BrokerRefusal("path_escapes_root", "Empty or traversal component.");
            if (part.Contains(':') || part.Contains('\0') || part.Any(char.IsControl) || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || part.EndsWith('.') || part.EndsWith(' '))
                throw new BrokerRefusal("invalid_name", "Invalid Windows path component.");
            var stem = part.Split('.')[0].TrimEnd(' ', '.').ToUpperInvariant();
            if (devices.Contains(stem)) throw new BrokerRefusal("device_name", "DOS device names are refused.");
        }
        return parts;
    }

    private static HashSet<string> BuildDevices()
    {
        var result = new HashSet<string>(["CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"], StringComparer.Ordinal);
        for (var i = 0; i < 10; i++) { result.Add($"COM{i}"); result.Add($"LPT{i}"); }
        foreach (var digit in new[] { '¹', '²', '³' }) { result.Add($"COM{digit}"); result.Add($"LPT{digit}"); }
        return result;
    }
}
