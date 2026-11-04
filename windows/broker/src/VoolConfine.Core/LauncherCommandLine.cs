using System.Text;

namespace VoolConfine.Core;

/// <summary>
/// Turns the child's argv back into one Windows command line that CommandLineToArgvW and the MSVC
/// runtime split into exactly the same argv (docs/WINDOWS_CONFINEMENT.md section 2).
/// </summary>
public static class LauncherCommandLine
{
    /// <summary>
    /// The program must be an absolute drive-letter path to an .exe, because the launcher never
    /// searches PATH, and CreateProcess runs a .cmd or .bat through cmd.exe with its own quoting
    /// rules (argument injection). A quote can't appear in a Windows file name, and the program
    /// token has no escape rules, so one is refused rather than guessed at.
    /// </summary>
    public static string CheckProgram(string program)
    {
        if (string.IsNullOrEmpty(program)) throw new PolicyException("the program (argv[0]) is empty");
        if (program.Contains('"') || program.Contains('\0'))
            throw new PolicyException("the program (argv[0]) contains a quote or NUL");
        if (program.Length < 3 || !char.IsAsciiLetter(program[0]) || program[1] != ':' || (program[2] != '\\' && program[2] != '/'))
            throw new PolicyException($"the program (argv[0]) must be an absolute drive-letter path: {program}");
        if (program.Split('\\', '/').Any(part => part == ".."))
            throw new PolicyException($"the program (argv[0]) must be resolved (no '..'): {program}");
        // Exactly ".exe" at the end: a trailing dot or space is trimmed by Win32 path handling, and
        // an alternate data stream (name:stream) is not a program file, so both are refused.
        var name = program[(program.LastIndexOfAny(['\\', '/']) + 1)..];
        if (!name.EndsWith(".exe", StringComparison.OrdinalIgnoreCase) || name.Length <= 4 || name.Contains(':'))
            throw new PolicyException($"the program (argv[0]) must be an .exe (a .cmd or .bat runs through cmd.exe): {program}");
        return program;
    }

    /// <summary>Builds the command line. argv[0] is always quoted; the rest only when needed.</summary>
    public static string Build(IReadOnlyList<string> argv)
    {
        if (argv.Count == 0) throw new PolicyException("no program given");
        var program = CheckProgram(argv[0]);
        var line = new StringBuilder();
        line.Append('"').Append(program).Append('"');
        for (var i = 1; i < argv.Count; i++)
        {
            if (argv[i].Contains('\0')) throw new PolicyException($"argument {i} contains NUL");
            line.Append(' ');
            AppendArgument(line, argv[i]);
        }
        if (line.Length >= 32767) throw new PolicyException("the command line is longer than Windows allows (32767)");
        return line.ToString();
    }

    static void AppendArgument(StringBuilder line, string argument)
    {
        if (argument.Length > 0 && argument.IndexOfAny([' ', '\t', '\n', '\v', '"']) < 0)
        {
            line.Append(argument);
            return;
        }
        line.Append('"');
        var backslashes = 0;
        foreach (var c in argument)
        {
            if (c == '\\')
            {
                backslashes++;
                continue;
            }
            if (c == '"')
            {
                // Each backslash before a quote is doubled, and the quote itself is escaped.
                line.Append('\\', backslashes * 2 + 1).Append('"');
            }
            else
            {
                line.Append('\\', backslashes).Append(c);
            }
            backslashes = 0;
        }
        // Backslashes before the closing quote are doubled so they don't escape it.
        line.Append('\\', backslashes * 2).Append('"');
    }
}
