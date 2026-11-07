using System.Collections;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

// VoolConfineStart.exe: the first process inside the box (docs/WINDOWS_CONFINEMENT.md, "TEMP").
// Windows replaces an AppContainer child's TEMP and TMP with the shared AC\Temp when the launcher
// creates it (windows-broker run 41902285066), but keeps the TEMP a process inside the box passes on
// (run 42358953341, NESTED-TEMP). So the launcher starts this trampoline with the real program, its
// command line and temp_dir in three variables; it sets TEMP and TMP to temp_dir, drops the three
// variables, starts the program with its command line exactly as given, in the same job (a child
// can't leave it), passing on only the standard handles it was given, and exits with its code.
// 0xE5C00125 means the program was never started (Launcher.TrampolineNotStarted); the launcher then
// refuses the run with 125 and a status file that says child_started false.
const string ProgramVar = "VOOL_CONFINE_PROGRAM";
const string CommandLineVar = "VOOL_CONFINE_COMMAND_LINE";
const string TempVar = "VOOL_CONFINE_TEMP";
const int NotStarted = unchecked((int)0xE5C00125);

var program = Environment.GetEnvironmentVariable(ProgramVar);
var commandLine = Environment.GetEnvironmentVariable(CommandLineVar);
var temp = Environment.GetEnvironmentVariable(TempVar);
if (string.IsNullOrEmpty(program) || string.IsNullOrEmpty(commandLine) || string.IsNullOrEmpty(temp)
    || !Path.IsPathFullyQualified(program) || !Path.IsPathFullyQualified(temp))
{
    Console.Error.WriteLine("VoolConfineStart: the launcher did not pass the program, its command line and temp_dir");
    return NotStarted;
}

var environment = new SortedDictionary<string, string>(StringComparer.OrdinalIgnoreCase);
foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
{
    var name = (string)entry.Key;
    if (name.Equals(ProgramVar, StringComparison.OrdinalIgnoreCase) || name.Equals(CommandLineVar, StringComparison.OrdinalIgnoreCase)
        || name.Equals(TempVar, StringComparison.OrdinalIgnoreCase)) continue;
    environment[name] = (string?)entry.Value ?? "";
}
environment["TEMP"] = temp;
environment["TMP"] = temp;
var block = new StringBuilder();
foreach (var (name, value) in environment.OrderBy(pair => pair.Key.ToUpperInvariant(), StringComparer.Ordinal))
    block.Append(name).Append('=').Append(value).Append('\0');
block.Append('\0');

var startup = new Native.StartupInfo
{
    Cb = Marshal.SizeOf<Native.StartupInfo>(),
    Flags = Native.StartfUseStdHandles,
    StdInput = Native.GetStdHandle(Native.StdInput),
    StdOutput = Native.GetStdHandle(Native.StdOutput),
    StdError = Native.GetStdHandle(Native.StdError),
};
var line = new StringBuilder(commandLine, 32768);
var environmentMemory = Marshal.StringToHGlobalUni(block.ToString());
try
{
    // Handles inherited: only the standard handles the launcher listed are inheritable here.
    if (!Native.CreateProcessW(program, line, 0, 0, true, Native.CreateUnicodeEnvironment, environmentMemory, null,
            ref startup, out var info))
    {
        Console.Error.WriteLine($"VoolConfineStart: could not start {program}: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");
        return NotStarted;
    }
    Native.CloseHandle(info.Thread);
    Native.WaitForSingleObject(info.Process, Native.Infinite);
    if (!Native.GetExitCodeProcess(info.Process, out var code))
    {
        Console.Error.WriteLine($"VoolConfineStart: the program's exit code could not be read: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");
        return 1;
    }
    Native.CloseHandle(info.Process);
    return unchecked((int)code);
}
finally
{
    Marshal.FreeHGlobal(environmentMemory);
}

static class Native
{
    internal const int StdInput = -10;
    internal const int StdOutput = -11;
    internal const int StdError = -12;
    internal const int StartfUseStdHandles = 0x100;
    internal const uint CreateUnicodeEnvironment = 0x400;
    internal const uint Infinite = 0xFFFFFFFF;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct StartupInfo
    {
        public int Cb;
        public nint Reserved;
        public nint Desktop;
        public nint Title;
        public int X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute;
        public int Flags;
        public short ShowWindow;
        public short Reserved2Size;
        public nint Reserved2;
        public nint StdInput;
        public nint StdOutput;
        public nint StdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct ProcessInformation
    {
        public nint Process;
        public nint Thread;
        public uint ProcessId;
        public uint ThreadId;
    }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    internal static extern bool CreateProcessW(string application, StringBuilder commandLine, nint processAttributes,
        nint threadAttributes, bool inheritHandles, uint flags, nint environment, string? currentDirectory,
        ref StartupInfo startupInfo, out ProcessInformation information);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern nint GetStdHandle(int which);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint WaitForSingleObject(nint handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool GetExitCodeProcess(nint process, out uint code);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern bool CloseHandle(nint handle);
}
