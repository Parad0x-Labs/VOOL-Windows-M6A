r"""Chunk 7 probe: run git / node / npm / pip / cargo inside a hand-made AppContainer on Steve.

Same shape as docs/WINDOWS_CONFINEMENT.md (chunk 0, 977692da/d5f85973), with its own names so nothing
here ever grants anything to the real profile:
  - AppContainer SID derived from the name "VOOL.Confine.Probe7" (NOT VOOL.Confine.v1). No profile is
    registered unless --create-profile is given (that writes HKCU, so it needs Jesse's OK).
  - write grant: (OI)(CI)M to a per-root capability SID derived from "vool.probe7.root.<sha256>".
  - read grants: (OI)(CI)RX to the package SID, ONLY on folders under --base (the VOOL Windows folder)
    or listed with --allow-grant. Anything else that a tool needs is reported as "needs grant", not done.
  - deny: write+delete on the probe repo's .git, to the package SID.
  - network: none, or internetClient (S-1-15-3-1) for the "net" variants. Loopback is never granted.
  - Job Object: KILL_ON_JOB_CLOSE, no breakaway, generous caps (8192 MB, 512 processes) so the probe
    MEASURES peak memory and process count rather than tripping them. Child created suspended,
    assigned, then resumed.
Every ACE it adds is removed in `finally` (icacls /remove of the two probe SIDs), and the probe
workspace is left for inspection. It installs nothing, never reads a credential file's contents
(it only reports whether open() succeeded), and runs nothing that could print a secret.

Run unconfined, from a Python on Steve:
  python confine7_probe.py --base "C:\Users\Satoshi\Documents\VOOL Windows" ^
      --root "C:\Users\Satoshi\Documents\VOOL Windows\missions\confine7-probe" ^
      --python "<abs path to a python.exe>"  [--allow-grant <dir> ...] [--create-profile]
Results: <root>\out\results.jsonl and <root>\out\SUMMARY.md
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import msvcrt
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes as wt
from pathlib import Path

PROFILE = "VOOL.Confine.Probe7"
CAP_PREFIX = "vool.probe7.root."
INTERNET_CLIENT = "S-1-15-3-1"

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
kbase = ctypes.WinDLL("kernelbase", use_last_error=True)
adv = ctypes.WinDLL("advapi32", use_last_error=True)
uenv = ctypes.WinDLL("userenv", use_last_error=True)

PSID = ctypes.c_void_p


class SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", PSID), ("Attributes", wt.DWORD)]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [("AppContainerSid", PSID), ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                ("CapabilityCount", wt.DWORD), ("Reserved", wt.DWORD)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR), ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD), ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD), ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD), ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE), ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RB", "WB", "OB")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", wt.DWORD), ("TotalProcesses", wt.DWORD),
                ("ActiveProcesses", wt.DWORD), ("TotalTerminatedProcesses", wt.DWORD)]


def _sigs() -> None:
    uenv.DeriveAppContainerSidFromAppContainerName.argtypes = [wt.LPCWSTR, ctypes.POINTER(PSID)]
    uenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    uenv.CreateAppContainerProfile.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p, wt.DWORD,
                                               ctypes.POINTER(PSID)]
    uenv.CreateAppContainerProfile.restype = ctypes.c_long
    uenv.DeleteAppContainerProfile.argtypes = [wt.LPCWSTR]
    uenv.DeleteAppContainerProfile.restype = ctypes.c_long
    kbase.DeriveCapabilitySidsFromName.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.POINTER(PSID)),
                                                   ctypes.POINTER(wt.DWORD), ctypes.POINTER(ctypes.POINTER(PSID)),
                                                   ctypes.POINTER(wt.DWORD)]
    kbase.DeriveCapabilitySidsFromName.restype = wt.BOOL
    adv.ConvertSidToStringSidW.argtypes = [PSID, ctypes.POINTER(wt.LPWSTR)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL
    adv.ConvertStringSidToSidW.argtypes = [wt.LPCWSTR, ctypes.POINTER(PSID)]
    adv.ConvertStringSidToSidW.restype = wt.BOOL
    k32.LocalFree.argtypes = [ctypes.c_void_p]
    k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wt.DWORD, wt.DWORD, ctypes.POINTER(ctypes.c_size_t)]
    k32.InitializeProcThreadAttributeList.restype = wt.BOOL
    k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wt.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                              ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    k32.UpdateProcThreadAttribute.restype = wt.BOOL
    k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    k32.CreateProcessW.argtypes = [wt.LPCWSTR, wt.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wt.BOOL, wt.DWORD,
                                   ctypes.c_void_p, wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(PROCESS_INFORMATION)]
    k32.CreateProcessW.restype = wt.BOOL
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k32.CreateJobObjectW.restype = wt.HANDLE
    k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k32.SetInformationJobObject.restype = wt.BOOL
    k32.QueryInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD, ctypes.c_void_p]
    k32.QueryInformationJobObject.restype = wt.BOOL
    k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k32.AssignProcessToJobObject.restype = wt.BOOL
    k32.ResumeThread.argtypes = [wt.HANDLE]
    k32.ResumeThread.restype = wt.DWORD
    k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
    k32.TerminateJobObject.restype = wt.BOOL
    k32.TerminateProcess.argtypes = [wt.HANDLE, wt.UINT]
    k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
    k32.WaitForSingleObject.restype = wt.DWORD
    k32.GetExitCodeProcess.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
    k32.GetExitCodeProcess.restype = wt.BOOL
    k32.CloseHandle.argtypes = [wt.HANDLE]


def sid_str(sid: int) -> str:
    out = wt.LPWSTR()
    if not adv.ConvertSidToStringSidW(sid, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return out.value
    finally:
        k32.LocalFree(out)


def package_sid() -> int:
    sid = PSID()
    hr = uenv.DeriveAppContainerSidFromAppContainerName(PROFILE, ctypes.byref(sid))
    if hr != 0:
        raise OSError(f"DeriveAppContainerSidFromAppContainerName hr=0x{hr & 0xFFFFFFFF:08x}")
    return sid.value


def capability_sid(name: str) -> int:
    groups, ngroups = ctypes.POINTER(PSID)(), wt.DWORD()
    caps, ncaps = ctypes.POINTER(PSID)(), wt.DWORD()
    if not kbase.DeriveCapabilitySidsFromName(name, ctypes.byref(groups), ctypes.byref(ngroups),
                                              ctypes.byref(caps), ctypes.byref(ncaps)):
        raise ctypes.WinError(ctypes.get_last_error())
    if ncaps.value < 1:
        raise OSError("DeriveCapabilitySidsFromName returned no capability SID")
    return caps[0]  # freed at process exit; a handful of SIDs per run


def string_sid(text: str) -> int:
    sid = PSID()
    if not adv.ConvertStringSidToSidW(text, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    return sid.value


def root_capability_name(root: str) -> str:
    # Same normalisation as sandbox.windows_confinement.root_capability_name, different prefix.
    trimmed = str(root).replace("/", "\\").rstrip("\\")
    normalized = "".join(chr(ord(c) - 32) if "a" <= c <= "z" else c for c in trimmed)
    return CAP_PREFIX + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


ICACLS = str(Path(os.environ["SystemRoot"]) / "System32" / "icacls.exe")


def icacls_full(*args: str) -> tuple[int, str]:
    p = subprocess.run([ICACLS, *args], capture_output=True, text=True, errors="replace")
    return p.returncode, (p.stdout + p.stderr)


def icacls(*args: str) -> tuple[int, str]:
    """For the log only: the last 600 characters. Never used to decide anything was revoked."""
    rc, text = icacls_full(*args)
    return rc, text.strip()[-600:]


def env_block(env: dict[str, str]) -> ctypes.Array:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    text = "".join(f"{k}={v}\0" for k, v in items) + "\0"
    return ctypes.create_unicode_buffer(text, len(text))


def inheritable_handle(f) -> int:
    h = msvcrt.get_osfhandle(f.fileno())
    os.set_handle_inheritable(h, True)
    return h


def run_confined(argv: list[str], *, cwd: Path, env: dict[str, str], pkg: int | None, caps: list[int],
                 out_dir: Path, tag: str, timeout: float) -> dict:
    rec: dict = {"tag": tag, "argv": argv, "cwd": str(cwd)}
    attrs = (SID_AND_ATTRIBUTES * max(1, len(caps)))()
    for i, c in enumerate(caps):
        attrs[i].Sid, attrs[i].Attributes = c, 0x4  # SE_GROUP_ENABLED
    sc = SECURITY_CAPABILITIES(pkg, ctypes.cast(attrs, ctypes.POINTER(SID_AND_ATTRIBUTES)) if caps else None, len(caps), 0)
    stdout_p, stderr_p = out_dir / f"{tag}.out.txt", out_dir / f"{tag}.err.txt"
    with open(os.devnull, "rb") as fin, open(stdout_p, "wb") as fout, open(stderr_p, "wb") as ferr:
        handles = (wt.HANDLE * 3)(inheritable_handle(fin), inheritable_handle(fout), inheritable_handle(ferr))
        size = ctypes.c_size_t()
        # pkg=None is the CONTROL launch: the same code path with no AppContainer attribute, so a
        # failure that also happens here is the launcher code, not the container.
        nattr = 2 if pkg is not None else 1
        k32.InitializeProcThreadAttributeList(None, nattr, 0, ctypes.byref(size))
        alist = ctypes.create_string_buffer(size.value)
        if not k32.InitializeProcThreadAttributeList(alist, nattr, 0, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if pkg is not None and not k32.UpdateProcThreadAttribute(alist, 0, 0x00020009, ctypes.byref(sc), ctypes.sizeof(sc), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not k32.UpdateProcThreadAttribute(alist, 0, 0x00020002, handles, ctypes.sizeof(handles), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            si.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = handles[0], handles[1], handles[2]
            si.lpAttributeList = ctypes.cast(alist, ctypes.c_void_p)
            pi = PROCESS_INFORMATION()
            cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            flags = 0x00080000 | 0x00000004 | 0x00000400 | 0x08000000  # EXTENDED|SUSPENDED|UNICODE_ENV|NO_WINDOW
            job = k32.CreateJobObjectW(None, None)
            lim = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            lim.BasicLimitInformation.LimitFlags = 0x2000 | 0x8 | 0x200  # KILL_ON_CLOSE|ACTIVE_PROCESS|JOB_MEMORY
            lim.BasicLimitInformation.ActiveProcessLimit = 512
            lim.JobMemoryLimit = 8192 * 1024 * 1024
            if not job or not k32.SetInformationJobObject(job, 9, ctypes.byref(lim), ctypes.sizeof(lim)):
                raise ctypes.WinError(ctypes.get_last_error())
            started = time.monotonic()
            envbuf = env_block(env)  # kept alive for the call
            ok = k32.CreateProcessW(None, cmdline, None, None, True, flags, ctypes.cast(envbuf, ctypes.c_void_p),
                                    str(cwd), ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                rec.update(child_started=False, error=f"CreateProcessW winerror={ctypes.get_last_error()}")
                k32.CloseHandle(job)
                return rec
            try:
                if not k32.AssignProcessToJobObject(job, pi.hProcess):
                    err = ctypes.get_last_error()
                    k32.TerminateProcess(pi.hProcess, 125)
                    rec.update(child_started=False, error=f"AssignProcessToJobObject winerror={err}")
                    return rec
                k32.ResumeThread(pi.hThread)
                rec["child_started"] = True
                waited = k32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
                rec["timed_out"] = waited == 0x102
                if rec["timed_out"]:
                    k32.TerminateJobObject(job, 124)
                    k32.WaitForSingleObject(pi.hProcess, 5000)
                code = wt.DWORD()
                k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code))
                rec["exit_code"] = code.value
                rec["exit_hex"] = f"0x{code.value:08x}"
                rec["seconds"] = round(time.monotonic() - started, 2)
                acct = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
                ext = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
                if k32.QueryInformationJobObject(job, 1, ctypes.byref(acct), ctypes.sizeof(acct), None):
                    rec["total_processes"] = acct.TotalProcesses
                    rec["still_active_at_exit"] = acct.ActiveProcesses
                if k32.QueryInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext), None):
                    rec["peak_job_mb"] = round(ext.PeakJobMemoryUsed / 1048576, 1)
            finally:
                k32.TerminateJobObject(job, 0)  # nothing outlives the probe
                for h in (pi.hThread, pi.hProcess, job):
                    k32.CloseHandle(h)
        finally:
            k32.DeleteProcThreadAttributeList(alist)
    rec["stdout_tail"] = stdout_p.read_bytes()[-1500:].decode("utf-8", "replace")
    rec["stderr_tail"] = stderr_p.read_bytes()[-1500:].decode("utf-8", "replace")
    return rec


# ---- environments: what JobRunner sends today vs what the matrix proposes (X1, X2, X11) ----

def env_current(ws: Path) -> dict[str, str]:
    """Mirror of JobRunner._child_environment at b5d530c9 (sandbox/job_runner.py:522-565)."""
    src = os.environ
    env = {k: src[k] for k in ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SHELL", "USER", "LOGNAME")
           if str(src.get(k) or "").strip()}
    home, tmp = ws / ".sandbox-home", ws / ".sandbox-tmp"
    for p in (home, tmp, home / ".cache", home / ".config", home / ".local" / "share"):
        p.mkdir(parents=True, exist_ok=True)
    env.update(HOME=str(home), TMPDIR=str(tmp), TEMP=str(tmp), TMP=str(tmp), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_CONFIG_HOME=str(home / ".config"), XDG_DATA_HOME=str(home / ".local" / "share"),
               VOOL_EXECUTION_BACKEND="subprocess", NO_PROXY="*", no_proxy="*", PYTHONDONTWRITEBYTECODE="1")
    return env


def env_proposed(ws: Path) -> dict[str, str]:
    env = env_current(ws)
    for k in ("SystemRoot", "windir", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    home = Path(env["HOME"])
    roaming, local = home / "AppData" / "Roaming", home / "AppData" / "Local"
    roaming.mkdir(parents=True, exist_ok=True)
    local.mkdir(parents=True, exist_ok=True)
    env.update(USERPROFILE=str(home), APPDATA=str(roaming), LOCALAPPDATA=str(local),
               HOMEDRIVE=str(home)[:2], HOMEPATH=str(home)[2:], GIT_CONFIG_NOSYSTEM="1",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", CARGO_HOME=str(home / ".cargo"))
    return env


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="grants are only ever made under this folder")
    ap.add_argument("--root", required=True, help="probe workspace parent (under --base)")
    ap.add_argument("--python", required=True)
    ap.add_argument("--allow-grant", action="append", default=[], help="extra read-grant folder (Jesse OK)")
    ap.add_argument("--create-profile", action="store_true", help="register the AppContainer profile (HKCU)")
    ap.add_argument("--timeout", type=float, default=20.0, help="per probe, seconds")
    ap.add_argument("--budget", type=float, default=480.0,
                    help="all probes together, seconds; later probes are skipped so revoke always runs well "
                         "inside an outer 20-minute kill")
    ap.add_argument("--revoke-only", action="store_true",
                    help="remove every ACE listed in <root>\\out\\grants-journal.json (after a killed run)")
    a = ap.parse_args()
    if a.timeout > 60 or a.budget > 600:
        print("--timeout is capped at 60 and --budget at 600 so the revoke in `finally` always runs"); return 2
    _sigs()
    base, root = Path(a.base).resolve(), Path(a.root).resolve()
    if base not in root.parents:
        print("--root must be inside --base"); return 2
    ws, out = root / "ws", root / "out"
    ws.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    results = out / "results.jsonl"
    journal = out / "grants-journal.json"
    if a.revoke_only:
        return _revoke_from_journal(journal, ws, [base, *(Path(x).resolve() for x in a.allow_grant)])
    log = open(results, "a", encoding="utf-8")

    def emit(rec: dict) -> None:
        log.write(json.dumps(rec) + "\n"); log.flush()
        print(f"{rec.get('tag')}: started={rec.get('child_started')} code={rec.get('exit_hex', rec.get('error'))}")

    emit({"tag": "P0-host", "windows": platform.version(), "release": platform.release(),
          "probe_python": sys.executable, "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})

    # ---- P0 inventory (unconfined) ----
    tools: dict[str, str | None] = {n: shutil.which(n) for n in ("git", "node", "npm", "cargo", "rustup", "rustc")}
    tools["python"] = str(Path(a.python).resolve())
    inv: dict = {"tag": "P0-inventory", "tools": tools}
    for n, cmd in (("git", ["--version"]), ("node", ["-p", "process.versions.node+' uv '+process.versions.uv"]),
                   ("cargo", ["-V"]), ("python", ["-VV"])):
        if tools.get(n):
            try:
                inv[f"{n}_version"] = subprocess.run([tools[n], *cmd], capture_output=True, text=True, timeout=30).stdout.strip()
            except Exception as e:  # report, don't guess
                inv[f"{n}_version"] = f"error: {e!r}"
    py_info = subprocess.run([tools["python"], "-c", "import sys,json;print(json.dumps([sys.prefix,sys.base_prefix,sys.executable]))"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    inv["python_prefixes"] = json.loads(py_info) if py_info else None
    emit(inv)

    def install_dir(exe: str | None) -> Path | None:
        if not exe:
            return None
        p = Path(exe).resolve().parent
        if p.name.lower() in {"cmd", "bin"} and p.parent.name.lower() in {"git", "mingw64"}:
            p = p.parent  # Git for Windows: grant the Git folder, not just cmd\
        return p

    # The toolchain cargo.exe, resolved through rustup OUTSIDE the container. Its folder
    # (~\.rustup\toolchains\<toolchain>) is the only Rust folder ever granted: the proxies in
    # ~\.cargo\bin are never used, and ~\.cargo (it holds credentials.toml) is never granted.
    cargo = None
    if tools.get("rustup"):
        try:
            r = subprocess.run([tools["rustup"], "which", "cargo"], capture_output=True, text=True, timeout=30)
            cargo = str(Path(r.stdout.strip()).resolve()) if r.returncode == 0 and r.stdout.strip() else None
        except Exception as e:
            inv["rustup_which_error"] = repr(e)
    real_profile = Path(os.environ["USERPROFILE"]).resolve()
    toolchains = real_profile / ".rustup" / "toolchains"
    emit({"tag": "P0-rust", "toolchain_cargo": cargo, "rustup_which_error": inv.get("rustup_which_error"),
          "under_default_toolchains": bool(cargo and toolchains in Path(cargo).parents)})

    wanted: dict[str, Path] = {}
    for n in ("git", "node"):
        d = install_dir(tools.get(n))
        if d:
            wanted[n] = d
    if cargo and toolchains in Path(cargo).parents:
        wanted["rust-toolchain"] = toolchains / Path(cargo).relative_to(toolchains).parts[0]
    for i, pfx in enumerate((inv.get("python_prefixes") or [])[:2]):
        wanted[f"python{i}"] = Path(pfx).resolve()
    allowed_extra = [Path(p).resolve() for p in a.allow_grant]

    # Never granted, whatever --allow-grant says: credential homes, and any folder that CONTAINS
    # the profile or its AppData roots (a grant there would cover everything below).
    never_under = [real_profile / n for n in (".cargo", ".ssh", ".aws", ".gnupg", ".config", ".docker",
                                              ".vool_runtime", ".git-credentials", ".npmrc", ".pypirc", ".netrc")]
    for k in ("APPDATA",):
        if os.environ.get(k):
            never_under.append(Path(os.environ[k]).resolve())
    never_above = [real_profile] + [Path(os.environ[k]).resolve() for k in ("APPDATA", "LOCALAPPDATA") if os.environ.get(k)]

    def grantable(p: Path) -> bool:
        if any(x == p or x in p.parents for x in never_under):
            return False
        if any(p == x or p in x.parents for x in never_above):
            return False
        return base == p or base in p.parents or any(x == p or x in p.parents for x in allowed_extra)

    pkg = package_sid()
    pkg_s = sid_str(pkg)
    cap_name = root_capability_name(str(ws))
    cap = capability_sid(cap_name)
    cap_s = sid_str(cap)
    net = string_sid(INTERNET_CLIENT)
    emit({"tag": "P0-identity", "package_sid": pkg_s, "root_capability": cap_name, "root_capability_sid": cap_s})

    profile_made = False
    granted: list[str] = []

    def write_journal() -> None:
        journal.write_text(json.dumps({"package_sid": pkg_s, "capability_sid": cap_s, "paths": granted,
                                       "profile_name": PROFILE, "profile_created_by_probe": profile_made}),
                           encoding="utf-8")

    def record(path: str) -> None:
        # Journal BEFORE icacls runs, so a killed run can still be revoked (--revoke-only).
        granted.append(path)
        write_journal()

    try:
        if a.create_profile:
            before = profile_traces(pkg_s)
            if any(before.values()):
                # Something of a Probe7 profile already exists: reuse it and leave it exactly as found.
                emit({"tag": "P0-profile", "created_by_this_run": False, "already_existed": True, "traces": before})
            else:
                # Journal the intent BEFORE creating, so a kill at any point leaves a record that
                # --revoke-only acts on (deleting a profile that was never made is harmless).
                profile_made = True
                write_journal()
                sid = PSID()
                hr = uenv.CreateAppContainerProfile(PROFILE, PROFILE, "VOOL chunk 7 probe", None, 0, ctypes.byref(sid))
                hr &= 0xFFFFFFFF
                emit({"tag": "P0-profile", "hr": f"0x{hr:08x}", "created_by_this_run": hr == 0,
                      "traces_after_create": profile_traces(pkg_s)})
                if hr != 0 and not any(profile_traces(pkg_s).values()):
                    profile_made = False
                    write_journal()

        # ---- fixtures, made unconfined ----
        repo = ws / "repo"
        if not (repo / ".git").exists() and tools.get("git"):
            repo.mkdir(exist_ok=True)
            g = [tools["git"], "-c", "core.hooksPath=NUL", "-c", "user.name=probe", "-c", "user.email=probe@invalid"]
            subprocess.run([*g, "init", "-q"], cwd=repo, check=False)
            (repo / "a.txt").write_text("hello\n")
            subprocess.run([*g, "add", "a.txt"], cwd=repo, check=False)
            subprocess.run([*g, "commit", "-q", "-m", "init"], cwd=repo, check=False)
            (repo / "a.txt").write_text("hello\nchanged\n")
        (ws / "pkg").mkdir(exist_ok=True)
        (ws / "pkg" / "package.json").write_text(json.dumps({"name": "p", "version": "1.0.0", "scripts": {
            "hello": "node -e \"console.log('hello-from-script')\""}}))
        (ws / "pkg" / "x.js").write_text("module.exports = 1;\n")
        crate = ws / "crate"
        (crate / "src").mkdir(parents=True, exist_ok=True)
        (crate / "Cargo.toml").write_text('[package]\nname = "p7"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n')
        (crate / "src" / "main.rs").write_text('fn main() { println!("hi"); }\n')
        # D: four copies of the repo, one per deny shape (v9; v7 found the package-SID deny did NOT
        # stop `git commit`). E: two folders inside the write root, one per deny-all shape.
        dshapes = {"D1-deny-package": "pkg", "D2-deny-capability": "cap", "D3-deny-both": "both",
                   "D4-no-inheritance-no-capability": "noinherit"}
        drepos: dict[str, Path] = {}
        if tools.get("git"):
            g = [tools["git"], "-c", "core.hooksPath=NUL", "-c", "user.name=probe", "-c", "user.email=probe@invalid"]
            for tag in dshapes:
                d = ws / tag.lower()
                if not (d / ".git").exists():
                    d.mkdir(exist_ok=True)
                    subprocess.run([*g, "init", "-q"], cwd=d, check=False)
                    (d / "a.txt").write_text("hello\n")
                    subprocess.run([*g, "add", "a.txt"], cwd=d, check=False)
                    subprocess.run([*g, "commit", "-q", "-m", "init"], cwd=d, check=False)
                (d / "a.txt").write_text("hello\nchanged\n")
                drepos[tag] = d
        eshapes = {"E1-denyall-package": "pkg", "E2-denyall-capability": "cap", "E3-denyall-both": "both"}
        edirs: dict[str, Path] = {}
        for tag in eshapes:
            d = ws / tag.lower()
            d.mkdir(exist_ok=True)
            (d / "fixture.txt").write_text("probe-fixture-not-a-secret\n")
            edirs[tag] = d
        # A fixture "credential" OUTSIDE every grant (beside ws, never granted): P8b must not open it.
        fixture_secret = root / "fixture-outside" / "fake.npmrc"
        fixture_secret.parent.mkdir(exist_ok=True)
        fixture_secret.write_text("probe-fixture-not-a-secret\n")

        # ---- grants ----
        record(str(ws))
        rc, msg = icacls(str(ws), "/grant", f"*{cap_s}:(OI)(CI)M")
        emit({"tag": "G-write-root", "path": str(ws), "rc": rc, "out": msg})
        if (repo / ".git").exists():
            record(str(repo / ".git"))
            rc, msg = icacls(str(repo / ".git"), "/deny", f"*{pkg_s}:(OI)(CI)(W,D)")
            emit({"tag": "G-deny-git", "rc": rc, "out": msg})

        def deny(path: Path, who: str, rights: str, tag: str) -> None:
            record(str(path))
            sids = {"pkg": [pkg_s], "cap": [cap_s], "both": [pkg_s, cap_s]}[who]
            for sid in sids:
                rc, msg = icacls(str(path), "/deny", f"*{sid}:(OI)(CI){rights}")
                emit({"tag": f"G-{tag}", "sid": sid, "rc": rc, "out": msg})

        for tag, d in drepos.items():
            shape = dshapes[tag]
            if shape == "noinherit":
                # Remove the allow instead of adding a deny: copy inherited ACEs, then drop the capability's.
                record(str(d / ".git"))
                rc1, m1 = icacls(str(d / ".git"), "/inheritance:d")
                rc2, m2 = icacls(str(d / ".git"), "/remove:g", f"*{cap_s}")
                emit({"tag": f"G-{tag}", "rc": [rc1, rc2], "out": [m1, m2]})
            else:
                deny(d / ".git", shape, "(W,D)", tag)
        for tag, d in edirs.items():
            deny(d, eshapes[tag], "(F)", tag)
        needs: dict[str, str] = {}
        for name, d in wanted.items():
            if grantable(d):
                record(str(d))
                rc, msg = icacls(str(d), "/grant", f"*{pkg_s}:(OI)(CI)RX")
                emit({"tag": f"G-read-{name}", "path": str(d), "rc": rc, "out": msg})
            else:
                needs[name] = str(d)
        emit({"tag": "G-not-granted", "note": "outside --base; probes show whether the default ACL is enough",
              "paths": needs})

        cur, prop = env_current(ws), env_proposed(ws)
        py, T = tools["python"], a.timeout
        none, inet = [cap], [cap, net]

        deadline = time.monotonic() + a.budget
        gate = {"open": True}

        def go(tag, argv, *, env=prop, caps=none, cwd=ws, package="probe"):
            if not gate["open"]:
                emit({"tag": tag, "child_started": False, "error": "skipped: launch ladder failed"})
                return None
            if time.monotonic() > deadline:
                emit({"tag": tag, "child_started": False, "error": "skipped: probe budget used up"})
                return None
            try:
                rec = run_confined(argv, cwd=cwd, env=env, pkg=pkg if package == "probe" else None, caps=caps,
                                   out_dir=out, tag=tag, timeout=T)
            except Exception as e:
                rec = {"tag": tag, "child_started": False, "error": repr(e)}
            emit(rec)
            return rec

        # L: launch ladder, smallest first. cmd.exe lives in System32, which every AppContainer can
        # read, so a failure here is the launch itself, never a missing grant.
        cmd = str(Path(os.environ["SystemRoot"]) / "System32" / "cmd.exe")
        l0 = go("L0-control-no-appcontainer", [cmd, "/d", "/c", "exit 7"], caps=[], package=None)
        l1 = go("L1-appcontainer-no-capabilities", [cmd, "/d", "/c", "exit 7"], caps=[])
        l2 = go("L2-appcontainer-root-capability", [cmd, "/d", "/c", "exit 7"])
        ok = [bool(r and r.get("child_started") and r.get("exit_code") == 7) for r in (l0, l1, l2)]
        emit({"tag": "L-verdict", "control": ok[0], "appcontainer": ok[1], "with_capability": ok[2],
              "profile_registered_by_flag": bool(a.create_profile),
              "reading": ("launch code broken (control fails too)" if not ok[0] else
                          "AppContainer launch fails; with no --create-profile the profile is unregistered" if not ok[1] else
                          "capability SID breaks the launch" if not ok[2] else "launch path works")})
        gate["open"] = all(ok)

        # P1 env
        p1 = "import os,socket,asyncio;print(sorted(os.environ));asyncio.new_event_loop().close();print('asyncio-ok')"
        go("P1-env-current", [py, "-c", p1], env=cur)
        go("P1-env-proposed", [py, "-c", p1])
        go("P1-home", [py, "-c", "import os,pathlib;print(pathlib.Path.home(), os.path.expanduser('~'))"])
        # P2 loopback: same-process socketpair, then a host listener outside the container
        lsock = socket.socket(); lsock.bind(("127.0.0.1", 0)); lsock.listen(4)
        port = lsock.getsockname()[1]
        threading.Thread(target=lambda: [lsock.accept() for _ in range(4)], daemon=True).start()
        p2 = ("import socket;a,b=socket.socketpair();a.send(b'x');print('socketpair', b.recv(1));"
              f"s=socket.socket();s.settimeout(5);print('host-loopback', s.connect_ex(('127.0.0.1',{port})))")
        go("P2-loopback-nonet", [py, "-c", p2])
        go("P2-loopback-inet", [py, "-c", p2], caps=inet)
        # P3 named pipes
        go("P3-mp-pipe", [py, "-c", "import multiprocessing as m;print(m.Pipe())"])
        go("P3-subprocess", [py, "-c", "import subprocess,sys;print(subprocess.run([sys.executable,'-c','print(1)'],capture_output=True).stdout)"])
        # P4 git
        if tools.get("git"):
            git = tools["git"]
            for sub in (["status", "--porcelain"], ["log", "-1", "--oneline"], ["diff", "--stat"]):
                go(f"P4-git-{sub[0]}", [git, *sub], cwd=repo)
            go("P4-git-status-current-env", [git, "status", "--porcelain"], cwd=repo, env=cur)
            sys_env = dict(prop); sys_env.pop("GIT_CONFIG_NOSYSTEM", None)
            go("P4-git-status-with-system-config", [git, "status", "--porcelain"], cwd=repo, env=sys_env)
            go("P4-git-commit-must-fail", [git, "-c", "user.name=p", "-c", "user.email=p@invalid", "commit", "-am", "x"], cwd=repo)
            shx = Path(git).resolve().parent.parent / "usr" / "bin" / "sh.exe"
            if shx.exists():
                go("P4-msys-sh", [str(shx), "-c", "true"])
        # P5 node / npm
        if tools.get("node"):
            node = str(Path(tools["node"]).resolve())
            go("P5-node-homedir", [node, "-e", "console.log(require('os').homedir())"])
            go("P5-node-require", [node, "-e", "console.log(require('./x.js'))"], cwd=ws / "pkg")
            go("P5-node-spawn-pipe", [node, "-e", "const r=require('child_process').spawnSync(process.execPath,['-e','console.log(7)'],{stdio:'pipe',timeout:20000});console.log(r.status,String(r.stdout),r.error&&r.error.code)"], )
            go("P5-node-spawn-inherit", [node, "-e", "const r=require('child_process').spawnSync(process.execPath,['-e','console.log(7)'],{stdio:'inherit',timeout:20000});console.log(r.status,r.error&&r.error.code)"])
            cli = Path(node).parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
            if cli.exists():
                go("P5-npm-run", [node, str(cli), "run", "hello"], cwd=ws / "pkg")
                go("P5-npm-run-foreground", [node, str(cli), "run", "hello", "--foreground-scripts"], cwd=ws / "pkg")
        # P6 pip
        go("P6-pip-list", [py, "-m", "pip", "list", "--disable-pip-version-check"])
        go("P6-venv", [py, "-m", "venv", "--without-pip", str(ws / "venv7")])
        vpy = ws / "venv7" / "Scripts" / "python.exe"
        if vpy.exists():
            go("P6-venv-python", [str(vpy), "-c", "import sys;print(sys.prefix, sys.base_prefix)"])
        go("P6-write-outside-must-fail", [py, "-c", f"open(r'{root}\\outside.txt','w').write('x')"])
        # P7 cargo (toolchain cargo resolved through rustup, outside the container)
        if cargo:
            go("P7-cargo-direct-1", [cargo, "build", "--offline"], cwd=crate)
            go("P7-cargo-direct-2", [cargo, "build", "--offline"], cwd=crate)
            links = [str(p) for p in (crate / "target").rglob("*") if p.is_file() and os.lstat(p).st_nlink > 1]
            emit({"tag": "P7-hardlinks", "count": len(links), "sample": links[:5]})
        if tools.get("cargo"):
            # Not resolved: on Steve cargo.exe is a symlink to rustup.exe, and resolving it ran rustup.
            go("P7-cargo-proxy", [str(Path(tools["cargo"])), "-V"], cwd=crate)
        # Unconfined control: does this machine link a Rust binary at all, outside any container?
        if cargo:
            ctl = ws / "crate-control"
            shutil.copytree(crate, ctl, dirs_exist_ok=True, ignore=shutil.ignore_patterns("target"))
            try:
                r = subprocess.run([cargo, "build", "--offline"], cwd=ctl, capture_output=True, text=True, timeout=120)
                emit({"tag": "P7-cargo-unconfined-control", "exit_code": r.returncode,
                      "stderr_tail": r.stderr[-800:], "where_link": shutil.which("link")})
            except Exception as e:
                emit({"tag": "P7-cargo-unconfined-control", "error": repr(e)})
        # D: each deny shape vs a real commit, plus a direct write and mkdir in .git. ACL dumped
        # just before; HEAD read unconfined before and after, so "exit 0" is never the only evidence.
        for tag, d in drepos.items():
            _, acl = icacls_full(str(d / ".git"))
            head0 = subprocess.run([tools["git"], "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
            go(f"{tag}-commit-must-fail", [tools["git"], "-c", "user.name=p", "-c", "user.email=p@invalid",
                                           "commit", "-am", "x"], cwd=d)
            head1 = subprocess.run([tools["git"], "rev-parse", "HEAD"], cwd=d, capture_output=True, text=True).stdout.strip()
            go(f"{tag}-write-must-fail", [py, "-c", "import os,sys\nfor a in ('w','mkdir'):\n    try:\n        "
                                          "(open(os.path.join('.git','probe-w.txt'),'w').close() if a=='w' else os.mkdir(os.path.join('.git','probe-d')));print(a,'ALLOWED')\n    "
                                          "except OSError as x: print(a, type(x).__name__)"], cwd=d)
            emit({"tag": f"{tag}-head", "head_moved": head0 != head1, "before": head0, "after": head1,
                  "acl_before": acl.strip()[-1500:]})
        # E: deny-all shapes inside the write root: read, list and write must all fail.
        for tag, d in edirs.items():
            _, acl = icacls_full(str(d))
            go(f"{tag}-access-must-fail", [py, "-c", "import os,sys\nd=sys.argv[1]\nfor a in ('read','list','write'):\n    try:\n        "
                                           "(open(os.path.join(d,'fixture.txt'),'rb').close() if a=='read' else os.listdir(d) if a=='list' else open(os.path.join(d,'w.txt'),'w').close());print(a,'ALLOWED')\n    "
                                           "except OSError as x: print(a, type(x).__name__)", str(d)])
            emit({"tag": f"{tag}-acl", "acl_before": acl.strip()[-1500:]})
        # P8 credential files: only whether open() succeeds, never the contents
        real = real_profile
        cands = [real / ".npmrc", real / ".cargo" / "credentials.toml", real / ".git-credentials",
                 Path(os.environ.get("APPDATA", real)) / "pip" / "pip.ini", real / ".ssh"]
        p8 = ("import sys,os\nfor p in sys.argv[1:]:\n    e=os.path.exists(p)\n    try:\n        "
              "(os.listdir(p) if os.path.isdir(p) else open(p,'rb').close());r='OPENED'\n    "
              "except OSError as x: r=type(x).__name__\n    print(p, 'exists' if e else 'absent?', r)")
        go("P8-credential-reads-must-fail", [py, "-c", p8, *map(str, cands)])
        go("P8b-fixture-outside-read-must-fail", [py, "-c", p8, str(fixture_secret), str(fixture_secret.parent)])
    finally:
        # Each clean-up step runs whatever the one before it raised: the ACE revokes one by one,
        # then the profile delete, then the check. An exception is logged and never skips a step.
        try:
            for p in reversed(granted):
                try:
                    rc, msg = icacls(p, "/remove", f"*{pkg_s}", f"*{cap_s}")
                    emit({"tag": "R-revoke", "path": p, "rc": rc, "out": msg})
                except Exception as e:
                    emit({"tag": "R-revoke", "path": p, "error": repr(e)})
        finally:
            try:
                if profile_made:
                    hr, left = delete_probe_profile(PROFILE, pkg_s)
                    gone = not any(left.values())
                    emit({"tag": "R-profile", "hr": f"0x{hr:08x}", "traces_left": left, "all_gone": gone})
                    profile_made = not gone
            except Exception as e:
                emit({"tag": "R-profile", "error": repr(e)})
            finally:
                try:
                    write_journal()
                    clean = all_revoked(granted, pkg_s, cap_s) and not profile_made
                    emit({"tag": "R-check", "all_probe_aces_gone": clean,
                          "note": "full icacls /T /C listing of every granted path; profile deleted if made" if clean
                          else "PROBE ACEs OR PROFILE MAY REMAIN: journal kept; rerun with --revoke-only and the same flags"})
                    if clean:
                        journal.unlink(missing_ok=True)
                except Exception as e:
                    emit({"tag": "R-check", "all_probe_aces_gone": False, "error": repr(e),
                          "note": "journal kept; rerun with --revoke-only and the same flags"})
                finally:
                    log.close()
    _summary(results, out / "SUMMARY.md")
    return 0


_AC_KEY = r"Software\Classes\Local Settings\Software\Microsoft\Windows\CurrentVersion\AppContainer"


def profile_traces(pkg_s: str) -> dict[str, bool]:
    """Which traces of the probe profile exist now: the HKCU Mappings and Storage keys for its SID,
    and its %LOCALAPPDATA%\\Packages folder. Unknown (an error other than not-found) counts as present."""
    import winreg

    out: dict[str, bool] = {}
    # Mappings is keyed by SID; Storage is keyed by the lower-case moniker (measured on Steve:
    # 166/166 Mappings by SID, 129/129 Storage by moniker). Both spellings are checked for both.
    for label, sub in (("Mappings", pkg_s), ("Mappings-moniker", PROFILE.lower()),
                       ("Storage", PROFILE.lower()), ("Storage-sid", pkg_s)):
        kind = label.split("-")[0]
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_AC_KEY}\\{kind}\\{sub}"))
            out[label] = True
        except FileNotFoundError:
            out[label] = False
        except OSError:
            out[label] = True
    local = os.environ.get("LOCALAPPDATA")
    out["Packages"] = True if not local else (Path(local) / "Packages" / PROFILE).exists()
    return out


def delete_probe_profile(name: str, pkg_s: str) -> tuple[int, dict[str, bool]]:
    """Delete the probe's own profile and nothing else, then report what is left."""
    if name != PROFILE or pkg_s != sid_str(package_sid()):
        raise ValueError(f"refusing to delete a profile that is not {PROFILE}")
    hr = uenv.DeleteAppContainerProfile(PROFILE) & 0xFFFFFFFF
    return hr, profile_traces(pkg_s)


