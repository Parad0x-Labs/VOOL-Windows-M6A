using VoolConfine.Core;

// VoolConfine.exe --policy <path> -- <program> [args...]   (docs/WINDOWS_CONFINEMENT.md)
// Chunk 0: parses and validates, then refuses. Nothing is ever started unconfined.

static int Refuse(int code, string reason)
{
    Console.Error.WriteLine($"{ExitCodes.RefusedMarker} {reason}");
    return code;
}

var separator = Array.IndexOf(args, "--");
if (args.Length < 4 || args[0] != "--policy" || separator != 2 || separator == args.Length - 1)
    return Refuse(ExitCodes.PolicyInvalid, "usage: VoolConfine.exe --policy <path> -- <program> [args...]");
try
{
    Policy.Parse(File.ReadAllText(args[1]));
}
catch (PolicyException e)
{
    return Refuse(ExitCodes.PolicyInvalid, e.Message);
}
catch (IOException e)
{
    return Refuse(ExitCodes.PolicyInvalid, $"policy unreadable: {e.Message}");
}
catch (UnauthorizedAccessException e)
{
    return Refuse(ExitCodes.PolicyInvalid, $"policy unreadable: {e.Message}");
}
return Refuse(ExitCodes.SetupFailed, "confinement is not implemented yet (interface stub)");
