using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Vool.WindowsBroker;

internal sealed class WindowsJob : IDisposable
{
    private WindowsJob(SafeNativeHandle handle, JobSecuritySnapshot security)
    {
        Handle = handle;
        Security = security;
    }

    internal SafeNativeHandle Handle { get; }
    internal JobSecuritySnapshot Security { get; }

    internal static WindowsJob Create()
    {
        var rawHandle = NativeMethods.CreateJobObjectW(IntPtr.Zero, null);
        var handle = new SafeNativeHandle(rawHandle, ownsHandle: true);
        if (handle.IsInvalid)
        {
            handle.Dispose();
            throw NativeMethods.LastError("CreateJobObjectW");
        }

        try
        {
            var limits = new NativeMethods.JobObjectExtendedLimitInformation
            {
                BasicLimitInformation = new NativeMethods.JobObjectBasicLimitInformation
                {
                    PerProcessUserTimeLimit = BrokerLimits.PerProcessUserTimeLimit100Ns,
                    LimitFlags = NativeMethods.JobObjectLimitActiveProcess
                        | NativeMethods.JobObjectLimitProcessTime
                        | NativeMethods.JobObjectLimitProcessMemory
                        | NativeMethods.JobObjectLimitJobMemory
                        | NativeMethods.JobObjectLimitKillOnJobClose,
                    ActiveProcessLimit = BrokerLimits.ActiveProcessLimit,
                },
                ProcessMemoryLimit = new UIntPtr(BrokerLimits.ProcessMemoryLimitBytes),
                JobMemoryLimit = new UIntPtr(BrokerLimits.JobMemoryLimitBytes),
            };

            if (!NativeMethods.SetInformationJobObject(
                    handle,
                    NativeMethods.JobObjectExtendedLimitInformationClass,
                    ref limits,
                    (uint)Marshal.SizeOf<NativeMethods.JobObjectExtendedLimitInformation>()))
            {
                throw NativeMethods.LastError("SetInformationJobObject(extended limits)");
            }

            var cpu = new NativeMethods.JobObjectCpuRateControlInformation
            {
                ControlFlags = NativeMethods.JobObjectCpuRateControlEnable
                    | NativeMethods.JobObjectCpuRateControlHardCap,
                CpuRate = BrokerLimits.CpuRate,
            };
            if (!NativeMethods.SetInformationJobObject(
                    handle,
                    NativeMethods.JobObjectCpuRateControlInformationClass,
                    ref cpu,
                    (uint)Marshal.SizeOf<NativeMethods.JobObjectCpuRateControlInformation>()))
            {
                throw NativeMethods.LastError("SetInformationJobObject(CPU rate)");
            }

            return new WindowsJob(handle, QuerySecurity(handle));
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    internal static JobSecuritySnapshot QuerySecurity(SafeNativeHandle job)
    {
        NativeMethods.JobObjectExtendedLimitInformation limits;
        if (!NativeMethods.QueryInformationJobObject(
                job,
                NativeMethods.JobObjectExtendedLimitInformationClass,
                out limits,
                (uint)Marshal.SizeOf<NativeMethods.JobObjectExtendedLimitInformation>(),
                out _))
        {
            throw NativeMethods.LastError("QueryInformationJobObject(extended limits)");
        }

        NativeMethods.JobObjectCpuRateControlInformation cpu;
        if (!NativeMethods.QueryInformationJobObject(
                job,
                NativeMethods.JobObjectCpuRateControlInformationClass,
                out cpu,
                (uint)Marshal.SizeOf<NativeMethods.JobObjectCpuRateControlInformation>(),
                out _))
        {
            throw NativeMethods.LastError("QueryInformationJobObject(CPU rate)");
        }

        var security = new JobSecuritySnapshot(
            limits.BasicLimitInformation.LimitFlags,
            limits.BasicLimitInformation.ActiveProcessLimit,
            limits.ProcessMemoryLimit.ToUInt64(),
            limits.JobMemoryLimit.ToUInt64(),
            limits.BasicLimitInformation.PerProcessUserTimeLimit,
            cpu.ControlFlags,
            cpu.CpuRate);
        var requiredFlags = NativeMethods.JobObjectLimitActiveProcess
            | NativeMethods.JobObjectLimitProcessTime
            | NativeMethods.JobObjectLimitProcessMemory
            | NativeMethods.JobObjectLimitJobMemory
            | NativeMethods.JobObjectLimitKillOnJobClose;
        var requiredCpuFlags = NativeMethods.JobObjectCpuRateControlEnable
            | NativeMethods.JobObjectCpuRateControlHardCap;
        if ((security.LimitFlags & requiredFlags) != requiredFlags
            || security.ActiveProcessLimit != BrokerLimits.ActiveProcessLimit
            || security.ProcessMemoryLimitBytes != BrokerLimits.ProcessMemoryLimitBytes
            || security.JobMemoryLimitBytes != BrokerLimits.JobMemoryLimitBytes
            || security.PerProcessUserTimeLimit100Ns != BrokerLimits.PerProcessUserTimeLimit100Ns
            || (security.CpuControlFlags & requiredCpuFlags) != requiredCpuFlags
            || security.CpuRate != BrokerLimits.CpuRate)
        {
            throw new BrokerSecurityException("Job Object limits did not match the enforced policy.");
        }
        return security;
    }

    internal static void TerminateAndWait(SafeNativeHandle job)
    {
        if (!NativeMethods.TerminateJobObject(job, 0xC000013A))
            throw NativeMethods.LastError("TerminateJobObject");

        var watch = Stopwatch.StartNew();
        while (watch.Elapsed < TimeSpan.FromSeconds(5))
        {
            if (!NativeMethods.QueryInformationJobObject(
                    job,
                    NativeMethods.JobObjectBasicAccountingInformationClass,
                    out NativeMethods.JobObjectBasicAccountingInformation accounting,
                    (uint)Marshal.SizeOf<NativeMethods.JobObjectBasicAccountingInformation>(),
                    out _))
            {
                throw NativeMethods.LastError("QueryInformationJobObject(active processes)");
            }
            if (accounting.ActiveProcesses == 0)
                return;
            Thread.Sleep(20);
        }
        throw new BrokerSecurityException("Job processes did not exit before scratch cleanup.");
    }

    public void Dispose() => Handle.Dispose();
}
