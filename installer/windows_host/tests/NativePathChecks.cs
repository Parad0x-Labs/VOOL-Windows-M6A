using System;
using System.IO;
using Vool.WindowsHost;

internal static class NativePathChecks
{
    internal static void Run(Action<bool, string> check)
    {
        var token = Guid.NewGuid().ToString("N");
        var bundleRoot = Path.Combine(Path.GetTempPath(), "vool-native-paths-" + token, "bundle");
        var nativeExecutable = Path.Combine(bundleRoot, "native", "VOOL.exe");
        var resolvedBundleRoot = NativePathResolver.ResolveBundleRoot(nativeExecutable);
        check(PathsEqual(resolvedBundleRoot, bundleRoot), "native host resolves its temporary bundle root");

        var installedDefaultHome = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "VOOL");
        check(!PathsEqual(resolvedBundleRoot, installedDefaultHome),
            "native host does not resolve the default installed VOOL root");

        var localAppDataRoot = Path.Combine(bundleRoot, "synthetic-local-appdata");
        var explicitHome = Path.Combine(bundleRoot, "temporary-profile");
        var explicitWebViewProfile = NativePathResolver.ResolveWebView2UserDataFolder(
            explicitHome, localAppDataRoot);
        check(PathsEqual(explicitWebViewProfile, Path.Combine(explicitHome, "WebView2")),
            "explicit VOOL_HOME owns the WebView2 profile");

        var defaultWebViewProfile = NativePathResolver.ResolveWebView2UserDataFolder(
            null, localAppDataRoot);
        check(PathsEqual(defaultWebViewProfile, Path.Combine(localAppDataRoot, "VOOL", "WebView2")),
            "absent VOOL_HOME preserves the per-user default WebView2 profile");

        check(!Directory.Exists(bundleRoot), "path resolution does not create the bundle root");
        check(!Directory.Exists(explicitHome), "path resolution does not create explicit VOOL_HOME");
        check(!Directory.Exists(explicitWebViewProfile), "path resolution does not create explicit WebView2 profile");
        check(!Directory.Exists(defaultWebViewProfile), "path resolution does not create default WebView2 profile");
    }

    private static bool PathsEqual(string left, string right)
    {
        return string.Equals(Path.GetFullPath(left), Path.GetFullPath(right), StringComparison.OrdinalIgnoreCase);
    }
}
