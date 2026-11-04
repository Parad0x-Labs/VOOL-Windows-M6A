using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

namespace VoolConfine.Core;

/// <summary>A child created suspended inside the job. Disposing closes the launcher's handles to it.</summary>
internal sealed class SuspendedChild : IDisposable
{
    internal nint Process { get; private set; }
    internal nint Thread { get; private set; }
    internal uint ProcessId { get; }

    internal SuspendedChild(LauncherNative.ProcessInformation info)
    {
        Process = info.Process;
        Thread = info.Thread;
        ProcessId = info.ProcessId;
    }

    public void Dispose()
    {
        if (Thread != 0) { LauncherNative.CloseHandle(Thread); Thread = 0; }
        if (Process != 0) { LauncherNative.CloseHandle(Process); Process = 0; }
    }
}

/// <summary>
/// Creates the confined child: an AppContainer process with only this job's capabilities, inside the
/// job from its creation (PROC_THREAD_ATTRIBUTE_JOB_LIST), suspended, and inheriting only the three
/// stdio handles (PROC_THREAD_ATTRIBUTE_HANDLE_LIST). The job handle is never inheritable and never
/// in the handle list, so the child can't hold the job open.
/// </summary>
internal static class LauncherProcess
{
    const int StdInput = -10;
    const int StdOutput = -11;
    const int StdError = -12;

