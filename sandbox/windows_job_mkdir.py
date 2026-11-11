"""Windows, inside a confined VOOL job only: `os.mkdir(path, 0o700)` becomes a plain `os.mkdir(path)`.

CPython 3.12.4 and later (gh-118486) create a folder asked for with mode 0o700 on Windows with the
protected DACL `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`. No ACE there names a confined
job's capability, so the AppContainer job loses every right on a folder it has just made. pytest's
`tmp_path`, `tmpdir` and `tmp_path_factory`, its cache plugin, and `tempfile.mkdtemp` /
`TemporaryDirectory` all ask for 0o700, so all of them failed inside the Windows sandbox.

installer/bundle/build_bundle.ps1 ships this file into the bundled Windows Python as
`Lib\\site-packages\\vool_windows_job_mkdir.py`, with a one-line `vool_windows_job_mkdir.pth` that
imports it at every start. It does something only on Windows and only when `VOOL_WINDOWS_CONFINED_JOB`
is exactly "1", which VOOL's job runner sets in the environment of a confined Python job
(`sandbox.job_runner._windows_job_environment`) and nowhere else. Then a 0o700 folder is made like any
other: it inherits its parent's DACL, which inside the job's write root is the user, SYSTEM,
Administrators and that job's own capability. Every other mode, the path and dir_fd are passed on
unchanged, and nothing but `os.mkdir` is touched. The AppContainer and its ACLs stay the boundary:
this cannot reach a folder the job may not already write. It also requires the process token to be
an AppContainer's (`_token_is_appcontainer`, the one use of ctypes), so the variable alone never
changes an unconfined process; any error there means not wrapped. Stdlib only, no VOOL code: the job
can read the interpreter's folders, not VOOL's.
"""
import os

#: Set to "1" by VOOL's job runner for a confined Windows Python job, and nowhere else.
ENV_VAR = "VOOL_WINDOWS_CONFINED_JOB"
_MARK = "_vool_confined_job_mkdir"


def _token_is_appcontainer():
    """True only when this process's token says TokenIsAppContainer (class 29). Any error: False."""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ]
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
            return False
        try:
            value = wintypes.DWORD(0)
            size = wintypes.DWORD(0)
            ok = advapi32.GetTokenInformation(token, 29, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(size))
            return bool(ok) and size.value == ctypes.sizeof(value) and value.value != 0
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return False


def install(*, environ=None, module=os, nt=None):
    """Wrap `module.mkdir` when this is Windows, the variable is "1" and the process token is an
    AppContainer's. True when it is wrapped."""
    environ = os.environ if environ is None else environ
    nt = (os.name == "nt") if nt is None else nt
    if not nt or environ.get(ENV_VAR) != "1":
        return False
    try:
        contained = _token_is_appcontainer() is True
    except Exception:
        contained = False
    if not contained:
        return False
    real = module.mkdir
    if getattr(real, _MARK, False):
        return True

    def mkdir(path, mode=0o777, *, dir_fd=None):
        if mode == 0o700:
            mode = 0o777
        return real(path, mode, dir_fd=dir_fd)

    setattr(mkdir, _MARK, True)
    mkdir.__doc__ = real.__doc__
    module.mkdir = mkdir
    return True


install()
