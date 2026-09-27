using System;
using System.IO;

namespace Vool.WindowsHost;

internal static class NativePathResolver
{
    internal static string ResolveBundleRoot(string executablePath)
    {
        if (string.IsNullOrWhiteSpace(executablePath))
            throw new InvalidOperationException("The native executable path is unavailable.");

        var fullExecutablePath = Path.GetFullPath(executablePath);
        if (!string.Equals(Path.GetFileName(fullExecutablePath), "VOOL.exe", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("The native executable is not VOOL.exe.");

        var nativeDirectory = Path.GetDirectoryName(fullExecutablePath)?.TrimEnd(
            Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        if (string.IsNullOrWhiteSpace(nativeDirectory)
            || !string.Equals(Path.GetFileName(nativeDirectory), "native", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("The native executable is not under the bundle native directory.");

        var bundleRoot = Path.GetDirectoryName(nativeDirectory);
        if (string.IsNullOrWhiteSpace(bundleRoot))
            throw new InvalidOperationException("The bundle root cannot be resolved from the native executable.");

        return Path.GetFullPath(bundleRoot);
    }

    internal static string ResolveRuntimeHome(string? explicitVoolHome, string localAppDataRoot)
    {
        if (!string.IsNullOrWhiteSpace(explicitVoolHome))
            return Path.GetFullPath(explicitVoolHome);
        if (string.IsNullOrWhiteSpace(localAppDataRoot))
            throw new InvalidOperationException("The default local application-data root is unavailable.");

        return Path.GetFullPath(Path.Combine(localAppDataRoot, "VOOL"));
    }

    internal static string ResolveWebView2UserDataFolder(string? explicitVoolHome, string localAppDataRoot)
    {
        return Path.Combine(ResolveRuntimeHome(explicitVoolHome, localAppDataRoot), "WebView2");
    }
}
