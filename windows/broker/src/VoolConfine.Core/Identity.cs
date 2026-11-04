using System.Security.Cryptography;
using System.Text;

namespace VoolConfine.Core;

/// <summary>AppContainer identity (docs/WINDOWS_CONFINEMENT.md section 2, "Identity").</summary>
public static class Identity
{
    public const string ProfileName = "VOOL.Confine.v1";

    /// <summary>The capability name a write root is granted to; must match
    /// sandbox.windows_confinement.root_capability_name byte for byte.</summary>
    public static string RootCapabilityName(string root)
    {
        // ASCII-only upper-casing, matching the Python side ("ß" stays "ß" on both).
        var trimmed = root.Replace('/', '\\').TrimEnd('\\');
        var normalized = string.Concat(trimmed.Select(c => c is >= 'a' and <= 'z' ? (char)(c - 32) : c));
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes(normalized));
        return "vool.root." + Convert.ToHexStringLower(hash);
    }
}