    internal static SuspendedChild CreateSuspendedInJob(string program, string commandLine, string packageSid,
        IReadOnlyList<string> capabilitySids, ConfinementJob job, IReadOnlyDictionary<string, string> environment)
    {
        var allocations = new List<nint>();
        var duplicated = new List<nint>();
        var attributeList = (nint)0;
        var attributeListReady = false;
        nint Alloc(int size)
        {
            var memory = Marshal.AllocHGlobal(size);
            allocations.Add(memory);
            // Zero it, so an unused field never carries garbage into the kernel.
            for (var i = 0; i < size; i++) Marshal.WriteByte(memory, i, 0);
            return memory;
        }
        nint AllocBytes(byte[] bytes)
        {
            var memory = Alloc(bytes.Length);
            Marshal.Copy(bytes, 0, memory, bytes.Length);
            return memory;
        }
        try
        {
            // SECURITY_CAPABILITIES: the package SID plus only this job's capabilities.
            var packageSidMemory = AllocBytes(Sids.SidBytes(packageSid));
            var entrySize = Marshal.SizeOf<LauncherNative.SidAndAttributes>();
            var capabilityArray = capabilitySids.Count == 0 ? 0 : Alloc(entrySize * capabilitySids.Count);
            for (var i = 0; i < capabilitySids.Count; i++)
            {
                var entry = new LauncherNative.SidAndAttributes
                {
                    Sid = AllocBytes(Sids.SidBytes(capabilitySids[i])),
                    Attributes = LauncherNative.SeGroupEnabled,
                };
                Marshal.StructureToPtr(entry, capabilityArray + i * entrySize, false);
            }
            var capabilities = new LauncherNative.SecurityCapabilities
            {
                AppContainerSid = packageSidMemory,
                Capabilities = capabilityArray,
                CapabilityCount = (uint)capabilitySids.Count,
            };
            var capabilitiesMemory = Alloc(Marshal.SizeOf<LauncherNative.SecurityCapabilities>());
            Marshal.StructureToPtr(capabilities, capabilitiesMemory, false);

            // The job list: the child is in the job before its first instruction.
            var jobList = Alloc(IntPtr.Size);
            Marshal.WriteIntPtr(jobList, job.Handle.DangerousGetHandle());

            // The stdio handles, duplicated as inheritable and listed once each.
            var self = LauncherNative.GetCurrentProcess();
            var sources = new Dictionary<nint, nint>();
            nint Inheritable(int which)
            {
                var source = LauncherNative.GetStdHandle(which);
                if (source == 0 || source == -1) return 0;
                if (sources.TryGetValue(source, out var existing)) return existing;
                if (!LauncherNative.DuplicateHandle(self, source, self, out var copy, 0, true, LauncherNative.DuplicateSameAccess))
                    throw new SetupException($"could not pass standard handle {which} to the child: {LastError()}");
                duplicated.Add(copy);
                sources[source] = copy;
                return copy;
            }
            var stdin = Inheritable(StdInput);
            var stdout = Inheritable(StdOutput);
            var stderr = Inheritable(StdError);
            var handleList = duplicated.Count == 0 ? 0 : Alloc(IntPtr.Size * duplicated.Count);
            for (var i = 0; i < duplicated.Count; i++) Marshal.WriteIntPtr(handleList, i * IntPtr.Size, duplicated[i]);

            var attributeCount = handleList == 0 ? 2 : 3;
            nint listSize = 0;
            LauncherNative.InitializeProcThreadAttributeList(0, attributeCount, 0, ref listSize);
            if (listSize == 0) throw new SetupException($"InitializeProcThreadAttributeList (size) failed: {LastError()}");
            attributeList = Alloc((int)listSize);
            if (!LauncherNative.InitializeProcThreadAttributeList(attributeList, attributeCount, 0, ref listSize))
                throw new SetupException($"InitializeProcThreadAttributeList failed: {LastError()}");
            attributeListReady = true;
            Update(attributeList, LauncherNative.ProcThreadAttributeSecurityCapabilities, capabilitiesMemory,
                Marshal.SizeOf<LauncherNative.SecurityCapabilities>());
            Update(attributeList, LauncherNative.ProcThreadAttributeJobList, jobList, IntPtr.Size);
            if (handleList != 0)
                Update(attributeList, LauncherNative.ProcThreadAttributeHandleList, handleList, IntPtr.Size * duplicated.Count);

            var startup = new LauncherNative.StartupInfoEx { AttributeList = attributeList };
            startup.StartupInfo.Cb = Marshal.SizeOf<LauncherNative.StartupInfoEx>();
            startup.StartupInfo.Flags = LauncherNative.StartfUseStdHandles;
            startup.StartupInfo.StdInput = stdin;
            startup.StartupInfo.StdOutput = stdout;
            startup.StartupInfo.StdError = stderr;

            var block = EnvironmentBlock(environment);
            var environmentMemory = Marshal.StringToHGlobalUni(block);
            allocations.Add(environmentMemory);

            var line = new StringBuilder(commandLine, 32768);
            if (!LauncherNative.CreateProcessW(program, line, 0, 0, inheritHandles: handleList != 0,
                    LauncherNative.CreateSuspended | LauncherNative.ExtendedStartupInfoPresent | LauncherNative.CreateUnicodeEnvironment,
                    environmentMemory, null, ref startup, out var info))
                throw new SetupException($"CreateProcess failed for {program}: {LastError()}");
            return new SuspendedChild(info);
        }
        finally
        {
            if (attributeListReady) LauncherNative.DeleteProcThreadAttributeList(attributeList);
            // The child has its own copies now; the launcher keeps none of the inheritable ones.
            foreach (var handle in duplicated) LauncherNative.CloseHandle(handle);
            foreach (var memory in allocations) Marshal.FreeHGlobal(memory);
        }
    }

    /// <summary>"NAME=value\0…\0\0", sorted by name the way Windows expects.</summary>
    internal static string EnvironmentBlock(IReadOnlyDictionary<string, string> environment)
    {
        var block = new StringBuilder();
        foreach (var (name, value) in environment.OrderBy(pair => pair.Key.ToUpperInvariant(), StringComparer.Ordinal))
        {
            if (name.Length == 0 || name.Contains('\0') || name.IndexOf('=', 1) >= 0 || value.Contains('\0'))
                throw new SetupException($"environment variable {name} cannot be passed on");
            block.Append(name).Append('=').Append(value).Append('\0');
        }
        if (block.Length == 0) block.Append('\0');
        block.Append('\0');
        return block.ToString();
    }

    static void Update(nint list, nint attribute, nint value, int size)
    {
        if (!LauncherNative.UpdateProcThreadAttribute(list, 0, attribute, value, size, 0, 0))
            throw new SetupException($"UpdateProcThreadAttribute(0x{attribute:X}) failed: {LastError()}");
    }

    static string LastError() => new Win32Exception(Marshal.GetLastWin32Error()).Message;
}
