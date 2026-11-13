using System.ComponentModel;
using System.Runtime.InteropServices;

namespace VoolConfine.Core;

/// <summary>
/// The AppContainer identity a job runs as (docs/WINDOWS_CONFINEMENT.md section 2, "Identity"): the
/// one fixed profile <see cref="Identity.ProfileName"/>, and one capability SID per write root. The
/// ACL chunk grants to exactly these SIDs, so both sides resolve them through this class.
/// </summary>
public static class Sids
{
    /// <summary>The well-known internetClient capability (S-1-15-3-1).</summary>
    public const string InternetClientSid = "S-1-15-3-1";

    /// <summary>Creates the profile if it does not exist yet and returns its package SID (S-1-15-2-…).</summary>
    public static string EnsureProfile()
    {
        var hr = LauncherNative.CreateAppContainerProfile(Identity.ProfileName, "VOOL confined job",
            "Runs VOOL jobs confined (docs/WINDOWS_CONFINEMENT.md)", 0, 0, out var sid);
        if (hr == LauncherNative.HResultAlreadyExists)
        {
            hr = LauncherNative.DeriveAppContainerSidFromAppContainerName(Identity.ProfileName, out sid);
            if (hr < 0) throw new SetupException($"DeriveAppContainerSidFromAppContainerName failed (0x{hr:X8})");
        }
        else if (hr < 0)
        {
            throw new SetupException($"CreateAppContainerProfile failed (0x{hr:X8})");
        }
        try
        {
            var text = SidToString(sid);
            if (!text.StartsWith("S-1-15-2-", StringComparison.Ordinal))
                throw new SetupException($"the profile SID is not an AppContainer package SID: {text}");
            return text;
        }
        finally { LauncherNative.FreeSid(sid); }
    }

    /// <summary>The package SID of the profile, derived from its name without creating anything.</summary>
    public static string Package()
    {
        var hr = LauncherNative.DeriveAppContainerSidFromAppContainerName(Identity.ProfileName, out var sid);
        if (hr < 0) throw new SetupException($"DeriveAppContainerSidFromAppContainerName failed (0x{hr:X8})");
        try { return SidToString(sid); }
        finally { LauncherNative.FreeSid(sid); }
    }

    /// <summary>The capability SID (S-1-15-3-1024-…) Windows derives for a capability name.</summary>
    public static string CapabilitySid(string capabilityName)
    {
        if (!LauncherNative.DeriveCapabilitySidsFromName(capabilityName, out var groupSids, out var groupCount,
                out var capabilitySids, out var capabilityCount))
            throw new SetupException($"DeriveCapabilitySidsFromName failed: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");
        try
        {
            if (capabilityCount != 1)
                throw new SetupException($"DeriveCapabilitySidsFromName returned {capabilityCount} capability SIDs, expected 1");
            var text = SidToString(Marshal.ReadIntPtr(capabilitySids));
            if (!text.StartsWith("S-1-15-3-", StringComparison.Ordinal))
                throw new SetupException($"the capability SID is not a capability SID: {text}");
            return text;
        }
        finally
        {
            FreeSidArray(groupSids, groupCount);
            FreeSidArray(capabilitySids, capabilityCount);
        }
    }

    /// <summary>The capability SID a write root's grants go to.</summary>
    public static string RootCapability(string root) => CapabilitySid(Identity.RootCapabilityName(root));

    /// <summary>The capability SID a job read root's read grant goes to.</summary>
    public static string ReadCapability(string root) => CapabilitySid(Identity.ReadCapabilityName(root));

    /// <summary>The capability SID a stat-only path's ACE goes to.</summary>
    public static string StatCapability(string path) => CapabilitySid(Identity.StatCapabilityName(path));

    /// <summary>
    /// The capabilities a job's token carries: only this job's write roots and job read roots, plus
    /// internetClient when the policy grants network. Nothing else, so the job can never use another
    /// job's grants (shared read roots go to the package SID, which every job holds).
    /// </summary>
    public static IReadOnlyList<string> CapabilitiesFor(Policy policy)
    {
        var sids = new List<string>();
        foreach (var name in policy.WriteRoots.Select(Identity.RootCapabilityName).Distinct(StringComparer.Ordinal))
            sids.Add(CapabilitySid(name));
        foreach (var name in policy.JobReadRoots.Select(Identity.ReadCapabilityName).Distinct(StringComparer.Ordinal))
            sids.Add(CapabilitySid(name));
        foreach (var name in policy.StatOnlyPaths.Select(Identity.StatCapabilityName).Distinct(StringComparer.Ordinal))
            sids.Add(CapabilitySid(name));
        if (policy.Network) sids.Add(InternetClientSid);
        return sids;
    }

    /// <summary>The profile's own folder (%LOCALAPPDATA%\Packages\&lt;name&gt;\AC).</summary>
    public static string FolderPath(string packageSid)
    {
        var hr = LauncherNative.GetAppContainerFolderPath(packageSid, out var path);
        if (hr < 0) throw new SetupException($"GetAppContainerFolderPath failed (0x{hr:X8})");
        try { return Marshal.PtrToStringUni(path) ?? throw new SetupException("GetAppContainerFolderPath returned no path"); }
        finally { LauncherNative.CoTaskMemFree(path); }
    }

    internal static string SidToString(nint sid)
    {
        if (sid == 0 || !LauncherNative.IsValidSid(sid)) throw new SetupException("invalid SID");
        if (!LauncherNative.ConvertSidToStringSidW(sid, out var text))
            throw new SetupException($"ConvertSidToStringSid failed: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");
        try { return Marshal.PtrToStringUni(text) ?? throw new SetupException("ConvertSidToStringSid returned nothing"); }
        finally { LauncherNative.LocalFree(text); }
    }

    /// <summary>A SID string as the binary SID, for SECURITY_CAPABILITIES.</summary>
    internal static byte[] SidBytes(string sid)
    {
        if (!LauncherNative.ConvertStringSidToSidW(sid, out var native))
            throw new SetupException($"ConvertStringSidToSid failed for {sid}: {new Win32Exception(Marshal.GetLastWin32Error()).Message}");
        try
        {
            var bytes = new byte[LauncherNative.GetLengthSid(native)];
            Marshal.Copy(native, bytes, 0, bytes.Length);
            return bytes;
        }
        finally { LauncherNative.LocalFree(native); }
    }

    static void FreeSidArray(nint array, uint count)
    {
        if (array == 0) return;
        for (var i = 0; i < count; i++)
        {
            var sid = Marshal.ReadIntPtr(array, i * IntPtr.Size);
            if (sid != 0) LauncherNative.LocalFree(sid);
        }
        LauncherNative.LocalFree(array);
    }
}
