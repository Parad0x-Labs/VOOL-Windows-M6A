using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;

namespace VoolConfine.Core;

/// <summary>The Win32 calls the launcher makes (x64 layouts; Directory.Build.props pins x64).</summary>
internal static class LauncherNative
{
    // CreateProcess flags.
    internal const uint CreateSuspended = 0x00000004;
    internal const uint CreateUnicodeEnvironment = 0x00000400;
    internal const uint ExtendedStartupInfoPresent = 0x00080000;
    internal const uint CreateBreakawayFromJob = 0x01000000;
    internal const uint StartfUseStdHandles = 0x00000100;

    // Process/thread attributes (ProcThreadAttributeValue(number, thread, input, additive)).
    internal const nint ProcThreadAttributeHandleList = 0x00020002;
    internal const nint ProcThreadAttributeSecurityCapabilities = 0x00020009;
    internal const nint ProcThreadAttributeJobList = 0x0002000D;

    // Job limit flags.
    internal const uint JobObjectLimitActiveProcess = 0x00000008;
    internal const uint JobObjectLimitJobMemory = 0x00000200;
    internal const uint JobObjectLimitDieOnUnhandledException = 0x00000400;
    internal const uint JobObjectLimitBreakawayOk = 0x00000800;
    internal const uint JobObjectLimitSilentBreakawayOk = 0x00001000;
    internal const uint JobObjectLimitKillOnJobClose = 0x00002000;
    internal const uint JobObjectUiLimitAll = 0x000000FF;

    // Job information classes.
    internal const int JobObjectBasicAccountingInformation = 1;
    internal const int JobObjectBasicUiRestrictions = 4;
    internal const int JobObjectAssociateCompletionPortInformation = 7;
    internal const int JobObjectExtendedLimitInformation = 9;

    // Job completion-port messages.
    internal const uint JobObjectMsgActiveProcessLimit = 3;
    internal const uint JobObjectMsgActiveProcessZero = 4;
    internal const uint JobObjectMsgProcessMemoryLimit = 9;
    internal const uint JobObjectMsgJobMemoryLimit = 10;

    internal const uint SeGroupEnabled = 0x00000004;
    internal const uint WaitObject0 = 0;
    internal const uint WaitTimeout = 0x102;
    internal const uint Infinite = 0xFFFFFFFF;
    internal const uint DuplicateSameAccess = 2;
    internal const int ErrorAlreadyExists = 183;
    internal const int HResultAlreadyExists = unchecked((int)0x800700B7);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    internal struct StartupInfo
    {
        public int Cb;
        public nint Reserved;
        public nint Desktop;
        public nint Title;
        public int X, Y, XSize, YSize, XCountChars, YCountChars, FillAttribute;
        public uint Flags;
        public short ShowWindow;
        public short Reserved2Count;
        public nint Reserved2;
        public nint StdInput;
        public nint StdOutput;
        public nint StdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct StartupInfoEx { public StartupInfo StartupInfo; public nint AttributeList; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct ProcessInformation { public nint Process; public nint Thread; public uint ProcessId; public uint ThreadId; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SidAndAttributes { public nint Sid; public uint Attributes; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct SecurityCapabilities { public nint AppContainerSid; public nint Capabilities; public uint CapabilityCount; public uint Reserved; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectBasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public nuint MinimumWorkingSetSize;
        public nuint MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public nuint Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct IoCounters
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectExtendedLimitInformation
    {
        public JobObjectBasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public nuint ProcessMemoryLimit;
        public nuint JobMemoryLimit;
        public nuint PeakProcessMemoryUsed;
        public nuint PeakJobMemoryUsed;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectBasicAccountingInformation
    {
        public long TotalUserTime, TotalKernelTime, ThisPeriodTotalUserTime, ThisPeriodTotalKernelTime;
        public uint TotalPageFaultCount, TotalProcesses, ActiveProcesses, TotalTerminatedProcesses;
    }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectBasicUiRestrictions { public uint UiRestrictionsClass; }

    [StructLayout(LayoutKind.Sequential)]
    internal struct JobObjectAssociateCompletionPort { public nint CompletionKey; public nint CompletionPort; }

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    internal static extern int CreateAppContainerProfile(string name, string displayName, string description,
        nint capabilities, uint capabilityCount, out nint sid);

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    internal static extern int DeriveAppContainerSidFromAppContainerName(string name, out nint sid);

    [DllImport("userenv.dll", CharSet = CharSet.Unicode)]
    internal static extern int GetAppContainerFolderPath(string sidString, out nint path);

    [DllImport("advapi32.dll")]
    internal static extern nint FreeSid(nint sid);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool ConvertSidToStringSidW(nint sid, out nint stringSid);

    [DllImport("advapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool ConvertStringSidToSidW(string stringSid, out nint sid);

    [DllImport("kernel32.dll")]
    internal static extern nint LocalFree(nint memory);

    [DllImport("ole32.dll")]
    internal static extern void CoTaskMemFree(nint memory);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    internal static extern SafeWaitHandle CreateJobObjectW(nint attributes, string? name);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool SetInformationJobObject(SafeWaitHandle job, int infoClass, nint info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool QueryInformationJobObject(SafeWaitHandle job, int infoClass, nint info, uint length, out uint returned);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool TerminateJobObject(SafeWaitHandle job, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsProcessInJob(nint process, SafeWaitHandle job, [MarshalAs(UnmanagedType.Bool)] out bool result);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern SafeWaitHandle CreateIoCompletionPort(nint file, nint existingPort, nuint key, uint threads);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetQueuedCompletionStatus(SafeWaitHandle port, out uint bytes, out nuint key, out nint overlapped, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool InitializeProcThreadAttributeList(nint list, int count, uint flags, ref nint size);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool UpdateProcThreadAttribute(nint list, uint flags, nint attribute, nint value, nint size, nint previous, nint returnSize);

    [DllImport("kernel32.dll")]
    internal static extern void DeleteProcThreadAttributeList(nint list);

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool CreateProcessW(string applicationName, System.Text.StringBuilder commandLine,
        nint processAttributes, nint threadAttributes, [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
        uint creationFlags, nint environment, string? currentDirectory, ref StartupInfoEx startupInfo,
        out ProcessInformation processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint ResumeThread(nint thread);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool TerminateProcess(nint process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern uint WaitForSingleObject(nint handle, uint milliseconds);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool GetExitCodeProcess(nint process, out uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool CloseHandle(nint handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    internal static extern nint GetStdHandle(int which);

    [DllImport("kernel32.dll")]
    internal static extern nint GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool DuplicateHandle(nint sourceProcess, nint source, nint targetProcess, out nint target,
        uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint options);

    // Exported by kernelbase.dll (api-ms-win-security-base-l1-2-2). Every returned SID and both
    // arrays are LocalAlloc'd and freed with LocalFree.
    [DllImport("kernelbase.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool DeriveCapabilitySidsFromName(string capabilityName, out nint capabilityGroupSids,
        out uint capabilityGroupSidCount, out nint capabilitySids, out uint capabilitySidCount);

    [DllImport("advapi32.dll")]
    internal static extern uint GetLengthSid(nint sid);

    [DllImport("advapi32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    internal static extern bool IsValidSid(nint sid);
}
