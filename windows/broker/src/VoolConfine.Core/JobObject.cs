using Microsoft.Win32.SafeHandles;
using System.ComponentModel;
using System.Runtime.InteropServices;

namespace VoolConfine.Core;

/// <summary>What a job's limits read back as from the kernel.</summary>
internal readonly record struct JobLimits(uint LimitFlags, uint ActiveProcessLimit, ulong JobMemoryLimit, uint UiRestrictions);

/// <summary>
/// The Job Object a confined child runs in (docs/WINDOWS_CONFINEMENT.md section 2). It is unnamed, so
/// nothing can open it by name. Only this object holds a handle to it, and the handle is never made
/// inheritable or duplicated anywhere, so when the launcher ends for any reason the kernel closes
/// the handle and KILL_ON_JOB_CLOSE ends the child's whole tree.
/// </summary>
internal sealed class ConfinementJob : IDisposable
{
    /// <summary>The only limit flags the job may have. Breakaway (OK or SILENT) is never among them.</summary>
    internal const uint RequiredFlags = LauncherNative.JobObjectLimitKillOnJobClose
        | LauncherNative.JobObjectLimitDieOnUnhandledException
        | LauncherNative.JobObjectLimitActiveProcess
        | LauncherNative.JobObjectLimitJobMemory;

    readonly SafeWaitHandle job;
    readonly SafeWaitHandle port;
    readonly Thread watcher;
    readonly ManualResetEventSlim empty = new(false);
    readonly uint maxProcesses;
    readonly ulong memoryLimit;
    string? limitHit;

    internal SafeWaitHandle Handle => job;

    /// <summary>"memory" or "processes" once that job limit has stopped something; else null.</summary>
    internal string? LimitHit => Volatile.Read(ref limitHit);

    ConfinementJob(SafeWaitHandle job, SafeWaitHandle port, uint maxProcesses, ulong memoryLimit)
    {
        this.job = job;
        this.port = port;
        this.maxProcesses = maxProcesses;
        this.memoryLimit = memoryLimit;
        watcher = new Thread(Watch) { IsBackground = true, Name = "VoolConfine job watcher" };
    }

    /// <summary>Creates the job, sets every limit, then reads them back; any mismatch is a setup failure.</summary>
    internal static ConfinementJob Create(int maxProcesses, int memoryMb)
    {
        if (maxProcesses <= 0) throw new SetupException("max_processes must be positive");
        if (memoryMb <= 0) throw new SetupException("memory_mb must be positive");
        var memoryLimit = checked((ulong)memoryMb * 1024UL * 1024UL);
        var handle = LauncherNative.CreateJobObjectW(0, null);
        if (handle.IsInvalid) throw new SetupException($"CreateJobObject failed: {LastError()}");
        SafeWaitHandle? port = null;
        try
        {
            port = LauncherNative.CreateIoCompletionPort(new IntPtr(-1), 0, 0, 1);
            if (port.IsInvalid) throw new SetupException($"CreateIoCompletionPort failed: {LastError()}");
            var created = new ConfinementJob(handle, port, (uint)maxProcesses, memoryLimit);
            created.Configure();
            created.watcher.Start();
            return created;
        }
        catch
        {
            port?.Dispose();
            handle.Dispose();
            throw;
        }
    }

    void Configure()
    {
        // The completion port is attached before any process can be in the job, so no limit message is lost.
        Set(LauncherNative.JobObjectAssociateCompletionPortInformationClass, new LauncherNative.JobObjectAssociateCompletionPort
        {
            CompletionKey = 1,
            CompletionPort = port.DangerousGetHandle(),
        });
        var limits = new LauncherNative.JobObjectExtendedLimitInformation();
        limits.BasicLimitInformation.LimitFlags = RequiredFlags;
        limits.BasicLimitInformation.ActiveProcessLimit = maxProcesses;
        limits.JobMemoryLimit = (nuint)memoryLimit;
        Set(LauncherNative.JobObjectExtendedLimitInformationClass, limits);
        Set(LauncherNative.JobObjectBasicUiRestrictionsClass, new LauncherNative.JobObjectBasicUiRestrictions
        {
            UiRestrictionsClass = LauncherNative.JobObjectUiLimitAll,
        });

        // Trust what the kernel holds, not what was asked for.
        var actual = ReadBack();
        if (actual.LimitFlags != RequiredFlags)
            throw new SetupException($"job limit flags read back as 0x{actual.LimitFlags:X}, expected 0x{RequiredFlags:X}");
        if (actual.ActiveProcessLimit != maxProcesses)
            throw new SetupException($"job process limit read back as {actual.ActiveProcessLimit}, expected {maxProcesses}");
        if (actual.JobMemoryLimit != memoryLimit)
            throw new SetupException($"job memory limit read back as {actual.JobMemoryLimit}, expected {memoryLimit}");
        if (actual.UiRestrictions != LauncherNative.JobObjectUiLimitAll)
            throw new SetupException($"job UI restrictions read back as 0x{actual.UiRestrictions:X}");
    }

