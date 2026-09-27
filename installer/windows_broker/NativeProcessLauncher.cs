using System.Buffers;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace Vool.WindowsBroker;

internal sealed record NativeProcessResult(
    int ExitCode,
    bool TimedOut,
    string Stdout,
    string Stderr);

internal sealed class NativeProcessLauncher : IDisposable
{
    private readonly SafeNativeHandle process;
    private readonly FileStream stdout;
    private readonly FileStream stderr;
    private bool disposed;

    private NativeProcessLauncher(
        SafeNativeHandle process,
        FileStream stdout,
        FileStream stderr,
        int processId,
        TokenSecuritySnapshot tokenSecurity,
        bool jobOwned)
    {
        this.process = process;
        this.stdout = stdout;
        this.stderr = stderr;
        ProcessId = processId;
        TokenSecurity = tokenSecurity;
        JobOwned = jobOwned;
    }

    internal int ProcessId { get; }
    internal TokenSecuritySnapshot TokenSecurity { get; }
    internal bool JobOwned { get; }

    internal static NativeProcessLauncher Start(
        IntPtr appContainerSid,
        string expectedAppContainerSid,
        WindowsJob job,
        string executablePath,
        IReadOnlyList<string> arguments,
        string workingDirectory,
        IReadOnlyDictionary<string, string> environment,
        string? standardInput = null)
    {
        var securityAttributes = new NativeMethods.SecurityAttributes
        {
            Length = Marshal.SizeOf<NativeMethods.SecurityAttributes>(),
            InheritHandle = true,
        };

        SafeNativeHandle? stdin = null;
        SafeNativeHandle? stdinRead = null;
        SafeNativeHandle? stdinWrite = null;
        SafeNativeHandle? stdoutRead = null;
        SafeNativeHandle? stdoutWrite = null;
        SafeNativeHandle? stderrRead = null;
        SafeNativeHandle? stderrWrite = null;
        SafeNativeHandle? process = null;
        SafeNativeHandle? thread = null;
        FileStream? stdoutStream = null;
        FileStream? stderrStream = null;
        var transferredStdoutRead = false;
        var transferredStderrRead = false;

        try
        {
            if (standardInput is null)
            {
                stdin = CreateInheritableNullInput();
            }
            else
            {
                CreatePipe(ref securityAttributes, out stdinRead, out stdinWrite);
                ClearInheritFlag(stdinWrite);
            }
            CreatePipe(ref securityAttributes, out stdoutRead, out stdoutWrite);
            CreatePipe(ref securityAttributes, out stderrRead, out stderrWrite);
            ClearInheritFlag(stdoutRead);
            ClearInheritFlag(stderrRead);

            var inheritedHandles = new[]
            {
                (stdin ?? stdinRead!).DangerousGetHandle(),
                stdoutWrite.DangerousGetHandle(),
                stderrWrite.DangerousGetHandle(),
            };

            var attributeSize = IntPtr.Zero;
            NativeMethods.InitializeProcThreadAttributeList(
                IntPtr.Zero,
                2,
                0,
                ref attributeSize);
            var firstError = Marshal.GetLastWin32Error();
            if (attributeSize == IntPtr.Zero || firstError != NativeMethods.ErrorInsufficientBuffer)
                throw new Win32Exception(firstError, "InitializeProcThreadAttributeList(size)");

            var attributeList = IntPtr.Zero;
            var handlesBuffer = IntPtr.Zero;
            var securityCapabilitiesBuffer = IntPtr.Zero;
            var commandLine = IntPtr.Zero;
            var environmentBlock = IntPtr.Zero;
            var attributeListInitialized = false;
            try
            {
                attributeList = Marshal.AllocHGlobal(checked((int)attributeSize));
                handlesBuffer = Marshal.AllocHGlobal(checked(IntPtr.Size * inheritedHandles.Length));
                securityCapabilitiesBuffer = Marshal.AllocHGlobal(
                    Marshal.SizeOf<NativeMethods.SecurityCapabilities>());
                commandLine = Marshal.StringToHGlobalUni(
                    WindowsCommandLine.Build(executablePath, arguments));
                environmentBlock = Marshal.StringToHGlobalUni(
                    WindowsEnvironment.Build(environment));
                if (!NativeMethods.InitializeProcThreadAttributeList(
                        attributeList,
                        2,
                        0,
                        ref attributeSize))
                {
                    throw NativeMethods.LastError("InitializeProcThreadAttributeList");
                }
                attributeListInitialized = true;

                for (var index = 0; index < inheritedHandles.Length; index++)
                    Marshal.WriteIntPtr(handlesBuffer, index * IntPtr.Size, inheritedHandles[index]);

                if (!NativeMethods.UpdateProcThreadAttribute(
                        attributeList,
                        0,
                        new IntPtr(NativeMethods.ProcThreadAttributeHandleList),
                        handlesBuffer,
                        new IntPtr(IntPtr.Size * inheritedHandles.Length),
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw NativeMethods.LastError("UpdateProcThreadAttribute(handle allowlist)");
                }

                var securityCapabilities = new NativeMethods.SecurityCapabilities
                {
                    AppContainerSid = appContainerSid,
                    Capabilities = IntPtr.Zero,
                    CapabilityCount = 0,
                    Reserved = 0,
                };
                Marshal.StructureToPtr(securityCapabilities, securityCapabilitiesBuffer, false);
                if (!NativeMethods.UpdateProcThreadAttribute(
                        attributeList,
                        0,
                        new IntPtr(NativeMethods.ProcThreadAttributeSecurityCapabilities),
                        securityCapabilitiesBuffer,
                        new IntPtr(Marshal.SizeOf<NativeMethods.SecurityCapabilities>()),
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw NativeMethods.LastError("UpdateProcThreadAttribute(AppContainer)");
                }

                var startupInfo = new NativeMethods.StartupInfoEx
                {
                    StartupInfo = new NativeMethods.StartupInfo
                    {
                        Cb = Marshal.SizeOf<NativeMethods.StartupInfoEx>(),
                        Flags = NativeMethods.StartupUseStdHandles,
                        StandardInput = (stdin ?? stdinRead!).DangerousGetHandle(),
                        StandardOutput = stdoutWrite.DangerousGetHandle(),
                        StandardError = stderrWrite.DangerousGetHandle(),
                    },
                    AttributeList = attributeList,
                };

                var creationFlags = NativeMethods.CreateSuspended
                    | NativeMethods.CreateUnicodeEnvironment
                    | NativeMethods.CreateNoWindow
                    | NativeMethods.ExtendedStartupInfoPresent;
                if (!NativeMethods.CreateProcessW(
                        executablePath,
                        commandLine,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        inheritHandles: true,
                        creationFlags,
                        environmentBlock,
                        workingDirectory,
                        ref startupInfo,
                        out var processInformation))
                {
                    throw NativeMethods.LastError("CreateProcessW(AppContainer)");
                }

                process = new SafeNativeHandle(processInformation.Process, ownsHandle: true);
                thread = new SafeNativeHandle(processInformation.Thread, ownsHandle: true);
                if (process.IsInvalid || thread.IsInvalid)
                    throw new BrokerSecurityException("CreateProcessW returned an invalid process handle.");

                if (!NativeMethods.AssignProcessToJobObject(job.Handle, process))
                {
                    NativeMethods.TerminateProcess(process, 0xC0000001);
                    NativeMethods.WaitForSingleObject(process, 5_000);
                    throw NativeMethods.LastError("AssignProcessToJobObject");
                }

                if (!NativeMethods.IsProcessInJob(
                        process.DangerousGetHandle(),
                        job.Handle.DangerousGetHandle(),
                        out var jobOwned)
                    || !jobOwned)
                {
                    NativeMethods.TerminateProcess(process, 0xC0000001);
                    NativeMethods.WaitForSingleObject(process, 5_000);
                    throw new BrokerSecurityException(
                        "The child was not confirmed inside the broker Job Object.");
                }

                if (!NativeMethods.OpenProcessToken(
                        process.DangerousGetHandle(),
                        NativeMethods.TokenQuery,
                        out var launchedToken))
                {
                    throw NativeMethods.LastError("OpenProcessToken(launched child)");
                }
                TokenSecuritySnapshot tokenSecurity;
                using (launchedToken)
                {
                    tokenSecurity = TokenInspector.Inspect(launchedToken);
                    TokenInspector.RequireUntrustedExecutionToken(
                        tokenSecurity,
                        expectedAppContainerSid);
                }

                if (NativeMethods.ResumeThread(thread) == uint.MaxValue)
                {
                    NativeMethods.TerminateJobObject(job.Handle, 0xC0000001);
                    NativeMethods.WaitForSingleObject(process, 5_000);
                    throw NativeMethods.LastError("ResumeThread");
                }

                if (standardInput is not null)
                {
                    using var inputStream = TransferWriteHandle(stdinWrite!);
                    stdinWrite = null;
                    var inputBytes = Encoding.UTF8.GetBytes(standardInput);
                    inputStream.Write(inputBytes, 0, inputBytes.Length);
                    inputStream.Flush();
                }

                stdoutStream = TransferReadHandle(stdoutRead);
                transferredStdoutRead = true;
                stdoutRead = null;
                stderrStream = TransferReadHandle(stderrRead);
                transferredStderrRead = true;
                stderrRead = null;
                var launched = new NativeProcessLauncher(
                    process,
                    stdoutStream,
                    stderrStream,
                    processInformation.ProcessId,
                    tokenSecurity,
                    jobOwned);
                process = null;
                return launched;
            }
            finally
            {
                if (attributeListInitialized)
                    NativeMethods.DeleteProcThreadAttributeList(attributeList);
                if (attributeList != IntPtr.Zero)
                    Marshal.FreeHGlobal(attributeList);
                if (handlesBuffer != IntPtr.Zero)
                    Marshal.FreeHGlobal(handlesBuffer);
                if (securityCapabilitiesBuffer != IntPtr.Zero)
                    Marshal.FreeHGlobal(securityCapabilitiesBuffer);
                if (commandLine != IntPtr.Zero)
                    Marshal.FreeHGlobal(commandLine);
                if (environmentBlock != IntPtr.Zero)
                    Marshal.FreeHGlobal(environmentBlock);
            }
        }
        catch
        {
            stdoutStream?.Dispose();
            stderrStream?.Dispose();
            if (process is not null && !process.IsInvalid)
            {
                NativeMethods.TerminateProcess(process, 0xC0000001);
                NativeMethods.WaitForSingleObject(process, 5_000);
            }
            throw;
        }
        finally
        {
            thread?.Dispose();
            process?.Dispose();
            stdin?.Dispose();
            stdinRead?.Dispose();
            stdinWrite?.Dispose();
            stdoutWrite?.Dispose();
            stderrWrite?.Dispose();
            if (!transferredStdoutRead)
                stdoutRead?.Dispose();
            if (!transferredStderrRead)
                stderrRead?.Dispose();
        }
    }

    internal async Task<NativeProcessResult> WaitAsync(
        SafeNativeHandle job,
        TimeSpan timeout,
        CancellationToken cancellationToken = default)
    {
        ThrowIfDisposed();
        var stdoutTask = CaptureAsync(stdout, cancellationToken);
        var stderrTask = CaptureAsync(stderr, cancellationToken);
        var watch = Stopwatch.StartNew();
        var timedOut = false;
        try
        {
            while (true)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var remaining = timeout - watch.Elapsed;
                if (remaining <= TimeSpan.Zero)
                {
                    timedOut = true;
                    break;
                }

                var slice = checked((uint)Math.Clamp(remaining.TotalMilliseconds, 1, 100));
                var waitResult = await Task.Run(
                    () => NativeMethods.WaitForSingleObject(process, slice)).ConfigureAwait(false);
                if (waitResult == NativeMethods.WaitObject0)
                    break;
                if (waitResult != NativeMethods.WaitTimeout)
                    throw NativeMethods.LastError("WaitForSingleObject");
            }
        }
        catch (OperationCanceledException)
        {
            WindowsJob.TerminateAndWait(job);
            try
            {
                await Task.WhenAll(stdoutTask, stderrTask).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                // The caller canceled reads after the process tree was terminated.
            }
            throw;
        }

        if (timedOut)
        {
            WindowsJob.TerminateAndWait(job);
            var terminated = NativeMethods.WaitForSingleObject(process, 5_000);
            if (terminated != NativeMethods.WaitObject0)
                throw new BrokerSecurityException("Timed-out process did not terminate with its job.");
        }
        else
        {
            WindowsJob.TerminateAndWait(job);
        }

        if (!NativeMethods.GetExitCodeProcess(process, out var exitCode))
            throw NativeMethods.LastError("GetExitCodeProcess");

        var output = await Task.WhenAll(stdoutTask, stderrTask).ConfigureAwait(false);
        return new NativeProcessResult(
            unchecked((int)exitCode),
            timedOut,
            output[0],
            output[1]);
    }

    public void Dispose()
    {
        if (disposed)
            return;
        disposed = true;
        stdout.Dispose();
        stderr.Dispose();
        process.Dispose();
    }

    private static SafeNativeHandle CreateInheritableNullInput()
    {
        var attributes = Marshal.AllocHGlobal(Marshal.SizeOf<NativeMethods.SecurityAttributes>());
        try
        {
            var securityAttributes = new NativeMethods.SecurityAttributes
            {
                Length = Marshal.SizeOf<NativeMethods.SecurityAttributes>(),
                InheritHandle = true,
            };
            Marshal.StructureToPtr(securityAttributes, attributes, fDeleteOld: false);
            var raw = NativeMethods.CreateFileW(
                "NUL",
                NativeMethods.GenericRead,
                NativeMethods.FileShareRead | NativeMethods.FileShareWrite | NativeMethods.FileShareDelete,
                attributes,
                NativeMethods.OpenExisting,
                NativeMethods.FileAttributeNormal,
                IntPtr.Zero);
            var handle = new SafeNativeHandle(raw, ownsHandle: true);
            if (handle.IsInvalid)
            {
                handle.Dispose();
                throw NativeMethods.LastError("CreateFileW(NUL)");
            }
            return handle;
        }
        finally
        {
            Marshal.FreeHGlobal(attributes);
        }
    }

    private static void CreatePipe(
        ref NativeMethods.SecurityAttributes securityAttributes,
        out SafeNativeHandle read,
        out SafeNativeHandle write)
    {
        if (!NativeMethods.CreatePipe(out read, out write, ref securityAttributes, 0))
            throw NativeMethods.LastError("CreatePipe");
    }

    private static void ClearInheritFlag(SafeNativeHandle handle)
    {
        if (!NativeMethods.SetHandleInformation(handle, NativeMethods.HandleFlagInherit, 0))
            throw NativeMethods.LastError("SetHandleInformation");
    }

    private static FileStream TransferReadHandle(SafeNativeHandle handle)
    {
        var safeFileHandle = new SafeFileHandle(handle.DangerousGetHandle(), ownsHandle: true);
        handle.SetHandleAsInvalid();
        return new FileStream(safeFileHandle, FileAccess.Read, 64 * 1024, isAsync: false);
    }

    private static FileStream TransferWriteHandle(SafeNativeHandle handle)
    {
        var safeFileHandle = new SafeFileHandle(handle.DangerousGetHandle(), ownsHandle: true);
        handle.SetHandleAsInvalid();
        return new FileStream(safeFileHandle, FileAccess.Write, 64 * 1024, isAsync: false);
    }

    private static async Task<string> CaptureAsync(Stream stream, CancellationToken cancellationToken)
    {
        var rented = ArrayPool<byte>.Shared.Rent(64 * 1024);
        using var captured = new MemoryStream();
        var truncated = false;
        try
        {
            while (true)
            {
                var read = await stream.ReadAsync(rented.AsMemory(), cancellationToken).ConfigureAwait(false);
                if (read == 0)
                    break;

                var remaining = BrokerLimits.MaximumCapturedOutputBytes - captured.Length;
                if (remaining > 0)
                {
                    var take = (int)Math.Min(remaining, read);
                    captured.Write(rented, 0, take);
                    if (take != read)
                        truncated = true;
                }
                else
                {
                    truncated = true;
                }
            }

            var text = Encoding.UTF8.GetString(captured.GetBuffer(), 0, checked((int)captured.Length));
            return truncated ? $"{text}\n[output truncated at {BrokerLimits.MaximumCapturedOutputBytes} bytes]" : text;
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(rented);
        }
    }

    private void ThrowIfDisposed()
    {
        if (disposed)
            throw new ObjectDisposedException(nameof(NativeProcessLauncher));
    }
}

internal static class WindowsCommandLine
{
    internal static string Build(string executablePath, IReadOnlyList<string> arguments)
    {
        var values = new string[arguments.Count + 1];
        values[0] = executablePath;
        for (var index = 0; index < arguments.Count; index++)
            values[index + 1] = arguments[index];
        return string.Join(" ", values.Select(Quote));
    }

    private static string Quote(string value)
    {
        var builder = new StringBuilder(value.Length + 2);
        builder.Append('"');
        var backslashes = 0;
        foreach (var character in value)
        {
            if (character == '\\')
            {
                backslashes++;
                continue;
            }

            if (character == '"')
            {
                builder.Append('\\', backslashes * 2 + 1);
                builder.Append('"');
                backslashes = 0;
                continue;
            }

            builder.Append('\\', backslashes);
            builder.Append(character);
            backslashes = 0;
        }

        builder.Append('\\', backslashes * 2);
        builder.Append('"');
        return builder.ToString();
    }
}

internal static class WindowsEnvironment
{
    internal static string Build(IReadOnlyDictionary<string, string> values)
    {
        var entries = values
            .OrderBy(pair => pair.Key, StringComparer.OrdinalIgnoreCase)
            .Select(pair => $"{pair.Key}={pair.Value}");
        return string.Join('\0', entries) + "\0\0";
    }
}