def all_revoked(paths: list[str], *sids: str) -> bool:
    """True only when the FULL `icacls <path> /T /C` listing of every listed path (each file and folder
    under it) exits 0 and names neither probe SID nor the profile, case-insensitively."""
    needles = [n.upper() for n in (*sids, PROFILE)]
    for p in paths:
        if not os.path.exists(p):
            continue
        rc, text = icacls_full(p, "/T", "/C")
        upper = text.upper()
        if rc != 0 or any(n in upper for n in needles):
            return False
    return True


def _revoke_from_journal(journal: Path, ws: Path, allowed: list[Path]) -> int:
    _sigs()
    if not journal.exists():
        print("no journal: nothing to revoke"); return 0
    data = json.loads(journal.read_text(encoding="utf-8"))
    pkg_s, cap_s = data["package_sid"], data["capability_sid"]
    # Cross-check: the SIDs are pure derivations of fixed names, so a tampered journal can't name others.
    if pkg_s != sid_str(package_sid()) or cap_s != sid_str(capability_sid(root_capability_name(str(ws)))):
        print("journal SIDs do not match this probe's identity; refusing"); return 2
    # Same scope as the grants: a journal path outside --base / --allow-grant is never touched.
    paths = [p for p in data["paths"]
             if any(x == Path(p).resolve() or x in Path(p).resolve().parents for x in allowed)]
    for p in data["paths"]:
        if p not in paths:
            print(f"skip {p}: outside --base and every --allow-grant, not touched")
    for p in reversed(paths):
        rc, msg = icacls(p, "/remove", f"*{pkg_s}", f"*{cap_s}")
        print(f"revoke {p}: rc={rc} {msg}")
    profile_left = False
    if data.get("profile_created_by_probe"):
        if data.get("profile_name") != PROFILE:
            print("journal names a different profile; refusing"); return 2
        hr, left = delete_probe_profile(PROFILE, pkg_s)
        print(f"delete profile {PROFILE}: hr=0x{hr:08x} traces left: {left}")
        profile_left = any(left.values())
        if not profile_left:
            data["profile_created_by_probe"] = False
            journal.write_text(json.dumps(data), encoding="utf-8")
    if not profile_left and len(paths) == len(data["paths"]) and all_revoked(paths, pkg_s, cap_s):
        journal.unlink()
        print("all probe ACEs removed")
        return 0
    print("SOME PROBE ACEs REMAIN: see icacls output above"); return 1


def _summary(results: Path, dest: Path) -> None:
    rows = [json.loads(l) for l in results.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = ["| tag | started | exit | s | procs | peak MB | stderr tail |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        if "child_started" in r:
            err = (r.get("error") or r.get("stderr_tail") or "").replace("\n", " ").replace("|", "/")[-160:]
            lines.append(f"| {r['tag']} | {r.get('child_started')} | {r.get('exit_hex', '')}{' TIMEOUT' if r.get('timed_out') else ''} "
                         f"| {r.get('seconds', '')} | {r.get('total_processes', '')} | {r.get('peak_job_mb', '')} | {err} |")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