    internal JobLimits ReadBack()
    {
        var limits = Query<LauncherNative.JobObjectExtendedLimitInformation>(LauncherNative.JobObjectExtendedLimitInformationClass);
        var ui = Query<LauncherNative.JobObjectBasicUiRestrictions>(LauncherNative.JobObjectBasicUiRestrictionsClass);
        return new JobLimits(limits.BasicLimitInformation.LimitFlags, limits.BasicLimitInformation.ActiveProcessLimit,
            limits.JobMemoryLimit, ui.UiRestrictionsClass);
    }

    internal uint ActiveProcesses() =>
        Query<LauncherNative.JobObjectBasicAccountingInformation>(LauncherNative.JobObjectBasicAccountingInformationClass).ActiveProcesses;

    /// <summary>Ends every process in the job and waits until the kernel reports none left.</summary>
    internal bool KillAll(uint exitCode, TimeSpan wait)
    {
        if (!LauncherNative.TerminateJobObject(job, exitCode))
            throw new SetupException($"TerminateJobObject failed: {LastError()}");
        var deadline = DateTime.UtcNow + wait;
        while (DateTime.UtcNow < deadline)
        {
            if (ActiveProcesses() == 0) return true;
            empty.Wait(TimeSpan.FromMilliseconds(50));
        }
        return ActiveProcesses() == 0;
    }

    void Watch()
    {
        while (LauncherNative.GetQueuedCompletionStatus(port, out var message, out _, out _, LauncherNative.Infinite))
        {
            switch (message)
            {
                case LauncherNative.JobObjectMsgJobMemoryLimit:
                case LauncherNative.JobObjectMsgProcessMemoryLimit:
                    Interlocked.CompareExchange(ref limitHit, "memory", null);
                    break;
                case LauncherNative.JobObjectMsgActiveProcessLimit:
                    Interlocked.CompareExchange(ref limitHit, "processes", null);
                    break;
                case LauncherNative.JobObjectMsgActiveProcessZero:
                    empty.Set();
                    break;
            }
        }
        // The port was closed (Dispose): the loop ends with ERROR_ABANDONED_WAIT_0.
    }

    /// <summary>Lets the watcher take any limit message already queued before the status is written.</summary>
    internal void Drain(TimeSpan wait) => empty.Wait(wait);

    void Set<T>(int infoClass, T value) where T : struct
    {
        var size = Marshal.SizeOf<T>();
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(value, memory, false);
            if (!LauncherNative.SetInformationJobObject(job, infoClass, memory, (uint)size))
                throw new SetupException($"SetInformationJobObject({infoClass}) failed: {LastError()}");
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    T Query<T>(int infoClass) where T : struct
    {
        var size = Marshal.SizeOf<T>();
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            if (!LauncherNative.QueryInformationJobObject(job, infoClass, memory, (uint)size, out _))
                throw new SetupException($"QueryInformationJobObject({infoClass}) failed: {LastError()}");
            return Marshal.PtrToStructure<T>(memory);
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    static string LastError() => new Win32Exception(Marshal.GetLastWin32Error()).Message;

    public void Dispose()
    {
        // Closing the job handle is what kills the tree (KILL_ON_JOB_CLOSE); then the port, which ends the watcher.
        job.Dispose();
        port.Dispose();
        if (!watcher.IsAlive || watcher.Join(TimeSpan.FromSeconds(2))) empty.Dispose();
    }
}
