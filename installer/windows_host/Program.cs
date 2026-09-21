using System.Security.Cryptography;
using System.Diagnostics;
using System.IO;

namespace Vool.WindowsHost;

internal static class Program
{
    [STAThread]
    private static int Main()
    {
        if (Environment.GetEnvironmentVariable("VOOL_WINDOWS_BUNDLE") != "1"
            || Environment.GetEnvironmentVariable("VOOL_NATIVE_CONTROL_STDIN") != "1")
            return StartSupervisor();

        var secret = new byte[NativeControl.SecretBytes];
        try
        {
            // The installer uses this same name to refuse upgrade/uninstall while a window is open.
            using var singleton = new Mutex(initiallyOwned: true, name: @"Local\VOOL_WINDOW_SINGLETON",
                createdNew: out var firstWindow);
            if (!firstWindow)
                return 0;
            using var input = Console.OpenStandardInput();
            input.ReadExactly(secret);
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            ApplicationConfiguration.Initialize();
            Application.Run(new VoolWindow(secret));
            return 0;
        }
        catch (Exception)
        {
            MessageBox.Show(
                "VOOL could not start its protected Windows window. Repair or reinstall VOOL; your data is preserved.",
                "VOOL could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(secret);
        }
    }

    private static int StartSupervisor()
    {
        try
        {
            var executable = Environment.ProcessPath;
            if (string.IsNullOrWhiteSpace(executable))
                return 1;
            var root = Directory.GetParent(Path.GetDirectoryName(executable) ?? string.Empty)?.FullName;
            if (string.IsNullOrWhiteSpace(root))
                return 1;
            var python = Path.Combine(root, "python", "pythonw.exe");
            var supervisor = Path.Combine(root, "bundle_supervisor.py");
            if (!File.Exists(python) || !File.Exists(supervisor))
                return 1;
            Process.Start(new ProcessStartInfo
            {
                FileName = python,
                WorkingDirectory = root,
                UseShellExecute = false,
                CreateNoWindow = true,
                ArgumentList = { supervisor, "--root", root },
            });
            return 0;
        }
        catch
        {
            MessageBox.Show(
                "VOOL could not start its protected runtime. Repair or reinstall VOOL; your data is preserved.",
                "VOOL could not start", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
