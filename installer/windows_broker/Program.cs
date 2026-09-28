using System.Globalization;
using System.Text.Json;

namespace Vool.WindowsBroker;

internal static class Program
{
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 0 || args[0].Equals("help", StringComparison.OrdinalIgnoreCase))
            {
                PrintUsage();
                return args.Length == 0 ? 2 : 0;
            }

            return args[0].ToLowerInvariant() switch
            {
                "support" => PrintSupport(),
                "probe" => Probe.Run(args[1..]),
                "run" => RunBroker(args[1..]),
                "plugin" => RunPlugin(),
                "workspace" => RunWorkspace(),
                "workspace-write-worker" => RunWorkspaceWorker(),
                _ => throw new ArgumentException($"Unknown command: {args[0]}"),
            };
        }
        catch (Exception exception)
        {
            var error = new
            {
                ok = false,
                error = exception.GetType().Name,
                message = exception.Message,
            };
            Console.WriteLine(JsonSerializer.Serialize(error, JsonOptions));
            return 1;
        }
    }

    private static int PrintSupport()
    {
        Console.WriteLine(JsonSerializer.Serialize(
            WindowsExecutionBroker.GetSupportReport(),
            JsonOptions));
        return 0;
    }

    private static int RunBroker(IReadOnlyList<string> args)
    {
        string? executable = null;
        var commandArguments = new List<string>();
        var timeout = BrokerLimits.DefaultTimeout;
        var stage = true;

        for (var index = 0; index < args.Count; index++)
        {
            switch (args[index])
            {
                case "--exe":
                    executable = ReadValue(args, ref index, "--exe");
                    break;
                case "--arg":
                    commandArguments.Add(ReadValue(args, ref index, "--arg"));
                    break;
                case "--timeout-ms":
                    var timeoutText = ReadValue(args, ref index, "--timeout-ms");
                    if (!int.TryParse(timeoutText, NumberStyles.Integer, CultureInfo.InvariantCulture, out var timeoutMs))
                        throw new ArgumentException("--timeout-ms must be an integer.");
                    timeout = TimeSpan.FromMilliseconds(timeoutMs);
                    break;
                case "--no-stage":
                    stage = false;
                    break;
                default:
                    throw new ArgumentException($"Unknown run argument: {args[index]}");
            }
        }

        if (string.IsNullOrWhiteSpace(executable))
            throw new ArgumentException("run requires --exe.");

        var result = new WindowsExecutionBroker().ExecuteAsync(
                new BrokerExecutionRequest(executable, commandArguments, timeout, stage))
            .GetAwaiter()
            .GetResult();
        Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
        return result.TimedOut || result.ExitCode != 0 ? 1 : 0;
    }

    private static int RunPlugin()
    {
        try
        {
            var requestText = Console.In.ReadToEnd();
            var request = JsonSerializer.Deserialize<PluginBrokerRequest>(requestText, JsonOptions)
                ?? throw new BrokerSecurityException("The plugin broker request was empty.");
            var result = new WindowsExecutionBroker()
                .ExecutePluginAsync(request)
                .GetAwaiter()
                .GetResult();
            Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
            return result.Ok ? 0 : 1;
        }
        catch (Exception exception)
        {
            var result = new PluginBrokerExecutionResult(
                false,
                "confinement_unavailable",
                "",
                "",
                -1,
                false,
                null,
                exception.Message);
            Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
            return 1;
        }
    }

    private static int RunWorkspace()
    {
        try
        {
            var requestText = Console.In.ReadToEnd();
            var request = JsonSerializer.Deserialize<WorkspaceWriteRequest>(requestText, JsonOptions)
                ?? throw new BrokerSecurityException("The workspace write request was empty.");
            var result = new WindowsExecutionBroker()
                .ExecuteWorkspaceWriteAsync(request)
                .GetAwaiter()
                .GetResult();
            Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
            return result.Ok ? 0 : 1;
        }
        catch (Exception exception)
        {
            Console.WriteLine(JsonSerializer.Serialize(new
            {
                ok = false,
                status = "confinement_unavailable",
                error = exception.Message,
            }, JsonOptions));
            return 1;
        }
    }

    private static int RunWorkspaceWorker()
    {
        try
        {
            if (!string.Equals(
                    Environment.GetEnvironmentVariable("VOOL_WORKSPACE_WRITE_WORKER"),
                    "1",
                    StringComparison.Ordinal))
            {
                throw new BrokerSecurityException("Workspace write worker mode is broker-only.");
            }
            var requestText = Console.In.ReadToEnd();
            var request = JsonSerializer.Deserialize<WorkspaceWriteRequest>(requestText, JsonOptions)
                ?? throw new BrokerSecurityException("The workspace write worker request was empty.");
            var result = WorkspaceWriteBroker.RunWorker(request);
            Console.WriteLine(JsonSerializer.Serialize(result, JsonOptions));
            return result.Ok ? 0 : 1;
        }
        catch (Exception exception)
        {
            Console.WriteLine(JsonSerializer.Serialize(new WorkspaceWriteWorkerResult(
                false,
                "refused",
                "",
                "",
                "",
                0,
                false,
                exception.Message), JsonOptions));
            return 1;
        }
    }

    private static string ReadValue(IReadOnlyList<string> args, ref int index, string option)
    {
        if (++index >= args.Count)
            throw new ArgumentException($"Missing value for {option}.");
        return args[index];
    }

    private static void PrintUsage()
    {
        Console.Error.WriteLine("vool-broker support");
        Console.Error.WriteLine("vool-broker probe [--inspect|--write-scratch NAME|--file PATH|--write-file PATH|--socket HOST PORT|--spawn-child-sleeper MS]");
        Console.Error.WriteLine("vool-broker run --exe PATH [--arg VALUE ...] [--timeout-ms N]");
        Console.Error.WriteLine("vool-broker plugin < JSON request on stdin");
        Console.Error.WriteLine("vool-broker workspace < JSON request on stdin");
    }

    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        PropertyNameCaseInsensitive = true,
    };
}
