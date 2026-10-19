namespace VoolBroker.Tests;

internal static class TestSupport
{
    public static string NewTempDirectory()
    {
        var path = Path.Combine(Path.GetTempPath(), "vool-broker-test-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(path);
        return path;
    }
}
